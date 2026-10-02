"""Run from the repository root: python -m opslens demo."""

import argparse
import getpass
import json
import os
import sys
import uuid
from pathlib import Path

from .providers import ChatProvider, DeepSeekProvider, DemoProvider, ProviderError
from .runtime import AgentRuntime
from .store import RunStore
from .tools import IncidentTools
from .config import load_local_env


def _provider(mode, profile="deepseek", prompt_key=False):
    if mode == "demo":
        return DemoProvider()
    load_local_env()
    if profile == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key and prompt_key:
            if not sys.stdin.isatty():
                raise ValueError("--prompt-key requires your interactive terminal")
            api_key = getpass.getpass("DeepSeek API Key (hidden, not saved): ").strip()
            if api_key:
                os.environ["DEEPSEEK_API_KEY"] = api_key  # This process only, never persisted.
        if not api_key:
            raise ValueError("set DEEPSEEK_API_KEY locally, or run with --prompt-key in your terminal")
        return DeepSeekProvider(api_key=api_key, model=os.environ.get("OPSLENS_MODEL", "deepseek-flash"))
    keys = ("OPSLENS_BASE_URL", "OPSLENS_MODEL", "OPSLENS_API_KEY")
    values = [os.environ.get(key, "").strip() for key in keys]
    if not all(values):
        raise ValueError("live mode requires OPSLENS_BASE_URL, OPSLENS_MODEL, OPSLENS_API_KEY")
    return ChatProvider(base_url=values[0], model=values[1], api_key=values[2])


def _export(state, directory):
    directory = Path(directory) / state["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    public = {key: value for key, value in state.items() if key != "messages"}
    (directory / "result.json").write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "trace.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in state["events"]), encoding="utf-8"
    )
    report = state["report"]
    lines = ["# OpsLens 诊断报告", "", f"- Run: `{state['run_id']}`",
             f"- Case: `{state['case_id']}`", f"- Mode: `{state['provider']}`",
             f"- Status: `{state['status']}`", ""]
    lines += [f"- Policy: `{state.get('policy', 'react')}`", f"- Source: `{state.get('source', 'unknown')}`", ""]
    audit = state.get("report_audit")
    if audit is not None:
        lines += ["## 数值风险检查", "", f"规则版本：`{audit['version']}`。这不是全面的语义或因果验证。", ""]
        lines += ([f"- {item['code']}: {item['message']}" for item in audit["issues"]]
                  or ["本次规则未检出已覆盖的数值风险，结论仍需审阅。"])
        lines.append("")
    if state["provider"] == "scripted-demo":
        lines += ["> 固定脚本演示：用于验证系统流程，不代表模型自主诊断能力。", ""]
    if report:
        lines += ["## 结论", "", report["root_cause"], "", report["explanation"], "", "## 证据", ""]
        for record_id in report["evidence_ids"]:
            record = state["observed"][record_id]
            lines += [f"- `{record_id}`: {json.dumps(record, ensure_ascii=False)}"]
        lines += ["", "## 待人工验证的建议", "", *[f"- {item}" for item in report["next_steps"]]]
        if state.get("hypotheses"):
            lines += ["", "## 假设与反证记录", ""]
            for item in state["hypotheses"].values():
                lines += [f"- {item['cause_code']} / {item['status']}: {item['statement']}",
                          f"  支持: {', '.join(item['supporting_evidence_ids']) or '无'}；反证: {', '.join(item['contradicting_evidence_ids']) or '无'}"]
    else:
        lines += ["本次运行尚无有效诊断。请查看 trace.jsonl 中的工具错误或停止原因。"]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": state["run_id"], "status": state["status"],
                      "provider": state["provider"], "steps": state["steps"],
                      "tool_calls": state["tool_calls"], "report": str((directory / "report.md").resolve())},
                     ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpsLens incident investigation lab")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("demo", "run", "resume"):
        child = sub.add_parser(command)
        child.add_argument("--case", type=Path, default=Path("data/cases/inc001.json"))
        child.add_argument("--db", type=Path, default=Path("runs/opslens.sqlite3"))
        child.add_argument("--output", type=Path, default=Path("runs"))
        child.add_argument("--stop-after", type=int, help="pause after N additional model steps")
        child.add_argument("--provider", choices=["deepseek", "compatible"], default="deepseek")
        child.add_argument("--prompt-key", action="store_true", help="read DeepSeek key without echo or saving")
        child.add_argument("--policy", choices=["react", "investigator"], help="live defaults to investigator, demo to react")
        if command == "resume":
            child.add_argument("run_id")
            child.add_argument("--mode", choices=["demo", "live"], default="demo")
        else:
            child.add_argument("--max-steps", type=int, default=12)
            child.add_argument("--max-tools", type=int, default=24)
            child.add_argument("--max-tokens", type=int, default=60000, help="stop before next request when reported total reaches threshold")
            child.add_argument("--context-chars", type=int, default=48000)
    evaluate = sub.add_parser("evaluate", help="score saved runs; does not call a model")
    evaluate.add_argument("results", nargs="+", type=Path)
    evaluate.add_argument("--labels", type=Path, default=Path("eval/labels.json"))
    evaluate.add_argument("--output", type=Path, default=Path("runs/evaluation.json"))
    lab = sub.add_parser("lab", help="collect actual local HTTP fault and recovery observations")
    lab.add_argument("--scenario", choices=["healthy", "pool_exhaustion", "dependency_timeout", "bad_configuration"], default="pool_exhaustion")
    lab.add_argument("--suite", action="store_true", help="collect all four scenarios")
    lab.add_argument("--seeds", type=int, nargs="+", default=[0])
    lab.add_argument("--output", type=Path, default=Path("runs/lab"))
    benchmark = sub.add_parser("benchmark", help="paired policy experiments over a hash-locked manifest")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output", type=Path)
    benchmark.add_argument("--policies", nargs="+", choices=["single-shot", "fixed-workflow", "react", "investigator"],
                           default=["single-shot", "fixed-workflow", "react", "investigator"])
    benchmark.add_argument("--repeats", type=int, default=1)
    benchmark.add_argument("--max-model-calls", type=int, default=60)
    benchmark.add_argument("--max-steps", type=int, default=12)
    benchmark.add_argument("--max-tools", type=int, default=24)
    benchmark.add_argument("--provider", choices=["deepseek", "compatible"], default="deepseek")
    benchmark.add_argument("--prompt-key", action="store_true")
    benchmark.add_argument("--dry-run", action="store_true", help="validate data and print plan without model calls")
    serve = sub.add_parser("serve", help="open the local investigation workbench")
    serve.add_argument("--cases", nargs="+", type=Path, default=[Path("data/cases/inc001.json")])
    serve.add_argument("--mode", choices=["demo", "live"], default="demo")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--db", type=Path, default=Path("runs/workbench.sqlite3"))
    serve.add_argument("--output", type=Path, default=Path("runs"))
    serve.add_argument("--provider", choices=["deepseek", "compatible"], default="deepseek")
    serve.add_argument("--prompt-key", action="store_true")
    mcp = sub.add_parser("mcp", help="expose read-only incident tools over MCP stdio")
    mcp.add_argument("--case", type=Path, default=Path("data/cases/inc001.json"))
    args = parser.parse_args(argv)
    try:
        if args.command == "lab":
            from .lab import collect_case, collect_suite
            if args.suite:
                result = collect_suite(args.output, seeds=args.seeds)
                print(json.dumps({"manifest_path": result["manifest_path"], "case_count": len(result["cases"]),
                                  "cases": [{"case_path": case["case_path"], "validation": case["provenance"]["validation"]}
                                            for case in result["cases"]]}, ensure_ascii=False, indent=2))
            else:
                if len(args.seeds) != 1:
                    raise ValueError("single scenario accepts one seed; use --suite for multiple seeds")
                result = collect_case(args.output, args.scenario, seed=args.seeds[0])
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "benchmark":
            from .benchmark import execute_benchmark, validate_manifest
            plan = validate_manifest(args.manifest)
            plan.update(policies=args.policies, repeats=args.repeats, max_model_calls=args.max_model_calls)
            if args.dry_run:
                if not 1 <= args.repeats <= 20 or args.max_model_calls < 1:
                    raise ValueError("invalid repeat count or model call budget")
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                return 0
            # Resolve configuration once before scheduling; key remains in memory.
            _provider("live", args.provider, args.prompt_key)
            output = args.output or Path("runs") / ("benchmark-" + uuid.uuid4().hex[:10])
            result = execute_benchmark(args.manifest, output, lambda: _provider("live", args.provider),
                                       args.policies, repeats=args.repeats, max_steps=args.max_steps,
                                       max_tool_calls=args.max_tools, max_model_calls=args.max_model_calls)
            print(json.dumps({"output": str(output.resolve()), "summary": result}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "serve":
            from .server import create_server
            if args.mode == "demo" and any(IncidentTools(path).case_id != "inc001" for path in args.cases):
                raise ValueError("demo workbench supports inc001 only; select --mode live for other cases")
            _provider(args.mode, args.provider, args.prompt_key)
            with create_server(args.cases, args.db, args.output, lambda: _provider(args.mode, args.provider), port=args.port) as server:
                print(f"OpsLens workbench: http://127.0.0.1:{server.server_address[1]}", flush=True)
                server.serve_forever()
            return 0
        if args.command == "mcp":
            from .mcp import serve_stdio
            return serve_stdio(args.case)
        if args.command == "evaluate":
            from .evaluation import evaluate_runs
            labels = json.loads(args.labels.read_text(encoding="utf-8"))
            states = [json.loads(path.read_text(encoding="utf-8")) for path in args.results]
            result = evaluate_runs(states, labels)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        mode = args.mode if args.command == "resume" else ("demo" if args.command == "demo" else "live")
        tools = IncidentTools(args.case)
        if mode == "demo" and tools.case_id != "inc001":
            raise ValueError("scripted demo supports only inc001; use run for live cases")
        store = RunStore(args.db)
        loaded = store.load(args.run_id) if args.command == "resume" else None
        policy = args.policy or (loaded.get("policy", "react") if loaded else "react" if mode == "demo" else "investigator")
        if mode == "demo" and policy != "react":
            raise ValueError("scripted demo supports react only; investigator requires a live model")
        runtime = AgentRuntime(tools, _provider(mode, args.provider, args.prompt_key), store, policy=policy)
        state = loaded if loaded else runtime.start(args.max_steps, args.max_tools, args.max_tokens, args.context_chars)
        print(f"Run {state['run_id']} | {state['provider']}", file=sys.stderr, flush=True)
        state = runtime.run(state, stop_after_steps=args.stop_after)
        _export(state, args.output)
        return 0 if state["status"] in ("completed", "paused") else 2
    except (ValueError, KeyError, OSError, ProviderError) as error:
        print(f"OpsLens: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Resume using the saved run_id in the checkpoint database.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

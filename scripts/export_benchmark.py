"""Export reproducible public benchmark evidence, excluding checkpoints/messages.

Run as python -m scripts.export_benchmark INPUT OUTPUT. Only explicit artifact
types are copied; credentials, databases and arbitrary files are never included.
"""

import argparse
import hashlib
import json
from pathlib import Path

from opslens.benchmark import load_manifest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    manifest = load_manifest(source / "manifest.json")
    summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    index = json.loads((source / "attempts.json").read_text(encoding="utf-8"))
    if output.exists() and any(output.iterdir()):
        raise ValueError("export output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[1] / "opslens"
    for name, expected in summary["source_hashes"].items():
        if Path(name).name != name or not name.endswith(".py"):
            raise ValueError("invalid source manifest filename")
        raw = (source_root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("current source differs from experiment; restore frozen source before exporting")
        destination = output / "source" / "opslens" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    # Manifest paths in the export are generated here, never taken as write paths.
    frozen = {"schema_version": 1, "dataset_version": manifest["dataset_version"],
              "observation_budget": manifest["observation_budget"], "cases": []}
    for number, entry in enumerate(manifest["cases"], 1):
        relative = f"cases/case-{number:04d}.json"
        destination = output / relative
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(entry["raw_snapshot"])
        frozen["cases"].append({key: entry[key] for key in ("case_id", "group_id", "sha256", "label", "source_kind", "source")})
        frozen["cases"][-1]["path"] = relative
        if "raw_provenance" in entry:
            provenance_path = f"cases/provenance-{number:04d}.json"
            (output / provenance_path).write_bytes(entry["raw_provenance"])
            frozen["cases"][-1]["provenance_path"] = provenance_path
    write(output / "manifest.json", frozen)
    summary["output_dir"] = "."
    # Preserve original manifest digest as experiment identity, not as digest of
    # this portable projection. Each copied snapshot is byte-identical.
    summary["export_note"] = "Portable projection; original experiment/manifest hashes retained. Revalidate copied snapshot hashes via manifest.json. No checkpoints or model message histories included."
    write(output / "summary.json", summary)
    write(output / "experiment.json", json.loads((source / "experiment.json").read_text(encoding="utf-8")))
    lines = ["# 真实模型对照实验", "", "数据与源码摘要见 experiment.json；完整指标见 summary.json。", "",
             "小样本探索性实验。来源与划分分开解释，不能据此宣称生产收益或全面语义正确。", "",
             "| 案例 | 来源 / 划分 | 策略 | 自动检查 | 模型请求 | Tokens | 失败类别 |",
             "| --- | --- | --- | --- | ---: | ---: | --- |"]
    public_rows = []
    for number, row in enumerate(index["attempts"], 1):
        state_path = (source / row["state_path"]).resolve()
        if not state_path.is_relative_to(source):
            raise ValueError("attempt path escapes source")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # Public fields come from the benchmark; omit messages defensively.
        state.pop("messages", None)
        relative = f"attempts/attempt-{number:05d}"
        events = state.pop("events", [])
        write(output / relative / "state.json", state)
        write(output / relative / "score.json", row["score"])
        (output / relative / "trace.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
        public_rows.append({**row, "state_path": f"{relative}/state.json", "score_path": f"{relative}/score.json"})
        score = row["score"]
        lines.append(f"| [{row['case_id']}]({relative}/state.json) | {row['source_kind']} / {row['split']} | {row['policy']} | "
                     f"{'通过' if score['task_success'] else '未通过'} | {row['model_calls']} | "
                     f"{score['usage']['total_tokens']} | {score['failure_category'] or '—'} |")
    write(output / "attempts.json", {"attempts": public_rows})
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_manifest(output / "manifest.json")
    return {"output": str(output), "cases": len(frozen["cases"]), "attempts": len(public_rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

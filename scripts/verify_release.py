"""Validate an installed wheel from an empty directory without model/network calls.

Only loopback HTTP is used. The fixed demo exercises the software, not model quality.
Build first with: python -m pip wheel . --no-deps --wheel-dir dist
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import venv


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def clean_environment():
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "DEEPSEEK_API_KEY", "OPSLENS_API_KEY"):
        environment.pop(key, None)
    environment.update(PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
                       PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    return environment


def run(command, cwd, environment, *, stdin=None, timeout=45):
    result = subprocess.run(command, cwd=cwd, env=environment, input=stdin,
                            text=True, encoding="utf-8", capture_output=True,
                            timeout=timeout)
    require(result.returncode == 0,
            f"Command failed ({result.returncode}): {result.stderr[-2000:]}")
    return result.stdout


@contextmanager
def workbench(python, work, environment):
    process = subprocess.Popen(
        [str(python), "-m", "opslens", "serve", "--mode", "demo", "--port", "0"],
        cwd=work, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        first = lines.get(timeout=15)
        match = re.fullmatch(r"OpsLens workbench: (http://127\.0\.0\.1:\d+)\s*", first)
        require(match is not None, "Installed workbench did not start on loopback")
        yield match.group(1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def api(base, path, body=None, token=None):
    headers = {"Origin": base}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.update({"Content-Type": "application/json", "X-CSRF-Token": token})
    # Ignore machine proxy settings for the local test server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(base + path, data=data, headers=headers)
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def verify(wheel, report):
    environment = clean_environment()
    with tempfile.TemporaryDirectory(prefix="opslens-wheel-check-") as directory:
        temporary = Path(directory)
        isolated = temporary / "venv"
        work = temporary / "empty-working-directory"
        work.mkdir()
        venv.EnvBuilder(with_pip=True).create(isolated)
        python = isolated / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            work, environment)
        identity = json.loads(run([str(python), "-c",
            "import json,opslens; from importlib.metadata import version; "
            "print(json.dumps({'package':opslens.__file__, 'module_version':opslens.__version__, "
            "'distribution_version':version('opslens-agent')}))"], work, environment))
        require(Path(identity["package"]).resolve().is_relative_to(isolated.resolve()),
                "Import came from source checkout, not the installed wheel")
        require(identity["module_version"] == identity["distribution_version"],
                "Distribution and module versions disagree")
        report["version"] = identity["module_version"]
        report["checks"].append("isolated_wheel_install")

        command = [str(python), "-m", "opslens"]
        version = run([*command, "--version"], work, environment).strip()
        require(version == "OpsLens " + report["version"], "CLI version disagrees")
        paused = json.loads(run([*command, "demo", "--stop-after", "2"], work, environment))
        require(paused["status"] == "paused", "Demo did not pause")
        completed = json.loads(run([*command, "resume", paused["run_id"]], work, environment))
        require(completed["status"] == "completed", "Installed demo could not resume")
        state = json.loads(Path(completed["report"]).with_name("result.json").read_text(encoding="utf-8"))
        require(state["provider"] == "scripted-demo", "Unexpected model provider")
        require(set(state["report"]["evidence_ids"]).issubset(state["observed"]),
                "Demo report cites evidence never observed")
        report["checks"].append("installed_demo_pause_resume")

        team = json.loads(run([*command, "demo", "--policy", "multi-agent", "--stop-after", "3"], work, environment))
        require(team["status"] == "paused", "Installed team did not pause")
        team = json.loads(run([*command, "resume", team["run_id"]], work, environment))
        require(team["status"] == "completed" and team["steps"] == 11, "Installed team failed to resume")
        team_state = json.loads(Path(team["report"]).with_name("result.json").read_text(encoding="utf-8"))
        require(len(team_state["agents"]) == 4 and "_members" not in team_state, "Invalid public team export")
        report["checks"].append("installed_multi_agent_pause_resume")

        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "release-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "list_services", "arguments": {}}},
        ]
        output = run([*command, "mcp"], work, environment,
                     stdin="".join(json.dumps(message) + "\n" for message in messages))
        responses = [json.loads(line) for line in output.splitlines()]
        require([item["id"] for item in responses] == [1, 2, 3], "MCP lifecycle failed")
        require(len(responses[1]["result"]["tools"]) == 7, "MCP tool catalog is incomplete")
        require(responses[2]["result"]["structuredContent"]["ok"], "Installed MCP query failed")
        report["checks"].append("installed_mcp_lifecycle_and_query")

        with workbench(python, work, environment) as base:
            config = api(base, "/api/config")
            require(any(case["case_id"] == "inc001" for case in config["cases"]),
                    "Installed workbench has no bundled demo")
            created = api(base, "/api/runs", {"case_id": "inc001", "max_steps": 12,
                          "max_tool_calls": 24}, config["csrf_token"])
            run_id = created["run_id"]
            deadline = time.monotonic() + 15
            while True:
                snapshot = api(base, "/api/runs/" + run_id)
                if snapshot["status"] not in ("running", "queued", "paused"):
                    break
                require(time.monotonic() < deadline, "Workbench demo exceeded deadline")
                time.sleep(0.05)
            require(snapshot["status"] == "completed", "Workbench demo failed")
            events = api(base, "/api/runs/" + run_id + "/events?after=0")["events"]
        report["checks"].append("installed_workbench_demo")

        # Restart the actual process using the same database; no run is resubmitted.
        with workbench(python, work, environment) as base:
            listing = api(base, "/api/runs")
            require(run_id in {item["run_id"] for item in listing["runs"]},
                    "Completed investigation disappeared after restart")
            restored = api(base, "/api/runs/" + run_id)
            require(restored["status"] == "completed" and restored["report"] == snapshot["report"],
                    "Restored investigation changed its report")
            restored_events = api(base, "/api/runs/" + run_id + "/events?after=0")["events"]
            require(restored_events == events, "Restart changed stored events")
        report["checks"].append("installed_workbench_history_after_restart")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("runs/release-check.json"))
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    report = {"schema_version": 1, "passed": False, "checks": [],
              "external_model_requests": 0, "provider": "scripted-demo",
              "scope": "Installed software integration; not model diagnosis accuracy."}
    try:
        require(wheel.is_file() and wheel.suffix == ".whl", "Provide an existing wheel file")
        report.update(wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest())
        verify(wheel, report)
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

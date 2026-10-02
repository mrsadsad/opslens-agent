"""Bounded, real loopback HTTP experiments; scenario labels stay on the control plane.

Nothing here calls an LLM, a cloud service, a shell, or a container runtime.
Snapshots contain measurements of this run, not fabricated production telemetry.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import platform
import socket
import threading
import time
import uuid


SCENARIOS = ("healthy", "pool_exhaustion", "dependency_timeout", "bad_configuration")
PHASES = ("baseline", "fault", "recovery")
CONCURRENCY = 8
REQUESTS_PER_PHASE = 24
CLIENT_TIMEOUT_SECONDS = 2.0
CAUSES = {"healthy": "healthy", "pool_exhaustion": "connection_pool_exhaustion",
          "dependency_timeout": "dependency_timeout", "bad_configuration": "bad_configuration"}


class LabValidationError(RuntimeError):
    """The actual observations did not demonstrate a clean, recovered experiment."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _opaque(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _dump(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 3)


@dataclass(frozen=True)
class _Config:
    pool_max: int = CONCURRENCY
    acquire_timeout_s: float = 0.035
    dependency_timeout_s: float = 0.25
    response_delay_s: float = 0.06
    dependency_path: str = "/work"


class _Pool:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.semaphore = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.in_use = self.waiters = self.peak_in_use = self.peak_waiters = 0

    def acquire(self, timeout: float) -> tuple[bool, float]:
        started = time.perf_counter()
        with self.lock:
            self.waiters += 1
            self.peak_waiters = max(self.peak_waiters, self.waiters)
        acquired = self.semaphore.acquire(timeout=timeout)
        with self.lock:
            self.waiters -= 1
            if acquired:
                self.in_use += 1
                self.peak_in_use = max(self.peak_in_use, self.in_use)
        return acquired, round((time.perf_counter() - started) * 1000, 3)

    def release(self) -> None:
        with self.lock:
            self.in_use -= 1
        self.semaphore.release()

    def snapshot(self) -> dict:
        with self.lock:
            return {"pool_max": self.capacity, "pool_in_use": self.in_use,
                    "pool_waiters": self.waiters, "pool_peak_in_use": self.peak_in_use,
                    "pool_peak_waiters": self.peak_waiters}


class _State:
    def __init__(self, front: str, dependency: str):
        self.front, self.dependency = front, dependency
        self.config = _Config()
        self.phase = "baseline"
        self.pool = _Pool(self.config.pool_max)
        self.dependency_port = 0
        self.events: list[dict] = []
        self.condition = threading.Condition()
        self.active = 0

    def record(self, value: dict) -> None:
        with self.condition:
            self.events.append(value)

    def enter(self) -> None:
        with self.condition:
            self.active += 1

    def leave(self) -> None:
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def drain(self) -> None:
        # In timed-out calls the downstream may still be completing its response.
        with self.condition:
            if not self.condition.wait_for(lambda: self.active == 0, timeout=4.0):
                raise LabValidationError("HTTP handlers failed to drain within four seconds")


class _Server(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False


def _handler(state: _State, role: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(CLIENT_TIMEOUT_SECONDS)

        def log_message(self, *_args):
            pass

        def _respond(self, status: int, payload: dict) -> bool:
            data = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return True
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                return False

        def do_GET(self):
            state.enter()
            started = time.perf_counter()
            config, phase, pool = state.config, state.phase, state.pool
            event = {"timestamp": _now(), "phase": phase, "component": role,
                     "service": state.front if role == "checkout" else state.dependency,
                     "request_id": self.headers.get("X-Request-ID", _opaque("req")),
                     "trace_id": self.headers.get("X-Trace-ID", _opaque("trace")),
                     "method": "GET", "path": self.path, "status": 500,
                     "error_code": None, "event": "http_request"}
            acquired = False
            connection = None
            try:
                if role == "dependency":
                    if self.path != "/work":
                        event.update(status=404, error_code="route_not_found")
                    else:
                        time.sleep(config.response_delay_s)
                        event["status"] = 200
                else:
                    acquired, wait_ms = pool.acquire(config.acquire_timeout_s)
                    event.update(acquire_wait_ms=wait_ms, **pool.snapshot())
                    if not acquired:
                        event.update(status=503, error_code="pool_acquire_timeout")
                    else:
                        connection = http.client.HTTPConnection("127.0.0.1", state.dependency_port,
                                                                timeout=config.dependency_timeout_s)
                        dependency_started = time.perf_counter()
                        try:
                            connection.request("GET", config.dependency_path, headers={
                                "X-Request-ID": event["request_id"], "X-Trace-ID": event["trace_id"]})
                            response = connection.getresponse()
                            response.read()
                            event["dependency_status"] = response.status
                            if response.status == 200:
                                event["status"] = 200
                            else:
                                event.update(status=502, error_code="dependency_http_error")
                        except (TimeoutError, socket.timeout):
                            event.update(status=504, error_code="dependency_deadline_exceeded")
                        except (OSError, http.client.HTTPException) as error:
                            event.update(status=502, error_code="dependency_transport_error",
                                         error_type=type(error).__name__)
                        finally:
                            event["dependency_latency_ms"] = round((time.perf_counter() - dependency_started) * 1000, 3)
                            event["dependency_path"] = config.dependency_path
                event["response_delivered"] = self._respond(event["status"], {
                    "request_id": event["request_id"], "trace_id": event["trace_id"],
                    "error_code": event["error_code"]})
            finally:
                if connection is not None:
                    connection.close()
                if acquired:
                    pool.release()
                event["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                state.record(event)
                state.leave()

    return Handler


def _load(state: _State, port: int) -> None:
    phase = state.phase

    def request_one(_index: int) -> None:
        event = {"timestamp": _now(), "phase": phase, "component": "load_client",
                 "service": state.front, "request_id": _opaque("req"), "trace_id": _opaque("trace"),
                 "method": "GET", "path": "/checkout", "event": "http_request",
                 "status": 0, "error_code": None}
        started = time.perf_counter()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CLIENT_TIMEOUT_SECONDS)
        try:
            connection.request("GET", "/checkout", headers={"X-Request-ID": event["request_id"],
                                                             "X-Trace-ID": event["trace_id"]})
            response = connection.getresponse()
            body = json.loads(response.read())
            event.update(status=response.status, error_code=body.get("error_code"))
        except (OSError, http.client.HTTPException, ValueError) as error:
            event.update(error_code="load_client_error", error_type=type(error).__name__)
        finally:
            connection.close()
            event["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            state.record(event)

    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="opslens-lab-load") as executor:
        list(executor.map(request_one, range(REQUESTS_PER_PHASE)))
    state.drain()


def _summary(events: list[dict], phase: str, component: str) -> dict:
    rows = [row for row in events if row["phase"] == phase and row["component"] == component]
    latencies = [row["latency_ms"] for row in rows]
    errors = [row for row in rows if not 200 <= row["status"] < 300]
    result = {"phase": phase, "component": component, "requests": len(rows),
              "errors": len(errors), "error_rate": round(len(errors) / len(rows), 6) if rows else None,
              "latency_p50_ms": _percentile(latencies, 0.5), "latency_p95_ms": _percentile(latencies, 0.95),
              "status_counts": dict(sorted(Counter(str(row["status"]) for row in rows).items())),
              "error_counts": dict(sorted(Counter(row["error_code"] for row in rows if row["error_code"]).items()))}
    if component == "checkout":
        result["acquire_wait_p95_ms"] = _percentile([row.get("acquire_wait_ms", 0) for row in rows], 0.95)
        result["outbound_latency_p95_ms"] = _percentile([row["dependency_latency_ms"] for row in rows
                                                        if "dependency_latency_ms" in row], 0.95)
    return result


def _validate_observations(scenario: str, summaries: dict) -> list[str]:
    failures = []
    client = {phase: summaries[phase]["load_client"] for phase in PHASES}
    for phase in PHASES:
        if client[phase]["requests"] != REQUESTS_PER_PHASE:
            failures.append(f"{phase}: expected {REQUESTS_PER_PHASE} actual requests")
        if client[phase]["error_counts"].get("load_client_error", 0):
            failures.append(f"{phase}: the load client itself failed")
    for phase in ("baseline", "recovery"):
        if client[phase]["error_rate"] is None or client[phase]["error_rate"] > 0.1:
            failures.append(f"{phase}: excessive measured error rate")
    if scenario == "healthy":
        if client["fault"]["error_rate"] is None or client["fault"]["error_rate"] > 0.1:
            failures.append("No-op intervention unexpectedly produced errors")
    else:
        fault_rate = client["fault"]["error_rate"] or 0
        recovery_rate = client["recovery"]["error_rate"] or 0
        if fault_rate < 0.25 or recovery_rate + 0.2 >= fault_rate:
            failures.append("Fault and recovery did not produce a measurable error-rate improvement")
        expected_error = {"pool_exhaustion": "pool_acquire_timeout",
                          "dependency_timeout": "dependency_deadline_exceeded",
                          "bad_configuration": "dependency_http_error"}[scenario]
        if not summaries["fault"]["checkout"]["error_counts"].get(expected_error, 0):
            failures.append(f"Expected actual HTTP failure mode was not observed: {expected_error}")
        if scenario == "dependency_timeout":
            increase = summaries["fault"]["dependency"]["latency_p95_ms"] - summaries["baseline"]["dependency"]["latency_p95_ms"]
            if increase < 150:
                failures.append("Downstream measurements did not establish the injected latency increase")
    return failures


def collect_case(output_dir: Path, scenario: str, seed: int = 0) -> dict:
    """Run one bounded experiment and return paths, evaluator label and provenance.

    The seed stabilizes service aliases and workload configuration, not OS timing,
    ports, UUIDs, or measurements. Each invocation creates a new opaque directory.
    Failure to establish the intended effect preserves audit files and raises
    LabValidationError; a failed experiment receives no usable evaluator label.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {SCENARIOS}")
    if type(seed) is not int or not -(2**63) <= seed < 2**63:
        raise ValueError("seed must be a signed 64-bit integer")
    started = time.perf_counter()
    case_id = _opaque("case")
    destination = Path(output_dir).resolve() / case_id
    destination.mkdir(parents=True, exist_ok=False)
    aliases = ["svc-" + hashlib.sha256(f"{seed}:{role}".encode()).hexdigest()[:10]
               for role in ("front", "dependency")]
    state = _State(*aliases)
    base = _Config()
    intervention = {"healthy": base, "pool_exhaustion": replace(base, pool_max=1),
                    "dependency_timeout": replace(base, response_delay_s=0.45),
                    "bad_configuration": replace(base, dependency_path="/work-typographical-error")}[scenario]
    changes, summaries = [], {}
    servers: list[_Server] = []
    threads: list[threading.Thread] = []
    endpoints = {}
    try:
        for role in ("dependency", "checkout"):
            server = _Server(("127.0.0.1", 0), _handler(state, role))
            servers.append(server)
            endpoints[role] = {"host": "127.0.0.1", "port": server.server_port}
            if role == "dependency":
                state.dependency_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                                      name=f"opslens-lab-{case_id}-{role}", daemon=True)
            thread.start()
            threads.append(thread)
        previous = base
        for phase, config in (("baseline", base), ("fault", intervention), ("recovery", base)):
            state.phase, state.config, state.pool = phase, config, _Pool(config.pool_max)
            if phase != "baseline":
                before, after = asdict(previous), asdict(config)
                differences = {key: {"before": before[key], "after": after[key]}
                               for key in before if before[key] != after[key]}
                changes.append({"timestamp": _now(), "phase": phase,
                                "service": state.dependency if "response_delay_s" in differences else state.front,
                                "event": "effective_configuration_change", "differences": differences})
            _load(state, endpoints["checkout"]["port"])
            summaries[phase] = {component: _summary(state.events, phase, component)
                                for component in ("load_client", "checkout", "dependency")}
            summaries[phase]["checkout"].update(state.pool.snapshot())
            previous = config
    finally:
        for index in reversed(range(len(servers))):
            server = servers[index]
            if index < len(threads):
                server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3.0)

    records, metric_ids, log_ids, change_ids = [], {}, {}, {}

    def record(kind: str, service: str, phase: str, content: dict, timestamp: str | None = None) -> str:
        record_id = _opaque("e")
        records.append({"id": record_id, "kind": kind, "service": service, "phase": phase,
                        "timestamp": timestamp or _now(),
                        "content": json.dumps(content, ensure_ascii=False, sort_keys=True)})
        if kind == "log":
            records[-1].update({key: content[key] for key in ("trace_id", "request_id") if key in content})
        return record_id

    # Fault observations first so the default bounded search also sees the event.
    for phase in ("fault", "baseline", "recovery"):
        for component, service in (("load_client", state.front), ("checkout", state.front),
                                   ("dependency", state.dependency)):
            metric_ids[(phase, component)] = record("metric", service, phase, summaries[phase][component])
        caller_traces = set()
        for component, service in (("checkout", state.front), ("dependency", state.dependency)):
            phase_rows = [row for row in state.events if row["phase"] == phase and row["component"] == component]
            # Deterministic sampling of actual errors then actual successes; full raw data is retained.
            sampled = sorted(phase_rows, key=lambda row: (bool(caller_traces) and row["trace_id"] not in caller_traces,
                                                         row["status"] < 400, row["timestamp"]))[:2]
            if component == "checkout":
                caller_traces = {row["trace_id"] for row in sampled}
            log_ids[(phase, component)] = [record("log", service, phase, row, row["timestamp"]) for row in sampled]
    for change in changes:
        change_ids[change["phase"]] = record("change", change["service"], change["phase"], change, change["timestamp"])
    record("runbook", state.front, "reference", {
        "phase": "reference", "text": "HTTP incident triage: compare observed baseline, intervention-window, and recovery measurements. Correlate request_id and trace_id across caller and dependency. Distinguish pool acquisition errors, deadline errors and HTTP status errors; check effective configuration changes. A no-op control can be healthy. Missing requests are not proof of a healthy dependency. This is general guidance, not an incident diagnosis."})
    observed = summaries["fault"]["load_client"]
    snapshot = {"synthetic": False, "source": "local_fault_lab", "case_id": case_id,
                "alert": f"Local HTTP observation: {state.front} returned {observed['errors']} non-success responses in {observed['requests']} requests during phase=fault; measured p95={observed['latency_p95_ms']} ms. Compare baseline and recovery evidence. This is a local controlled experiment, not production telemetry.",
                "services": aliases, "records": records}
    case_path = destination / "snapshot.json"
    _dump(case_path, snapshot)
    request_path = destination / "requests.jsonl"
    request_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in state.events), encoding="utf-8")
    failures = _validate_observations(scenario, summaries)
    provenance = {"schema_version": 1, "source": "local_fault_lab", "case_id": case_id,
                  "scenario": scenario, "seed": seed, "group_id": f"local-http-v1:{scenario}",
                  "generated_at": _now(), "python": platform.python_version(), "platform": platform.system(),
                  "elapsed_seconds": round(time.perf_counter() - started, 3),
                  "workload": {"concurrency": CONCURRENCY, "requests_per_phase": REQUESTS_PER_PHASE,
                               "client_timeout_seconds": CLIENT_TIMEOUT_SECONDS, "phases": list(PHASES)},
                  "control_plane": {"baseline": asdict(base), "intervention": asdict(intervention)},
                  "endpoints": endpoints, "services": {"checkout": state.front, "dependency": state.dependency},
                  "configuration_events": changes, "measurements": summaries,
                  "validation": {"passed": not failures, "failures": failures},
                  "servers_closed": all(not thread.is_alive() for thread in threads),
                  "artifacts": {"snapshot": "snapshot.json", "requests": "requests.jsonl", "label": "label.json"},
                  "sha256": {"snapshot": hashlib.sha256(case_path.read_bytes()).hexdigest(),
                             "requests": hashlib.sha256(request_path.read_bytes()).hexdigest()},
                  "limitations": ["Controlled local HTTP experiment, not production traffic or a model benchmark.",
                                  "Seed reproduces service aliases and configuration; scheduling, ports, identifiers and timings vary.",
                                  "Different seeds of one fault family do not establish generalization to unseen fault families."]}
    _dump(destination / "provenance.json", provenance)
    if failures:
        raise LabValidationError(f"Experiment failed validation; audit preserved at {destination}: {'; '.join(failures)}")
    # Labels are assembled only after observing the actual failure and recovery.
    required = [metric_ids[("fault", "checkout")], metric_ids[("recovery", "load_client")]]
    if scenario != "healthy":
        required += [log_ids[("fault", "checkout")][0], change_ids["fault"]]
    else:
        required += [metric_ids[("baseline", "load_client")]]
    root_service = None if scenario == "healthy" else state.dependency if scenario == "dependency_timeout" else state.front
    label = {"cause_code": CAUSES[scenario], "root_service": root_service,
             "required_evidence": required, "split": "dev"}
    _dump(destination / "label.json", {case_id: label})
    return {"case_path": str(case_path), "label": label, "provenance": provenance}


def collect_suite(output_dir: Path, seeds: list[int] | None = None,
                  scenarios: list[str] | tuple[str, ...] | None = None) -> dict:
    """Collect a bounded dev suite and an evaluator-only, hash-pinned manifest."""
    seeds = [0] if seeds is None else seeds
    scenarios = list(SCENARIOS) if scenarios is None else list(scenarios)
    if not isinstance(seeds, list) or not seeds or len(seeds) > 10 or any(type(seed) is not int for seed in seeds):
        raise ValueError("seeds must contain between one and ten integers")
    if not scenarios or len(scenarios) > len(SCENARIOS) or any(scenario not in SCENARIOS for scenario in scenarios):
        raise ValueError("scenarios must be a nonempty selection from SCENARIOS")
    if len(set(seeds)) != len(seeds) or len(set(scenarios)) != len(scenarios):
        raise ValueError("Duplicate seeds or scenarios would duplicate the selected experiment")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases, entries = [], []
    manifest_path = output / f"manifest-{uuid.uuid4().hex}.json"
    for seed in seeds:
        for scenario in scenarios:
            result = collect_case(output, scenario, seed)
            cases.append(result)
            path = Path(result["case_path"])
            entries.append({"case_id": result["provenance"]["case_id"], "path": path.relative_to(output).as_posix(),
                            "sha256": result["provenance"]["sha256"]["snapshot"], "label": result["label"],
                            "group_id": result["provenance"]["group_id"], "source": "local_fault_lab",
                            "provenance_path": (path.parent / "provenance.json").relative_to(output).as_posix()})
            # Preserve completed entries if a later experiment fails or is interrupted.
            _dump(manifest_path, {"schema_version": 1, "cases": entries})
    return {"manifest_path": str(manifest_path), "cases": cases}

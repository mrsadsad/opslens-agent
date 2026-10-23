"""Loopback-only investigation service for the local OpsLens workbench.

This is a bounded local development server, not an Internet deployment. A
provider factory is injected by the CLI; browsers never submit credentials.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .runtime import AgentRuntime
from .store import RunStore
from .tools import IncidentTools


WORKERS = 2
QUEUE_SIZE = 4
MAX_SESSION_RUNS = 200
HISTORY_PAGE_SIZE = 50
MAX_HISTORY_PAGE_SIZE = 200
MAX_BODY_BYTES = 8192
MAX_REJECT_DRAIN_BYTES = 64 * 1024
REJECT_DRAIN_SECONDS = 0.25
_RUN_PATH = re.compile(r"/api/runs/([a-f0-9]{32})(?:/(events|cancel))?\Z")
_PRIVATE_KEYS = {"messages", "_members", "reasoning_content", "api_key", "_api_key", "authorization"}
_PUBLIC_FIELDS = {
    "run_id", "case_id", "provider", "status", "steps", "tool_calls", "max_steps",
    "max_tool_calls", "usage", "usage_complete", "model_seconds", "report", "observed",
    "pending_model_request", "max_seconds", "elapsed_seconds", "deadline_seconds",
    "hypotheses", "tool_seconds", "transport_attempts",
    "policy", "max_total_tokens", "max_context_chars", "source", "definition_hash",
    "report_audit", "agents", "phase", "partial",
}


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key.lower() not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _add_event(state, kind, **details):
    state["events"].append({"seq": len(state["events"]) + 1,
                            "time": datetime.now(timezone.utc).isoformat(),
                            "kind": kind, **details})


@dataclass
class _Job:
    runtime: AgentRuntime
    state: dict
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    phase: str = "queued"
    future: object = None


class _WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, cases, store, output_dir, provider_factory):
        self.cases = cases
        self.case_fingerprints = {case_id: case.fingerprint for case_id, case in cases.items()}
        self.store = store
        self.output_dir = Path(output_dir)
        self.provider_factory = provider_factory
        self.csrf_token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(WORKERS + QUEUE_SIZE)
        self.closing = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="opslens-run")
        self._resources_closed = False
        try:
            super().__init__(address, _Handler)
        except BaseException:
            self.executor.shutdown(wait=False, cancel_futures=True)
            raise

    def serve_forever(self, poll_interval=0.25):
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self.server_close()

    def server_close(self):
        if self._resources_closed:
            return
        self._resources_closed = True
        self.closing.set()
        with self.jobs_lock:
            for job in self.jobs.values():
                job.cancel.set()
        super().server_close()
        # Calls already in flight cannot be force-killed safely. Provider
        # transport timeouts bound how long orderly shutdown can take.
        self.executor.shutdown(wait=True, cancel_futures=False)

    def start_run(self, case_id, max_steps, max_tool_calls, policy=None):
        if self.closing.is_set():
            return None, "service_closing"
        if case_id not in self.cases:
            return None, "unknown_case"
        if not self.slots.acquire(blocking=False):
            return None, "queue_full"
        try:
            with self.jobs_lock:
                if len(self.jobs) >= MAX_SESSION_RUNS:
                    return None, "session_limit"
                provider = self.provider_factory()
                selected_policy = policy or ("react" if provider.name == "scripted-demo" else "investigator")
                if provider.name == "scripted-demo" and selected_policy not in {"react", "multi-agent"}:
                    return None, "unsupported_demo_policy"
                if selected_policy == "multi-agent":
                    if max_steps < 12 or max_tool_calls < 16:
                        return None, "invalid_team_budget"
                    from .team import TeamRuntime
                    runtime = TeamRuntime(self.cases[case_id], self.provider_factory, self.store)
                else:
                    runtime = AgentRuntime(self.cases[case_id], provider, self.store, policy=selected_policy)
                # The scripted provider only has an inc001 walkthrough.
                if runtime.provider.name == "scripted-demo" and case_id != "inc001":
                    return None, "demo_case_unsupported"
                state = runtime.start(max_steps=max_steps, max_tool_calls=max_tool_calls)
                state["status"] = "queued"
                _add_event(state, "queued")
                self.store.save(state)
                job = _Job(runtime, state)
                self.jobs[state["run_id"]] = job
                try:
                    future = self.executor.submit(self._run_job, job)
                except RuntimeError:
                    self.jobs.pop(state["run_id"], None)
                    raise
                job.future = future
                future.add_done_callback(lambda _: self.slots.release())
                return state["run_id"], None
        except Exception:
            # Never expose provider config, filesystem paths or raw exceptions.
            return None, "start_failed"
        finally:
            # An admitted job owns its semaphore permit until the future ends.
            if "future" not in locals():
                self.slots.release()

    def cancel_run(self, job):
        job.cancel.set()
        if job.future is not None and job.future.cancel():
            with job.lock:
                job.phase = job.state["status"] = "cancelled"
                _add_event(job.state, "cancelled", code="cancelled_before_start")
                self.store.save(job.state)
                self._safe_export(job.state)

    def _run_job(self, job):
        # Each run gets exactly one worker and is never externally resumed.
        with job.lock:
            state = job.state
            try:
                job.phase = "running"
                while True:
                    if job.cancel.is_set() or self.closing.is_set():
                        state["status"] = "cancelled"
                        _add_event(state, "cancelled", code="cooperative_cancel")
                        self.store.save(state)
                        break
                    state = job.runtime.run(state, stop_after_steps=1)
                    job.state = state
                    if state["status"] not in {"paused", "running"}:
                        break
                job.phase = state["status"]
            except Exception:
                job.phase = state["status"] = "server_error"
                _add_event(state, "server_error", code="run_failed")
                try:
                    self.store.save(state)
                except Exception:
                    pass
            self._safe_export(state)

    def _safe_export(self, state):
        try:
            self._export(state)
        except (OSError, ValueError):
            # Completed diagnostic evidence remains available in SQLite.
            _add_event(state, "export_error", code="artifact_write_failed")
            try:
                self.store.save(state)
            except Exception:
                pass

    def _export(self, state):
        destination = self.output_dir / state["run_id"]
        destination.mkdir(parents=True, exist_ok=True)
        snapshot = _public({key: value for key, value in state.items() if key != "messages"})
        for filename, text in (
            ("result.json", json.dumps(snapshot, ensure_ascii=False, indent=2)),
            ("trace.jsonl", "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in snapshot["events"])),
        ):
            temporary = destination / (filename + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(destination / filename)

    def snapshot(self, run_id, detail=True):
        try:
            state = self.store.load(run_id)
        except KeyError:
            return None
        return self._snapshot_state(state, detail=detail)

    def _snapshot_state(self, state, detail=True):
        # A reused case ID with different contents must not expose a previous
        # snapshot's observations. The database plus exact case allowlist defines
        # this local workbench's visible history; it is not a multi-user service.
        if (state.get("case_id") not in self.case_fingerprints
                or self.case_fingerprints[state["case_id"]] != state.get("case_fingerprint")):
            return None
        with self.jobs_lock:
            job = self.jobs.get(state["run_id"])
        fields = _PUBLIC_FIELDS if detail else _PUBLIC_FIELDS - {"observed", "report"}
        result = _public({key: value for key, value in state.items() if key in fields})
        result["historical"] = job is None
        result["checkpoint_status"] = state["status"]
        result["status"] = job.phase if job else state["status"]
        result["cancel_requested"] = job.cancel.is_set() if job else False
        if job is None:
            if state["status"] in {"queued", "running", "paused"}:
                result["status"] = "interrupted"
                result["interruption_reason"] = "not_owned_by_current_session"
            if state.get("pending_model_request"):
                # The request could have reached the provider before process
                # interruption. Do not claim its usage or billing is known.
                result["usage_complete"] = False
        result["event_cursor"] = len(state["events"])
        result["created_at"] = state["events"][0]["time"] if state["events"] else None
        return result

    def list_runs(self, limit=HISTORY_PAGE_SIZE, before=None):
        states, next_before = self.store.list_runs(self.case_fingerprints, limit=limit, before=before)
        return {"runs": [self._snapshot_state(state, detail=False) for state in states],
                "next_before": next_before, "limit": limit}


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpsLensLocal"
    sys_version = ""

    def log_message(self, format, *args):
        # Raw request paths, headers and model content do not belong in logs.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _respond(self, status, payload, content_type="application/json; charset=utf-8", nonce=None):
        raw = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
               if content_type.startswith("application/json") else payload.encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if nonce:
            self.send_header("Content-Security-Policy", "default-src 'none'; "
                             f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                             "connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, code):
        self._respond(status, {"error": {"code": code}})

    def _reject_oversized_body(self, length):
        # Send the rejection before draining, so a client waiting for the
        # response does not have to finish an oversized or incomplete upload.
        self.close_connection = True
        self._error(413, "body_too_large")
        self.wfile.flush()
        remaining = min(max(length, 0), MAX_REJECT_DRAIN_BYTES)
        deadline = time.monotonic() + REJECT_DRAIN_SECONDS
        original_timeout = self.connection.gettimeout()
        try:
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                # BufferedReader.read() may wait for the entire requested count.
                # read1() drains currently buffered/available data in one read.
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # A peer closing or the short deadline expiring ends this drain.
            pass
        finally:
            self.connection.settimeout(original_timeout)

    def _validate_origin(self, mutation=False):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if len(hosts) != 1 or hosts[0] not in allowed_hosts:
            self._error(403, "invalid_host")
            return False
        origins = self.headers.get_all("Origin", [])
        if (len(origins) > 1 or (origins and origins[0] != "http://" + hosts[0])
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self._error(403, "cross_origin_denied")
            return False
        if mutation:
            if not origins:
                self._error(403, "origin_required")
                return False
            tokens = self.headers.get_all("X-CSRF-Token", [])
            if (len(tokens) != 1 or not tokens[0].isascii()
                    or not secrets.compare_digest(tokens[0], self.server.csrf_token)):
                self._error(403, "invalid_csrf_token")
                return False
        return True

    def do_OPTIONS(self):
        self._error(403, "cross_origin_denied")

    def do_GET(self):
        try:
            self._get()
        except Exception:
            self._error(500, "service_error")

    def _get(self):
        if not self._validate_origin():
            return
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self._error(404, "not_found")
            return
        if parsed.path == "/" and not parsed.query:
            nonce = secrets.token_urlsafe(24)
            page = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
            self._respond(200, page.replace("__CSP_NONCE__", nonce), "text/html; charset=utf-8", nonce)
            return
        if parsed.path == "/api/config" and not parsed.query:
            self._respond(200, {"csrf_token": self.server.csrf_token,
                                "cases": [{"case_id": case.case_id, "alert": case.alert}
                                          for case in self.server.cases.values()],
                                "limits": {"workers": WORKERS, "queue": QUEUE_SIZE,
                                           "session_runs": MAX_SESSION_RUNS,
                                           "history_page_size": HISTORY_PAGE_SIZE,
                                           "max_history_page_size": MAX_HISTORY_PAGE_SIZE}})
            return
        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if set(query) - {"limit", "before"} or any(len(values) != 1 for values in query.values()):
                    raise ValueError
                limit = int(query.get("limit", [str(HISTORY_PAGE_SIZE)])[0])
                before = int(query["before"][0]) if "before" in query else None
                if (not 1 <= limit <= MAX_HISTORY_PAGE_SIZE
                        or (before is not None and not 1 <= before <= 2**63 - 1)):
                    raise ValueError
            except ValueError:
                self._error(400, "invalid_history_cursor")
                return
            self._respond(200, self.server.list_runs(limit=limit, before=before))
            return
        match = _RUN_PATH.fullmatch(parsed.path)
        if not match or match[2] == "cancel":
            self._error(404, "not_found")
            return
        state = self.server.snapshot(match[1])
        if state is None:
            self._error(404, "unknown_run")
            return
        if match[2] == "events":
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                    raise ValueError
                after = int(query.get("after", ["0"])[0])
                if not 0 <= after <= 2**31:
                    raise ValueError
            except ValueError:
                self._error(400, "invalid_cursor")
                return
            events = self.server.store.load(match[1])["events"]
            self._respond(200, {"events": _public([event for event in events if event["seq"] > after]),
                                "next_cursor": len(events), "status": state["status"]})
        elif parsed.query:
            self._error(400, "invalid_query")
        else:
            self._respond(200, state)

    def do_POST(self):
        try:
            self._post()
        except Exception:
            self._error(500, "service_error")

    def _post(self):
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY_BYTES:
                self._reject_oversized_body(length)
                return
            # Drain a bounded request body before early rejection. Closing a
            # socket with unread bytes can reset the connection on Windows,
            # hiding the intended 403 response from the browser.
            raw = self.rfile.read(length)
            if not self._validate_origin(mutation=True):
                return
            if self.headers.get_content_type() != "application/json":
                self._error(415, "json_required")
                return
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError, OSError):
            self._error(400, "invalid_json")
            return
        if self.path == "/api/runs":
            case_id = payload.get("case_id")
            max_steps = payload.get("max_steps", 12)
            max_tools = payload.get("max_tool_calls", 24)
            policy = payload.get("policy")
            if (set(payload) - {"case_id", "max_steps", "max_tool_calls", "policy"}
                    or not isinstance(case_id, str) or len(case_id) > 80
                    or ("policy" in payload and policy not in ("react", "investigator", "multi-agent"))
                    or type(max_steps) is not int or not 1 <= max_steps <= 100
                    or type(max_tools) is not int or not 1 <= max_tools <= 200):
                self._error(400, "invalid_request")
                return
            run_id, error = self.server.start_run(case_id, max_steps, max_tools, policy)
            if error:
                status = (400 if error in {"unsupported_demo_policy", "invalid_team_budget"} else 404 if error == "unknown_case"
                          else 429 if error in {"queue_full", "session_limit"} else 503)
                self._error(status, error)
            else:
                self._respond(202, {"run_id": run_id, "status": "queued"})
            return
        match = _RUN_PATH.fullmatch(self.path)
        if not match or match[2] != "cancel":
            self._error(404, "not_found")
            return
        if payload:
            self._error(400, "invalid_request")
            return
        with self.server.jobs_lock:
            job = self.server.jobs.get(match[1])
        if job is None:
            if self.server.snapshot(match[1]) is None:
                self._error(404, "unknown_run")
            else:
                self._error(409, "history_read_only")
        elif job.phase not in {"queued", "running"}:
            self._respond(200, {"run_id": match[1], "status": job.phase, "cancel_requested": False})
        else:
            self.server.cancel_run(job)
            self._respond(202, {"run_id": match[1], "status": job.phase, "cancel_requested": True})


def create_server(case_paths: list[Path], db_path: Path, output_dir: Path,
                  provider_factory, host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    """Create a local workbench; use as a context manager and serve_forever().

    Case IDs come from explicitly provided fixtures, never browser paths.
    Offline callers should provide only inc001 and ``DemoProvider`` as factory.
    """
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("local workbench must bind to 127.0.0.1")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be 0..65535")
    cases = {}
    for case_path in case_paths:
        case = IncidentTools(case_path)
        if case.case_id in cases:
            raise ValueError("duplicate case ID")
        cases[case.case_id] = case
    if not cases:
        raise ValueError("at least one case is required")
    return _WorkbenchServer(("127.0.0.1", port), cases, RunStore(db_path), output_dir, provider_factory)

"""Real loopback HTTP tests; model requests remain offline and deterministic."""

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from opslens.providers import DemoProvider, ProviderError
from opslens.server import create_server


CASE = Path(__file__).resolve().parents[1] / "data" / "cases" / "inc001.json"


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.provider_factory = DemoProvider
        self.server = create_server([CASE], self.root / "checkpoints.sqlite3", self.root / "output",
                                    lambda: self.provider_factory(), port=0)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, self.config, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive(), "server failed to clean up workers")
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {}
        if body is not None:
            body = json.dumps(body)
            request_headers.update({"Origin": self.origin, "Content-Type": "application/json",
                                    "X-CSRF-Token": self.config["csrf_token"]})
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def create_run(self, **extra):
        status, result, _ = self.request("POST", "/api/runs", {"case_id": "inc001", **extra})
        self.assertEqual(status, 202, result)
        return result["run_id"]

    def wait_for(self, run_id, statuses):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            status, state, _ = self.request("GET", "/api/runs/" + run_id)
            self.assertEqual(status, 200)
            if state["status"] in statuses:
                return state
            time.sleep(0.01)
        self.fail(f"run did not reach {statuses}: {state}")

    def test_create_read_events_report_and_public_artifacts(self):
        run_id = self.create_run()
        state = self.wait_for(run_id, {"completed"})
        self.assertEqual(state["case_id"], "inc001")
        self.assertEqual(state["steps"], 4)
        self.assertEqual(state["report"]["cause_code"], "connection_pool_exhaustion")
        self.assertNotIn("messages", state)
        self.assertFalse(state["usage_complete"])
        _, listing, _ = self.request("GET", "/api/runs")
        self.assertEqual(listing["runs"][0]["run_id"], run_id)
        self.assertNotIn("observed", listing["runs"][0])
        status, first, _ = self.request("GET", f"/api/runs/{run_id}/events?after=0")
        self.assertEqual(status, 200)
        self.assertTrue(any(event["kind"] == "tool_result" for event in first["events"]))
        _, rest, _ = self.request("GET", f"/api/runs/{run_id}/events?after={first['next_cursor']}")
        self.assertEqual(rest["events"], [])
        self.assertEqual(rest["next_cursor"], first["next_cursor"])
        destination = self.root / "output" / run_id / "result.json"
        end = time.monotonic() + 2
        while not destination.exists() and time.monotonic() < end:
            time.sleep(0.01)
        artifact = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(artifact["status"], "completed")
        self.assertNotIn("messages", artifact)
        self.assertTrue(destination.with_name("trace.jsonl").exists())

    def test_case_allowlist_path_rejection_and_request_limits(self):
        status, error, _ = self.request("POST", "/api/runs", {"case_id": "../../secrets"})
        self.assertEqual((status, error["error"]["code"]), (404, "unknown_case"))
        for payload in ({"case_id": "inc001", "path": str(CASE)},
                        {"case_id": "inc001", "api_key": "browser-secret"},
                        {"case_id": "inc001", "max_steps": True},
                        {"case_id": "inc001", "max_tool_calls": 201}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/api/runs", payload)[0], 400)
        for path in ("/../../secrets", "/api/files?path=C:/secrets", "/api/runs/not-a-run", "/web/index.html"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        run_id = self.create_run()
        self.assertEqual(self.request("GET", f"/api/runs/{run_id}/events?after=-1")[0], 400)
        self.assertEqual(self.request("GET", f"/api/runs/{run_id}/events?after=1&after=2")[0], 400)
        self.assertEqual(self.request("POST", "/api/runs", {"case_id": "x" * 9000})[0], 413)

    def test_oversized_incomplete_upload_is_rejected_and_closed_promptly(self):
        # Neither a huge declaration nor a client withholding the body may
        # hold the rejection path open for the normal ten-second read timeout.
        for declared, prefix in ((2**40, b""), (9000, b"{")):
            with self.subTest(declared=declared, prefix=prefix):
                with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
                    headers = (f"POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                               f"Content-Length: {declared}\r\nContent-Type: application/json\r\n\r\n")
                    started = time.monotonic()
                    connection.sendall(headers.encode("ascii") + prefix)
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, 413)
                    self.assertEqual(response.getheader("Connection"), "close")
                    self.assertEqual(json.loads(response.read())["error"]["code"], "body_too_large")
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertEqual(connection.recv(1), b"")
                    self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.request("GET", "/api/runs")[1]["runs"], [])

    def test_host_origin_csrf_and_content_type_checks(self):
        self.assertEqual(self.request("GET", "/api/config", headers={"Host": "rebind.evil"})[0], 403)
        self.assertEqual(self.request("GET", "/api/config", headers={"Origin": "https://evil.example"})[0], 403)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": "é"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _, response_headers = self.request("POST", "/api/runs", {"case_id": "inc001"}, headers)
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        self.assertEqual(self.request("POST", "/api/runs", {"case_id": "inc001"},
                                      {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("OPTIONS", "/api/runs", headers={"Origin": "https://evil.example"})[0], 403)

    def test_worker_queue_limit_and_cooperative_cancellation(self):
        gate = threading.Event()
        self.gates.append(gate)

        class BlockingDemo(DemoProvider):
            def complete(self, messages, tools):
                gate.wait(timeout=5)
                return super().complete(messages, tools)

        self.provider_factory = BlockingDemo
        runs = [self.create_run() for _ in range(6)]
        self.wait_for(runs[0], {"running"})
        self.wait_for(runs[1], {"running"})
        status, error, _ = self.request("POST", "/api/runs", {"case_id": "inc001"})
        self.assertEqual((status, error["error"]["code"]), (429, "queue_full"))
        self.assertEqual(self.request("POST", f"/api/runs/{runs[2]}/cancel", {})[0], 202)
        queued = self.wait_for(runs[2], {"cancelled"})
        self.assertEqual(queued["steps"], 0)
        replacement = self.create_run()
        self.assertEqual(self.request("POST", f"/api/runs/{runs[0]}/cancel", {})[0], 202)
        self.assertTrue(self.request("GET", f"/api/runs/{runs[0]}")[1]["cancel_requested"])
        gate.set()
        cancelled = self.wait_for(runs[0], {"cancelled"})
        self.assertEqual(cancelled["steps"], 1)
        self.assertIsNone(cancelled["report"])
        self.wait_for(replacement, {"completed"})

    def test_provider_failure_is_explicit_and_private_protocol_fields_are_hidden(self):
        secret = "should-never-reach-browser"

        class PrivateDemo(DemoProvider):
            def complete(self, messages, tools):
                result = super().complete(messages, tools)
                result["message"]["reasoning_content"] = secret
                return result

        self.provider_factory = PrivateDemo
        run_id = self.create_run()
        state = self.wait_for(run_id, {"completed"})
        self.assertNotIn(secret, json.dumps(state))
        self.assertNotIn(secret, json.dumps(self.request("GET", f"/api/runs/{run_id}/events")[1]))

        class BrokenProvider:
            name = "failed-provider"

            def complete(self, messages, tools):
                raise ProviderError("http_401")

        self.provider_factory = BrokenProvider
        failed = self.create_run()
        state = self.wait_for(failed, {"provider_error"})
        self.assertIsNone(state["report"])
        events = self.request("GET", f"/api/runs/{failed}/events")[1]["events"]
        self.assertTrue(any(event.get("code") == "http_401" for event in events))

    def test_budget_stop_and_safe_ui_response(self):
        run_id = self.create_run(max_steps=1)
        state = self.wait_for(run_id, {"budget_exhausted"})
        self.assertEqual(state["steps"], 1)
        self.assertIsNone(state["report"])
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("调查工作台", page)
        self.assertNotIn("__CSP_NONCE__", page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)
        self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_public_binding_rejected(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_server([CASE], self.root / "other.sqlite3", self.root / "output", DemoProvider, host="0.0.0.0")

    def test_policy_defaults_override_and_auditable_fields(self):
        demo = self.create_run(max_steps=1)
        state = self.wait_for(demo, {"budget_exhausted"})
        self.assertEqual(state["policy"], "react")
        for field in ("hypotheses", "tool_seconds", "transport_attempts", "max_total_tokens",
                      "max_context_chars", "source", "definition_hash"):
            self.assertIn(field, state)

        class ModelProfile(DemoProvider):
            name = "test-model-profile"

        self.provider_factory = ModelProfile
        live = self.create_run(max_steps=1)
        self.assertEqual(self.wait_for(live, {"budget_exhausted"})["policy"], "investigator")
        explicit = self.create_run(policy="react")
        self.assertEqual(self.wait_for(explicit, {"completed"})["policy"], "react")
        self.assertEqual(self.request("POST", "/api/runs", {"case_id": "inc001", "policy": "invalid"})[0], 400)

    def test_scripted_demo_rejects_investigator_before_creating_a_run(self):
        for _ in range(7):
            status, result, _ = self.request("POST", "/api/runs",
                                             {"case_id": "inc001", "policy": "investigator"})
            self.assertEqual((status, result["error"]["code"]), (400, "unsupported_demo_policy"))
        self.assertEqual(self.request("GET", "/api/runs")[1]["runs"], [])
        # Rejections must return the admission permit, so valid work still runs.
        valid = self.create_run()
        self.assertEqual(self.wait_for(valid, {"completed"})["policy"], "react")


if __name__ == "__main__":
    unittest.main()

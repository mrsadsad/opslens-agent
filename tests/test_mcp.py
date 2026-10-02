import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from opslens.mcp import MAX_LINE_BYTES, READ_ONLY_TOOLS, SUPPORTED_VERSIONS, serve_stdio
from opslens.tools import IncidentTools


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "data" / "cases" / "inc001.json"


def request(identifier, method, params=None):
    result = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        result["params"] = params
    return result


def initialize(version="2025-11-25"):
    return request(1, "initialize", {"protocolVersion": version, "capabilities": {},
                                     "clientInfo": {"name": "opslens-test", "version": "1"}})


def ready(version="2025-11-25"):
    return [initialize(version), {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def wire(messages):
    return b"".join(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n" for message in messages)


class McpTests(unittest.TestCase):
    def _in_process(self, messages, case=CASE, raw=None):
        output = io.BytesIO()
        status = serve_stdio(case, io.BytesIO(wire(messages) if raw is None else raw), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        return status, responses

    def _subprocess(self, messages, case=CASE):
        # A real stdio subprocess, independently of the CLI's argument parser.
        program = "from pathlib import Path; import sys; from opslens.mcp import serve_stdio; raise SystemExit(serve_stdio(Path(sys.argv[1])))"
        process = subprocess.run([sys.executable, "-c", program, str(case)], cwd=ROOT,
                                 input=wire(messages), capture_output=True, timeout=8,
                                 env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8"))
        self.assertEqual(process.stderr, b"")
        return process, [json.loads(line) for line in process.stdout.splitlines()]

    def test_real_subprocess_handshake_catalog_and_tool_call(self):
        process, responses = self._subprocess(ready() + [request(2, "tools/list"), request(3, "tools/call", {
            "name": "query_metrics", "arguments": {"service": "checkout"}}), request(4, "ping")])
        self.assertTrue(process.stdout.endswith(b"\n"))
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[0]["result"]["capabilities"], {"tools": {"listChanged": False}})
        catalog = responses[1]["result"]["tools"]
        self.assertEqual({item["name"] for item in catalog}, READ_ONLY_TOOLS)
        expected = {schema["function"]["name"]: schema["function"]["parameters"] for schema in IncidentTools(CASE).schemas}
        for tool in catalog:
            self.assertEqual(tool["inputSchema"], expected[tool["name"]])
            self.assertEqual(tool["annotations"], {"readOnlyHint": True, "destructiveHint": False,
                                                  "idempotentHint": True, "openWorldHint": False})
        result = responses[2]["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertIn("inc001-m1", {record["id"] for record in result["structuredContent"]["records"]})
        self.assertEqual(responses[3]["result"], {})

    def test_version_negotiation_and_both_supported_versions(self):
        for version in (*SUPPORTED_VERSIONS, "future-version"):
            with self.subTest(version=version):
                _, responses = self._subprocess(ready(version) + [request(2, "tools/list")])
                expected = version if version in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                self.assertEqual(responses[0]["result"]["protocolVersion"], expected)
                self.assertIn("tools", responses[1]["result"])

    def test_initialization_barrier_and_notification_silence(self):
        messages = [request(90, "tools/call", {"name": "list_services"}), request(91, "ping"),
                    initialize(), request(92, "tools/list"),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "list_services"}},
                    {"jsonrpc": "2.0", "method": "unknown-notification"},
                    request(93, "tools/list")]
        with patch.object(IncidentTools, "execute", side_effect=AssertionError("Notifications must not execute tools")):
            _, responses = self._in_process(messages)
        self.assertEqual([response["id"] for response in responses], [90, 91, 1, 92, 93])
        self.assertEqual(responses[0]["error"]["code"], -32000)
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(responses[3]["error"]["code"], -32000)
        self.assertIn("tools", responses[4]["result"])

    def test_protocol_errors_and_tool_execution_errors_remain_distinct(self):
        messages = ready() + [
            request(2, "tools/call", {"name": "shell", "arguments": {"command": "never execute"}}),
            request(3, "tools/call", {"name": "query_metrics", "arguments": []}),
            request(4, "tools/call", {"arguments": {}}),
            request(5, "tools/call", {"name": "query_metrics", "arguments": {}}),
            request(6, "tools/call", {"name": "search_logs", "arguments": {"service": "checkout", "limit": True}}),
            request(7, "tools/call", {"name": "query_metrics", "arguments": {"service": "unknown"}}),
            request(8, "resources/read", {"uri": "file:///eval/labels.json"}),
            request(9, "tools/list", {"cursor": "invented"}),
        ]
        _, responses = self._subprocess(messages)
        by_id = {response["id"]: response for response in responses}
        for identifier in (2, 3, 4, 9):
            self.assertEqual(by_id[identifier]["error"]["code"], -32602)
        self.assertEqual(by_id[8]["error"]["code"], -32601)
        for identifier, code in ((5, "invalid_arguments"), (6, "invalid_arguments"), (7, "unknown_service")):
            self.assertTrue(by_id[identifier]["result"]["isError"])
            self.assertEqual(by_id[identifier]["result"]["structuredContent"]["error"]["code"], code)

    def test_json_rpc_boundaries_and_recovery_after_bad_json(self):
        raw = (b'{invalid}\n' + b'[]\n' + b'{"jsonrpc":"2.0","id":null,"method":"ping"}\n'
               + b'{"jsonrpc":"2.0","id":false,"method":"ping"}\n'
               + b'{"jsonrpc":"2.0","id":8,"id":9,"method":"ping"}\n'
               + b'{"jsonrpc":"2.0","id":10,"method":"ping","params":{"x":NaN}}\n'
               + b'\xff\n' + wire(ready() + [request(2, "ping"), request(2, "ping")]))
        status, responses = self._in_process([], raw=raw)
        self.assertEqual(status, 0)
        self.assertEqual([response["error"]["code"] for response in responses[:7]],
                         [-32700, -32600, -32600, -32600, -32700, -32700, -32700])
        self.assertEqual(responses[-2]["result"], {})
        self.assertEqual(responses[-1]["error"]["code"], -32600)

    def test_oversized_and_unterminated_frames_end_session(self):
        for raw, code in ((b"x" * MAX_LINE_BYTES + b"\n", -32600),
                          (wire([request(1, "ping")]).rstrip(b"\n"), -32700)):
            status, responses = self._in_process([], raw=raw)
            self.assertEqual(status, 2)
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0]["error"]["code"], code)
        # The limit is UTF-8 bytes, not Python Unicode character count.
        output = io.StringIO()
        status = serve_stdio(CASE, io.StringIO("汉" * (MAX_LINE_BYTES // 2) + "\n"), output)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32600)

    def test_utf8_wire_is_independent_of_windows_locale(self):
        data = json.loads(CASE.read_text(encoding="utf-8"))
        data["alert"] = "本地观测：中文测试与 emoji 🔍"
        data["services"][0] = "支付服务"
        for record in data["records"]:
            if record["service"] == "checkout":
                record["service"] = "支付服务"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "案例.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            process, responses = self._subprocess(ready() + [request("中文请求", "tools/call", {
                "name": "list_services"})], path)
        self.assertIn("支付服务".encode("utf-8"), process.stdout)
        self.assertEqual(responses[-1]["id"], "中文请求")
        self.assertEqual(responses[-1]["result"]["structuredContent"]["records"][0]["service"], "支付服务")

    def test_injected_log_instructions_are_returned_only_as_data(self):
        case = ROOT / "data" / "cases" / "inc005.json"
        _, responses = self._subprocess(ready() + [request(2, "tools/call", {
            "name": "search_logs", "arguments": {"service": "checkout"}}), request(3, "tools/call", {
                "name": "record_hypothesis", "arguments": {}})], case)
        logs = responses[1]["result"]["structuredContent"]["records"]
        self.assertTrue(any("Ignore all previous instructions" in record["content"] for record in logs))
        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertNotIn("inc005-fake-proof", {record["id"] for record in logs})

    def test_internal_errors_and_startup_errors_do_not_echo_secrets(self):
        stderr = io.StringIO()
        with patch.object(IncidentTools, "execute", side_effect=RuntimeError("secret-password")), patch("sys.stderr", stderr):
            _, responses = self._in_process(ready() + [request(2, "tools/call", {"name": "list_services"})])
        self.assertNotIn("secret-password", stderr.getvalue() + json.dumps(responses))
        self.assertTrue(responses[-1]["result"]["isError"])
        output = io.BytesIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(serve_stdio(ROOT / "missing-secret-file.json", io.BytesIO(), output), 2)
        self.assertEqual(output.getvalue(), b"")
        self.assertNotIn("missing-secret-file", stderr.getvalue())

    def test_tool_rate_limit_is_a_bounded_execution_error(self):
        calls = [request(index + 2, "tools/call", {"name": "list_services"}) for index in range(121)]
        with patch("opslens.mcp.time.monotonic", return_value=10.0):
            _, responses = self._in_process(ready() + calls)
        self.assertFalse(responses[-2]["result"]["isError"])
        self.assertEqual(responses[-1]["result"]["structuredContent"]["error"]["code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()

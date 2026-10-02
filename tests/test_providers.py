"""Provider contract tests use no real network or model credentials."""

from __future__ import annotations

import copy
import email.message
import http.client
import io
import json
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest.mock import Mock, patch

from opslens.providers import (
    MAX_RESPONSE_BYTES,
    ChatProvider,
    DeepSeekProvider,
    DemoProvider,
    ProviderError,
    _NoRedirectHandler,
)


SECRET = "test-secret-never-display"


def response_payload():
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "query_metrics",
                                "arguments": '{"service":"checkout"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


def http_response(payload=None, raw=None):
    if raw is None:
        raw = json.dumps(response_payload() if payload is None else payload).encode()
    return io.BytesIO(raw)


class ChatProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = ChatProvider("https://example.test/v1/", "configured-model", SECRET)
        self.provider._opener = Mock()
        self.provider._opener.open.return_value = http_response()
        sleep_patch = patch("opslens.providers.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_native_tool_calling_request_and_result(self):
        messages = [{"role": "user", "content": "排查 checkout"}]
        tools = [{"type": "function", "function": {"name": "query_metrics"}}]
        result = self.provider.complete(messages, tools)
        request = self.provider._opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(body["model"], "configured-model")
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "required")
        self.assertIs(body["parallel_tool_calls"], False)
        self.assertNotIn("temperature", body)
        self.assertNotIn("thinking", body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(self.provider._opener.open.call_args.kwargs, {"timeout": 30})
        self.assertEqual(result["message"]["tool_calls"][0]["function"]["name"], "query_metrics")
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertIn("configured-model", self.provider.name)
        self.assertIn("https://example.test/v1", self.provider.name)
        self.assertNotIn(SECRET, self.provider.name)
        self.assertNotIn(SECRET, repr(self.provider))

    def test_preserves_assistant_protocol_fields_for_next_turn(self):
        payload = response_payload()
        payload["choices"][0]["message"]["reasoning_content"] = "provider-private-field"
        self.provider._opener.open.return_value = http_response(payload)
        result = self.provider.complete([], [])
        self.assertEqual(result["message"]["reasoning_content"], "provider-private-field")
        self.provider._opener.open.return_value = http_response()
        self.provider.complete([result["message"]], [])
        request = self.provider._opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["messages"][0], result["message"])

    def test_unknown_usage_is_never_fabricated_as_zero(self):
        for usage in (None, {}, {"prompt_tokens": None}, {"completion_tokens": 7}):
            with self.subTest(usage=usage):
                payload = response_payload()
                payload["usage"] = usage
                self.provider._opener.open.return_value = http_response(payload)
                self.assertEqual(self.provider.complete([], [])["usage"], usage or {})
        payload.pop("usage")
        self.provider._opener.open.return_value = http_response(payload)
        self.assertEqual(self.provider.complete([], [])["usage"], {})

    def test_invalid_response_shapes_and_usage_are_rejected_without_retry(self):
        invalid_payloads = [None, [], {}, {"choices": []}, {"choices": [None]}]
        for field, value in (("role", "user"), ("content", {}), ("tool_calls", [])):
            payload = response_payload()
            payload["choices"][0]["message"][field] = value
            invalid_payloads.append(payload)
        for usage in ([], {"total_tokens": -1}, {"prompt_tokens": True}, {"total_tokens": "30"}):
            payload = response_payload()
            payload["usage"] = usage
            invalid_payloads.append(payload)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.provider._opener.open.reset_mock()
                self.provider._opener.open.return_value = http_response(raw=json.dumps(payload).encode())
                with self.assertRaises(ProviderError):
                    self.provider.complete([], [])
                self.assertEqual(self.provider._opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_invalid_tool_calls_are_rejected(self):
        invalid_calls = [
            {},
            {"id": "", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c", "type": "other", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c", "type": "function", "function": {"name": "a", "arguments": {}}},
            {"id": "c", "type": "function", "function": {"name": "bad name", "arguments": "{}"}},
        ]
        for call in invalid_calls:
            with self.subTest(call=call):
                payload = response_payload()
                payload["choices"][0]["message"]["tool_calls"] = [call]
                self.provider._opener.open.return_value = http_response(payload)
                with self.assertRaises(ProviderError):
                    self.provider.complete([], [])
        payload = response_payload()
        payload["choices"][0]["message"]["tool_calls"] *= 2
        self.provider._opener.open.return_value = http_response(payload)
        with self.assertRaisesRegex(ProviderError, "^invalid_tool_call$"):
            self.provider.complete([], [])

    def test_malformed_arguments_preserve_usage_for_runtime_repair(self):
        for arguments in ("bad JSON", "[]", "null", "", '{"service":12}', '{"service":'):
            with self.subTest(arguments=arguments):
                payload = response_payload()
                payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
                self.provider._opener.open.return_value = http_response(payload)
                result = self.provider.complete([], [])
                self.assertEqual(result["message"]["tool_calls"][0]["function"]["arguments"], arguments)
                self.assertEqual(result["usage"], payload["usage"])
        self.sleep.assert_not_called()

    def test_retries_rate_limits_and_server_errors(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                self.provider._opener.open.reset_mock()
                error = urllib.error.HTTPError("https://example.test", status, SECRET, {}, io.BytesIO(SECRET.encode()))
                self.provider._opener.open.side_effect = [error, http_response()]
                self.assertEqual(self.provider.complete([], [])["usage"]["total_tokens"], 30)
                self.assertEqual(self.provider._opener.open.call_count, 2)

    def test_client_errors_are_not_retried_and_do_not_reveal_body_or_secret(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.provider._opener.open.reset_mock()
                body = io.BytesIO((SECRET + " private provider response").encode())
                self.provider._opener.open.side_effect = urllib.error.HTTPError(
                    "https://example.test/" + SECRET, status, SECRET, {}, body
                )
                with self.assertRaises(ProviderError) as caught:
                    self.provider.complete([], [])
                self.assertEqual(str(caught.exception), f"http_{status}")
                self.assertNotIn(SECRET, repr(caught.exception))
                self.assertTrue(body.closed)
                self.assertEqual(self.provider._opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_connection_failures_and_timeouts_have_bounded_retries(self):
        errors = [
            (urllib.error.URLError(SECRET), "network_error"),
            (urllib.error.URLError(TimeoutError(SECRET)), "provider_timeout"),
            (ConnectionResetError(SECRET), "network_error"),
            (TimeoutError(SECRET), "provider_timeout"),
            (http.client.IncompleteRead(SECRET.encode()), "network_error"),
        ]
        for error, expected_code in errors:
            with self.subTest(error=type(error)):
                self.provider._opener.open.reset_mock()
                self.provider._opener.open.side_effect = error
                with self.assertRaises(ProviderError) as caught:
                    self.provider.complete([], [])
                self.assertEqual(str(caught.exception), expected_code)
                self.assertNotIn(SECRET, repr(caught.exception))
                self.assertEqual(self.provider._opener.open.call_count, 2)

    def test_response_size_and_malformed_json(self):
        for raw, code in ((b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
                          (SECRET.encode(), "invalid_json"), (b"\xff", "invalid_json")):
            with self.subTest(code=code):
                self.provider._opener.open.return_value = http_response(raw=raw)
                with self.assertRaises(ProviderError) as caught:
                    self.provider.complete([], [])
                self.assertEqual(str(caught.exception), code)
        self.sleep.assert_not_called()

    def test_invalid_request_is_safe_and_not_sent(self):
        with self.assertRaisesRegex(ProviderError, "^invalid_request$"):
            self.provider.complete([{"content": {SECRET}}], [])
        self.provider._opener.open.assert_not_called()

    def test_configuration_validation(self):
        for url in ("http://public.example/v1", "http://localhost.evil/v1", "file:///secret", "https://u:p@example.test", "https://example.test?key=" + SECRET, "https://example.test/#" + SECRET, "https://example.test:99999", "https://exa mple.test", ""):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ProviderError, "^invalid_configuration$"):
                    ChatProvider(url, "model", SECRET)
        for url in ("http://localhost:8080/v1", "http://127.0.0.1:8000", "http://[::1]:8000", "https://example.test/v1/"):
            with self.subTest(url=url):
                provider = ChatProvider(url, "model", SECRET)
                self.assertEqual(provider.base_url, url.rstrip("/"))
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"max_attempts": 0}, {"max_attempts": 6}, {"max_attempts": True}, {"api_key": "bad\r\nkey"}):
            with self.subTest(kwargs=kwargs):
                config = {"base_url": "https://example.test", "model": "model", "api_key": SECRET}
                config.update(kwargs)
                with self.assertRaisesRegex(ProviderError, "^invalid_configuration$"):
                    ChatProvider(**config)

    def test_redirect_is_blocked_before_authorization_can_be_forwarded(self):
        recorded_requests = []

        class RedirectingHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, request):
                recorded_requests.append(request)
                headers = email.message.Message()
                headers["Location"] = "http://127.0.0.1:65534/credential-trap"
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                return response

        provider = ChatProvider("http://localhost:8080/v1", "model", SECRET)
        provider._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RedirectingHTTPHandler(), _NoRedirectHandler()
        )
        with self.assertRaisesRegex(ProviderError, "^redirect_blocked$"):
            provider.complete([], [])
        self.assertEqual(len(recorded_requests), 1)
        self.assertEqual(recorded_requests[0].full_url, "http://localhost:8080/v1/chat/completions")
        self.sleep.assert_not_called()


class DeepSeekProviderTests(unittest.TestCase):
    def test_non_thinking_tool_call_profile_and_endpoint(self):
        provider = DeepSeekProvider(SECRET)
        provider._opener = Mock()
        provider._opener.open.return_value = http_response()
        messages = [{"role": "user", "content": "排查 checkout"}]
        tools = [{"type": "function", "function": {"name": "query_metrics"}}]
        result = provider.complete(messages, tools)
        request = provider._opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["tools"], tools)
        self.assertNotIn("parallel_tool_calls", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(provider._opener.open.call_args.kwargs, {"timeout": 60})
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertIn("thinking=disabled", provider.name)
        self.assertIn("max_tokens=4096", provider.name)
        self.assertIn("deepseek-flash", provider.name)
        self.assertIn("https://api.deepseek.com", provider.name)
        self.assertNotIn(SECRET, provider.name)
        generic = ChatProvider("https://api.deepseek.com", "deepseek-flash", SECRET)
        self.assertNotEqual(provider.name, generic.name)

    def test_custom_model_timeout_and_multiple_tool_calls(self):
        provider = DeepSeekProvider(SECRET, model="custom-model-name", timeout=15, max_attempts=1)
        provider._opener = Mock()
        payload = response_payload()
        second_call = copy.deepcopy(payload["choices"][0]["message"]["tool_calls"][0])
        second_call["id"] = "call_2"
        second_call["function"]["name"] = "search_logs"
        payload["choices"][0]["message"]["tool_calls"].append(second_call)
        provider._opener.open.return_value = http_response(payload)
        result = provider.complete([], [])
        request = provider._opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "custom-model-name")
        self.assertEqual(provider._opener.open.call_args.kwargs, {"timeout": 15})
        self.assertEqual(len(result["message"]["tool_calls"]), 2)
        self.assertIn("custom-model-name", provider.name)
        self.assertEqual(provider.max_attempts, 1)

    def test_inherits_safe_http_failure_handling(self):
        provider = DeepSeekProvider(SECRET)
        provider._opener = Mock()
        provider._opener.open.side_effect = urllib.error.HTTPError(
            "https://api.deepseek.com", 400, SECRET, {}, io.BytesIO(SECRET.encode())
        )
        with self.assertRaises(ProviderError) as caught:
            provider.complete([], [])
        self.assertEqual(str(caught.exception), "http_400")
        self.assertNotIn(SECRET, repr(caught.exception))
        self.assertEqual(provider._opener.open.call_count, 1)


class DemoProviderTests(unittest.TestCase):
    def test_fixed_demo_sequence_and_diagnosis(self):
        provider = DemoProvider()
        self.assertEqual(provider.name, "scripted-demo")
        messages = [{"role": "system", "content": "demo"}, {"role": "user", "content": "inc001"}]
        expected_names = ["query_metrics", "search_logs", "get_changes", "submit_diagnosis"]
        for index, expected_name in enumerate(expected_names):
            original = copy.deepcopy(messages)
            result = provider.complete(messages, [])
            self.assertEqual(messages, original)
            self.assertEqual(result["usage"], {})
            message = result["message"]
            call = message["tool_calls"][0]
            self.assertEqual(call["id"], f"demo-call-{index + 1}")
            self.assertEqual(call["function"]["name"], expected_name)
            arguments = json.loads(call["function"]["arguments"])
            if index < 3:
                self.assertEqual(arguments, {"service": "checkout"})
            else:
                self.assertEqual(arguments["status"], "diagnosed")
                self.assertEqual(arguments["cause_code"], "connection_pool_exhaustion")
                self.assertEqual(arguments["evidence_ids"], ["inc001-m1", "inc001-l1", "inc001-c1"])
                self.assertIn("100", arguments["root_cause"])
                self.assertIn("20", arguments["root_cause"])
                self.assertIn("人工", arguments["next_steps"][0])
            messages.extend([message, {"role": "tool", "tool_call_id": call["id"], "content": "{}"}])
        with self.assertRaisesRegex(ProviderError, "^demo_exhausted$"):
            provider.complete(messages, [])


if __name__ == "__main__":
    unittest.main()

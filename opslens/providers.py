"""Small Chat Completions adapter and an explicitly scripted, offline demo.

The adapter keeps provider credentials and response bodies out of errors. It
does not expose model reasoning: optional protocol fields are returned only so
the runtime can send the assistant message back in the next model request.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .json_utils import strict_loads


MAX_RESPONSE_BYTES = 1_048_576
MAX_ATTEMPTS = 5
_FUNCTION_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


class ProviderError(RuntimeError):
    """Provider failure with a safe code suitable for display or persistence."""

    def __init__(self, code: str, transport_attempts: int | None = None):
        self.code = code
        self.transport_attempts = transport_attempts
        super().__init__(code)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never send a bearer credential to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _normalize_base_url(base_url: str) -> str:
    try:
        if not isinstance(base_url, str) or any(c.isspace() or ord(c) == 127 for c in base_url.strip()):
            raise ValueError
        parts = urllib.parse.urlsplit(base_url.strip())
        hostname = parts.hostname
        if (
            not hostname
            or parts.scheme not in {"http", "https"}
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        if parts.scheme == "http" and hostname.lower() != "localhost":
            try:
                if not ipaddress.ip_address(hostname).is_loopback:
                    raise ValueError
            except ValueError:
                raise ValueError from None
        # Accessing .port also rejects malformed and out-of-range port values.
        port = parts.port
        authority = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
        if port is not None:
            authority += f":{port}"
        return urllib.parse.urlunsplit(
            (parts.scheme, authority, parts.path.rstrip("/"), "", "")
        )
    except (ValueError, TypeError, AttributeError):
        raise ProviderError("invalid_configuration") from None


def _validate_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("invalid_response")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderError("invalid_response")
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise ProviderError("invalid_response")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ProviderError("missing_tool_calls")
    call_ids: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderError("invalid_tool_call")
        call_id = call.get("id")
        function = call.get("function")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or call_id in call_ids
            or not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not _FUNCTION_NAME.fullmatch(function["name"])
            or not isinstance(function.get("arguments"), str)
        ):
            raise ProviderError("invalid_tool_call")
        call_ids.add(call_id)
        # Arguments may contain invalid JSON or violate the tool schema. Keep
        # them and usage intact so the runtime can send a structured tool error
        # back to the model, allowing it to repair its next call.
    raw_usage = payload.get("usage")
    if raw_usage is not None and not isinstance(raw_usage, dict):
        raise ProviderError("invalid_usage")
    usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if raw_usage is not None and key in raw_usage:
            value = raw_usage[key]
            if value is not None and (type(value) is not int or value < 0):
                raise ProviderError("invalid_usage")
            usage[key] = value
    # Preserve fields such as reasoning_content for compatible providers'
    # subsequent turns; the presentation layer must not render these fields.
    assistant_message = dict(message)
    assistant_message.setdefault("content", None)
    return {"message": assistant_message, "usage": usage}


class ChatProvider:
    """Call a user-configured endpoint using native function tool calling.

    ``base_url`` is the API prefix (for example, ``https://host.example/v1``).
    HTTPS is mandatory except on localhost or a loopback IP. Retries are bounded
    to 1–5 attempts; timeout applies to transport operations, not the whole run.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 30,
        max_attempts: int = 2,
    ):
        self.base_url = _normalize_base_url(base_url)
        if (
            not isinstance(model, str)
            or not model.strip()
            or any(ord(c) < 32 for c in model)
            or not isinstance(api_key, str)
            or not api_key.strip()
            or any(ord(c) < 33 or ord(c) > 126 for c in api_key)
            or not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise ProviderError("invalid_configuration")
        self.model = model.strip()
        self._api_key = api_key
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.name = f"chat-completions:{self.model}@{self.base_url}"
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _build_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "required",
            "parallel_tool_calls": False,
        }

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        try:
            data = json.dumps(
                self._build_payload(messages, tools),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ProviderError("invalid_request") from None
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=data,
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        for attempt in range(self.max_attempts):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProviderError("response_too_large")
                try:
                    payload = strict_loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise ProviderError("invalid_json") from None
                validated = _validate_response(payload)
                validated["transport_attempts"] = attempt + 1
                # A network or server retry can have a billable predecessor whose
                # usage we never received. Never label the known sum as complete.
                validated["usage_complete"] = attempt == 0
                return validated
            except ProviderError as error:
                error.transport_attempts = attempt + 1
                raise
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if 300 <= status < 400:
                    raise ProviderError("redirect_blocked", transport_attempts=attempt + 1) from None
                code = f"http_{status}"
                retryable = status == 429 or 500 <= status <= 599
            except urllib.error.URLError as exc:
                code = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "network_error"
                retryable = True
            except TimeoutError:
                code, retryable = "provider_timeout", True
            except OSError:
                code, retryable = "network_error", True
            except http.client.HTTPException:
                code, retryable = "network_error", True
            except (ValueError, UnicodeError):
                raise ProviderError("invalid_request", transport_attempts=attempt + 1) from None
            if not retryable or attempt + 1 == self.max_attempts:
                raise ProviderError(code, transport_attempts=attempt + 1) from None
            time.sleep(min(0.25 * 2**attempt, 2.0))
        raise ProviderError("network_error")  # Defensive: attempts is at least 1.


class DeepSeekProvider(ChatProvider):
    """DeepSeek profile for required tools in non-thinking mode.

    The default profile limits each completion to 4096 tokens. The official
    Chat Completions API requires thinking to be disabled for ``required``
    tool choice and does not document ``parallel_tool_calls``. Multiple tool
    calls in a response are left to the runtime to execute and validate.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        timeout: float = 60,
        max_attempts: int = 2,
    ):
        super().__init__("https://api.deepseek.com", model, api_key, timeout, max_attempts)
        self.name = (
            f"deepseek:{self.model}@{self.base_url}"
            ";thinking=disabled;max_tokens=4096"
        )

    def _build_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = super()._build_payload(messages, tools)
        del payload["parallel_tool_calls"]
        payload["thinking"] = {"type": "disabled"}
        payload["max_tokens"] = 4096
        return payload


class DemoProvider:
    """Fixed inc001 walkthrough; this is not an AI model or an evaluation."""

    name = "scripted-demo"

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        index = sum(message.get("role") == "assistant" for message in messages)
        script = [
            ("query_metrics", {"service": "checkout"}),
            ("search_logs", {"service": "checkout"}),
            ("get_changes", {"service": "checkout"}),
            (
                "submit_diagnosis",
                {
                    "status": "diagnosed",
                    "cause_code": "connection_pool_exhaustion",
                    "root_service": "checkout",
                    "root_cause": "checkout 发布将连接池上限从 100 降至 20，引发连接获取超时。",
                    "evidence_ids": ["inc001-m1", "inc001-l1", "inc001-c1"],
                    "explanation": "配置变化与连接池饱和、获取连接超时相互印证；需继续核验数据库侧负载。",
                    "next_steps": ["核对发布配置，人工确认恢复连接池上限后观察等待时间。"],
                },
            ),
        ]
        if index >= len(script):
            raise ProviderError("demo_exhausted")
        name, arguments = script[index]
        return {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"demo-call-{index + 1}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            },
            "usage": {},
            "transport_attempts": 0,
            "usage_complete": False,
        }

"""Explicit, bounded JSON Chat Completions transport using the standard library.

Construction never opens a socket. ``complete`` or ``self_test`` is an explicit
request. Offline mode permits only literal loopback/localhost endpoints, disables
environment proxies and refuses every redirect. Remote use needs both explicit
network permission and TLS. Credentials are read from a named environment entry
at request time and never included in configuration, return data or errors.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .agent import AgentError, ModelReply, ToolCall, ToolSchema


class ProviderError(AgentError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _positive_integer(value: Any, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ProviderError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ModelProfile:
    model: str
    context_window: int = 32_768
    max_output_tokens: int = 2048
    temperature: float | None = 0.0
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = False
    token_parameter: str = "max_completion_tokens"
    system_role: str = "system"

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 256 or any(ord(c) < 32 for c in self.model):
            raise ProviderError("model must be an explicit nonempty bounded identifier")
        _positive_integer(self.context_window, "context_window")
        _positive_integer(self.max_output_tokens, "max_output_tokens")
        if self.max_output_tokens >= self.context_window:
            raise ProviderError("max_output_tokens must be smaller than context_window")
        if self.temperature is not None and (not _finite(self.temperature) or not 0 <= self.temperature <= 2):
            raise ProviderError("temperature must be null or a finite number between 0 and 2")
        if any(type(value) is not bool for value in (self.supports_tools, self.supports_parallel_tool_calls)):
            raise ProviderError("Model capabilities must be booleans")
        if self.supports_parallel_tool_calls and not self.supports_tools:
            raise ProviderError("Parallel tool calling requires tool support")
        if self.token_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ProviderError("Unsupported completion-token parameter")
        if self.system_role not in ("system", "developer"):
            raise ProviderError("system_role must be system or developer")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelProfile":
        if type(data) is not dict or set(data) - set(cls.__dataclass_fields__) or "model" not in data:
            raise ProviderError("Model profile requires model and known configuration fields only")
        return cls(**data)


def _endpoint(url: str, *, offline: bool, allow_network: bool) -> tuple[str, bool]:
    if not isinstance(url, str) or not url or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ProviderError("Endpoint must be a bounded URL without whitespace/control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        raise ProviderError("Invalid endpoint URL") from None
    if parsed.scheme not in ("http", "https") or not hostname:
        raise ProviderError("Endpoint requires an http or https scheme and hostname")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ProviderError("Credentials, queries and fragments are forbidden in endpoint URLs")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderError("Invalid endpoint port")
    # Strict path syntax avoids encoded userinfo, queries and ambiguous URL
    # normalization. Tokens belong in the environment, never a URL component.
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", parsed.path) or re.search(r"(?:api[_-]?key|password|secret|sk-)", parsed.path, re.I):
        raise ProviderError("Endpoint path must be a plain API base path without credential material")
    if hostname.casefold() == "localhost":
        hostname = "127.0.0.1"  # No DNS lookup or hosts-file override in offline mode.
        local = True
    else:
        try:
            local = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = False
    if not local:
        if offline or not allow_network:
            raise ProviderError("Remote endpoints require offline=False and allow_network=True")
        if parsed.scheme != "https":
            raise ProviderError("TLS is mandatory for remote endpoints")
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority += f":{port}"
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, authority, path, "", "")), not local


@dataclass(frozen=True)
class ProviderConfig:
    profile: ModelProfile
    endpoint: str = "http://127.0.0.1:8000/v1"
    api_key_env: str | None = None
    offline: bool = True
    allow_network: bool = False
    timeout_s: float = 30.0
    max_response_bytes: int = 1_048_576
    max_requests: int = 32
    max_total_tokens: int = 256_000

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ModelProfile):
            raise ProviderError("profile must be a ModelProfile")
        if type(self.offline) is not bool or type(self.allow_network) is not bool:
            raise ProviderError("Network permissions must be booleans")
        _endpoint(self.endpoint, offline=self.offline, allow_network=self.allow_network)
        if self.api_key_env is not None and (not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env)):
            raise ProviderError("api_key_env must be an environment-variable name, never a literal key")
        if not _finite(self.timeout_s) or not 0 < self.timeout_s <= 120:
            raise ProviderError("timeout_s must be positive and at most 120 seconds")
        for name in ("max_response_bytes", "max_requests", "max_total_tokens"):
            _positive_integer(getattr(self, name), name)
        if self.max_response_bytes > 16 * 1024 * 1024:
            raise ProviderError("Response limit cannot exceed 16 MiB")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderConfig":
        if type(data) is not dict or set(data) - set(cls.__dataclass_fields__) or "profile" not in data:
            raise ProviderError("Provider configuration requires profile and known fields only")
        return cls(**{**data, "profile": ModelProfile.from_dict(data["profile"])})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("Duplicate JSON response/argument key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ProviderError("Nonfinite JSON response/argument value")


def _parse(text: str | bytes) -> Any:
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        result = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
        _json(result).encode("utf-8")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ProviderError("Response or tool arguments are not valid finite UTF-8 JSON") from None


def _schema(value: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, depth: int = 0) -> None:
    """Validate the conservative JSON Schema subset used by harness tools."""
    if depth > 64 or not isinstance(schema, dict):
        raise ProviderError("Tool schema or argument nesting is invalid")
    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise ProviderError("Only local tool-schema references are supported")
        target = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
        if target is None:
            raise ProviderError("Unresolved tool-schema reference")
        return _schema(value, target, root, depth + 1)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            successes = 0
            for candidate in schema[key]:
                try:
                    _schema(value, candidate, root, depth + 1)
                    successes += 1
                except ProviderError:
                    pass
            if (key == "oneOf" and successes != 1) or (key == "anyOf" and successes < 1):
                raise ProviderError("Tool argument does not match its alternative schemas")
    kind = schema.get("type")
    types = kind if isinstance(kind, list) else [kind] if kind else []
    predicates = {"object": lambda: type(value) is dict, "array": lambda: type(value) is list,
                  "string": lambda: type(value) is str, "number": lambda: _finite(value),
                  "integer": lambda: type(value) is int, "boolean": lambda: type(value) is bool,
                  "null": lambda: value is None}
    if types and not any(name in predicates and predicates[name]() for name in types):
        raise ProviderError("Tool argument has an incorrect scalar/container type")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise ProviderError("Tool argument violates a constant")
    if "enum" in schema and not any(value == candidate and type(value) is type(candidate) for candidate in schema["enum"]):
        raise ProviderError("Tool argument is outside its enum")
    if isinstance(value, dict):
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})
        if required - set(value):
            raise ProviderError("Tool arguments omit required fields")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                _schema(item, properties[name], root, depth + 1)
            elif extra is False:
                raise ProviderError("Tool arguments contain unknown fields")
            elif isinstance(extra, dict):
                _schema(item, extra, root, depth + 1)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1_000_000):
            raise ProviderError("Tool array length is outside its schema")
        for item in value:
            if "items" in schema:
                _schema(item, schema["items"], root, depth + 1)
    if isinstance(value, str) and (len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000)):
        raise ProviderError("Tool string length is outside its schema")
    if _finite(value):
        if "minimum" in schema and value < schema["minimum"] or "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ProviderError("Tool numeric value is below its schema limit")


def _calls(calls: Any, tools: dict[str, ToolSchema], *, parallel: bool) -> tuple[ToolCall, ...]:
    if calls is None:
        return ()
    if not isinstance(calls, list) or len(calls) > (32 if parallel else 1):
        raise ProviderError("Invalid or unsupported number of returned tool calls")
    result, identifiers = [], set()
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"id", "type", "function"} or call["type"] != "function":
            raise ProviderError("Returned tool call is not a typed function call")
        identifier, function = call["id"], call["function"]
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128 or identifier in identifiers:
            raise ProviderError("Returned tool-call identifier is invalid or duplicated")
        identifiers.add(identifier)
        if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
            raise ProviderError("Returned function fields are invalid")
        name, arguments = function["name"], function["arguments"]
        if not isinstance(name, str) or name not in tools or not isinstance(arguments, str):
            raise ProviderError("Returned function is not an allowed tool with JSON-string arguments")
        arguments = _parse(arguments)
        if not isinstance(arguments, dict):
            raise ProviderError("Tool arguments must be an object")
        _schema(arguments, tools[name].parameters)
        result.append(ToolCall(identifier, name, arguments))
    return tuple(result)


def _wire_messages(messages: tuple[dict[str, Any], ...], system_role: str) -> list[dict[str, Any]]:
    result = []
    if not isinstance(messages, (tuple, list)) or not messages:
        raise ProviderError("A request requires nonempty typed messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ProviderError("Unsupported message role")
        role = message["role"]
        allowed = {"role", "content", "tool_calls"} if role == "assistant" else {"role", "content", "tool_call_id", "name"} if role == "tool" else {"role", "content"}
        if set(message) - allowed or not isinstance(message.get("content"), str):
            raise ProviderError("Only bounded text messages with known fields are supported")
        wire = {"role": system_role if role == "system" else role, "content": message["content"]}
        if role == "tool":
            if not isinstance(message.get("tool_call_id"), str) or not message["tool_call_id"]:
                raise ProviderError("Tool observations require their tool_call_id")
            wire["tool_call_id"] = message["tool_call_id"]
        if role == "assistant" and message.get("tool_calls"):
            converted = []
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
                    raise ProviderError("Invalid assistant tool-call history")
                arguments = call["arguments"] if isinstance(call["arguments"], str) else _json(call["arguments"])
                converted.append({"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": arguments}})
            wire["tool_calls"] = converted
        result.append(wire)
    return result


class ChatCompletionsProvider:
    def __init__(self, config: ProviderConfig):
        if not isinstance(config, ProviderConfig):
            raise ProviderError("config must be a ProviderConfig")
        self.config = config
        self._url, self.network_enabled = _endpoint(config.endpoint, offline=config.offline, allow_network=config.allow_network)
        self.requests_used = 0
        self.budget_tokens_used = 0
        # ProxyHandler({}) intentionally ignores HTTP(S)_PROXY/ALL_PROXY.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def complete(self, messages: tuple[dict[str, Any], ...], tools: tuple[ToolSchema, ...], *, max_tokens: int) -> ModelReply:
        return self._complete(messages, tools, max_tokens=max_tokens)

    def _complete(self, messages, tools, *, max_tokens: int, forced_tool: str | None = None) -> ModelReply:
        _positive_integer(max_tokens, "max_tokens")
        if self.requests_used >= self.config.max_requests:
            raise ProviderError("Provider request quota exhausted")
        if not isinstance(tools, (tuple, list)) or not all(isinstance(tool, ToolSchema) for tool in tools):
            raise ProviderError("tools must be typed ToolSchema entries")
        names = {tool.name: tool for tool in tools}
        if len(names) != len(tools) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in names):
            raise ProviderError("Tool names are duplicated or invalid")
        profile = self.config.profile
        if tools and not profile.supports_tools:
            raise ProviderError("Configured model has no declared tool-call support")
        output_limit = min(max_tokens, profile.max_output_tokens)
        payload: dict[str, Any] = {"model": profile.model, "messages": _wire_messages(messages, profile.system_role),
                                   "stream": False, "n": 1, profile.token_parameter: output_limit}
        if profile.temperature is not None:
            payload["temperature"] = 0.0 if forced_tool else profile.temperature
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                                           "parameters": tool.parameters}} for tool in tools]
            payload["tool_choice"] = {"type": "function", "function": {"name": forced_tool}} if forced_tool else "auto"
            payload["parallel_tool_calls"] = profile.supports_parallel_tool_calls
        try:
            encoded = _json(payload).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("Request must contain valid finite UTF-8 JSON") from None
        request_units = len(encoded)
        if request_units + output_limit > profile.context_window:
            raise ProviderError("Request exceeds the conservative model context budget")
        if self.budget_tokens_used + request_units + output_limit > self.config.max_total_tokens:
            raise ProviderError("Provider token quota cannot cover this request")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "pcb-dev"}
        if self.config.api_key_env is not None:
            key = os.environ.get(self.config.api_key_env)
            if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ProviderError("Configured credential environment entry is missing or invalid")
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(self._url, data=encoded, headers=headers, method="POST")
        self.requests_used += 1
        self.budget_tokens_used += request_units
        try:
            with self._opener.open(request, timeout=self.config.timeout_s) as response:
                if response.geturl() != self._url:
                    raise ProviderError("Unexpected endpoint destination; redirects are forbidden")
                if response.status != 200:
                    raise ProviderError(f"Endpoint returned HTTP {int(response.status)}")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ProviderError("Endpoint returned a non-JSON content type")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > self.config.max_response_bytes):
                    raise ProviderError("Endpoint response length is invalid or exceeds its limit")
                content = response.read(self.config.max_response_bytes + 1)
                self.budget_tokens_used += len(content)
                if len(content) > self.config.max_response_bytes:
                    raise ProviderError("Endpoint response exceeded its byte limit")
        except ProviderError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise ProviderError(f"Endpoint returned HTTP {code}; redirects are forbidden") from None
        except Exception as exc:
            # Never include raw response bodies, URLs, request headers or a
            # third-party error message, any of which can echo a credential.
            raise ProviderError(f"Endpoint request failed ({type(exc).__name__})") from None
        if self.budget_tokens_used > self.config.max_total_tokens:
            raise ProviderError("Endpoint response exceeded its conservative token quota")
        data = _parse(content)
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
            raise ProviderError("Endpoint must return exactly one completion choice")
        choice = data["choices"][0]
        if not isinstance(choice, dict) or choice.get("finish_reason") not in ("stop", "tool_calls"):
            raise ProviderError("Completion is truncated, filtered or has an unsupported finish reason")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("function_call") is not None:
            raise ProviderError("Completion is not a supported assistant message")
        text = message.get("content")
        if text is not None and not isinstance(text, str):
            raise ProviderError("Only text completion content is supported")
        calls = _calls(message.get("tool_calls"), names, parallel=profile.supports_parallel_tool_calls)
        if choice["finish_reason"] == "tool_calls" and not calls:
            raise ProviderError("Completion claims tool calls but contains none")
        if calls and choice["finish_reason"] != "tool_calls":
            raise ProviderError("Completion finish reason contradicts its tool calls")
        usage = data.get("usage")
        input_tokens = output_tokens = 0
        if usage is not None:
            if not isinstance(usage, dict):
                raise ProviderError("Invalid endpoint usage accounting")
            input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
                raise ProviderError("Endpoint token counts must be nonnegative integers")
            if "total_tokens" in usage and (type(usage["total_tokens"]) is not int or usage["total_tokens"] != input_tokens + output_tokens):
                raise ProviderError("Endpoint total-token count contradicts its input/output counts")
        self.budget_tokens_used += max(0, input_tokens - request_units) + max(0, output_tokens - len(content))
        if self.budget_tokens_used > self.config.max_total_tokens or output_tokens > output_limit:
            raise ProviderError("Endpoint response exceeded its token quota; tool calls were not accepted")
        return ModelReply(text or "", calls, input_tokens, output_tokens, usage_reported=usage is not None)

    def self_test(self) -> dict[str, Any]:
        """Explicitly send one harmless deterministic tool challenge.

        This checks protocol compliance only, never design quality or a gate.
        """
        nonce = "pcb_dev_self_test_v1"
        tool = ToolSchema("self_test_echo", "Echo the fixed test value; no design changes.",
                          {"type": "object", "properties": {"value": {"type": "string", "const": nonce}},
                           "required": ["value"], "additionalProperties": False})
        try:
            reply = self._complete(({"role": "user", "content": f"Call self_test_echo once with value {nonce}."},),
                                   (tool,), max_tokens=256, forced_tool=tool.name)
            passed = len(reply.tool_calls) == 1 and reply.tool_calls[0].name == tool.name and reply.tool_calls[0].arguments == {"value": nonce}
            return {"passed": passed, "check": "tool-protocol-only", "model": self.config.profile.model,
                    "fabrication_ready": False, "reason": "Challenge matched" if passed else "Challenge not returned"}
        except ProviderError as exc:
            return {"passed": False, "check": "tool-protocol-only", "fabrication_ready": False, "reason": str(exc)}

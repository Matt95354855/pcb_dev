"""Human-requested native Anthropic Messages transport with project accounting.

No startup requests, automatic fallback, redirects, environment proxies or
implicit credentials. Token counting is an estimate, never a billing guarantee.
Unknown or excessive final usage retains its reservation and quarantines Claude.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .agent import ModelReply, ToolSchema
from .providers import ProviderError, _NoRedirect, _calls, _finite, _json, _parse, _wire_messages
from .quota import QuotaError, TokenLedger


class ClaudeError(ProviderError):
    pass


def _positive(value, name):
    if type(value) is not int or not 0 < value <= 10**9:
        raise ClaudeError(f"{name} must be a positive bounded integer")


def _url(endpoint, allowed_hosts):
    if (not isinstance(endpoint, str) or len(endpoint) > 2048
            or any(ord(char) < 33 for char in endpoint)):
        raise ClaudeError("Claude endpoint must be a bounded HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        raise ClaudeError("Invalid Claude endpoint URL") from None
    if (parsed.scheme != "https" or not hostname or hostname.casefold() not in allowed_hosts
            or port not in (None, 443) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/", "/v1", "/v1/", "/v1/messages")):
        raise ClaudeError("Claude endpoint requires HTTPS, an explicitly allowed host and a plain Messages path")
    return f"https://{hostname.casefold()}/v1/messages"


@dataclass(frozen=True)
class ClaudeConfig:
    model: str
    endpoint: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    allow_network: bool = False
    allowed_hosts: tuple[str, ...] = ("api.anthropic.com",)
    context_window: int = 200_000
    max_output_tokens: int = 2048
    timeout_s: float = 30.0
    max_response_bytes: int = 1_048_576
    max_requests: int = 16
    max_total_tokens: int = 128_000
    count_margin_tokens: int = 1024
    count_margin_ratio: float = 0.1
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = False

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 256
                or any(ord(char) < 33 or ord(char) > 126 for char in self.model)):
            raise ClaudeError("Claude model must be an explicit bounded identifier")
        if (not isinstance(self.allowed_hosts, (tuple, list)) or not self.allowed_hosts
                or any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
                       or ".." in host for host in self.allowed_hosts)
                or len(set(self.allowed_hosts)) != len(self.allowed_hosts)):
            raise ClaudeError("Claude allowed_hosts requires distinct explicit lowercase hostnames")
        object.__setattr__(self, "allowed_hosts", tuple(self.allowed_hosts))
        _url(self.endpoint, self.allowed_hosts)
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ClaudeError("Claude api_key_env must name an environment entry, never a literal key")
        for name in ("allow_network", "supports_tools", "supports_parallel_tool_calls"):
            if type(getattr(self, name)) is not bool:
                raise ClaudeError(f"{name} must be a boolean")
        if self.supports_parallel_tool_calls and not self.supports_tools:
            raise ClaudeError("Parallel Claude tools require declared tool support")
        for name in ("context_window", "max_output_tokens", "max_response_bytes", "max_requests", "max_total_tokens"):
            _positive(getattr(self, name), name)
        if self.max_output_tokens >= self.context_window or self.max_response_bytes > 16 * 1024 * 1024:
            raise ClaudeError("Claude context or response limit is invalid")
        if type(self.count_margin_tokens) is not int or not 1 <= self.count_margin_tokens <= 1_000_000:
            raise ClaudeError("count_margin_tokens must be a positive bounded integer")
        for name, low, high in (("timeout_s", 0, 120), ("count_margin_ratio", 0, 1)):
            value = getattr(self, name)
            if not _finite(value) or not low < value <= high:
                raise ClaudeError(f"{name} is outside its finite positive limit")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["allowed_hosts"] = list(self.allowed_hosts)
        return data

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or "model" not in data or set(data) - set(cls.__dataclass_fields__):
            raise ClaudeError("Claude configuration requires an explicit model and known fields only")
        return cls(**data)


def _messages(messages):
    """Translate the same bounded text/tool history as the Qwen transport."""
    source = _wire_messages(messages, "system")
    system, result, pending = [], [], {}
    for message in source:
        role = message["role"]
        if role in ("system", "developer"):
            if result:
                raise ClaudeError("Claude system instructions must precede conversation messages")
            system.append(message["content"])
            continue
        blocks = []
        if role == "tool":
            identifier = message["tool_call_id"]
            if identifier not in pending:
                raise ClaudeError("Claude tool result has no pending tool invocation")
            pending.pop(identifier)
            blocks.append({"type": "tool_result", "tool_use_id": identifier, "content": message["content"]})
            role = "user"
        else:
            if pending:
                raise ClaudeError("Claude tool history omits a required tool result")
            if message["content"]:
                blocks.append({"type": "text", "text": message["content"]})
            if role == "assistant":
                for call in message.get("tool_calls", []):
                    identifier, function = call["id"], call["function"]
                    if not isinstance(identifier, str) or not identifier or identifier in pending:
                        raise ClaudeError("Invalid Claude tool history identifier")
                    arguments = _parse(function["arguments"])
                    if type(arguments) is not dict:
                        raise ClaudeError("Claude tool history input must be an object")
                    blocks.append({"type": "tool_use", "id": identifier,
                                   "name": function["name"], "input": arguments})
                    pending[identifier] = True
        if not blocks:
            raise ClaudeError("Claude conversation messages cannot be empty")
        if result and role == result[-1]["role"]:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    if not result or result[0]["role"] != "user" or result[-1]["role"] != "user" or pending:
        raise ClaudeError("Claude requests require a completed history ending with user input")
    return system, result


def _usage(data):
    usage = data.get("usage")
    if type(usage) is not dict:
        raise ClaudeError("Claude final API usage is missing or invalid")
    result = {name: usage.get(name, 0) for name in
              ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    if ("input_tokens" not in usage or "output_tokens" not in usage
            or any(type(value) is not int or not 0 <= value <= 10**9 for value in result.values())):
        raise ClaudeError("Claude API token counts must be nonnegative bounded integers")
    breakdown = usage.get("cache_creation")
    if breakdown is not None:
        fields = {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}
        if (type(breakdown) is not dict or set(breakdown) != fields
                or any(type(value) is not int or value < 0 for value in breakdown.values())
                or sum(breakdown.values()) != result["cache_creation_input_tokens"]):
            raise ClaudeError("Claude cache-creation breakdown contradicts its aggregate usage")
    # cache_creation is a breakdown of the aggregate, not additional tokens.
    return result


class ClaudeProvider:
    network_enabled = True

    def __init__(self, config: ClaudeConfig, *, ledger: TokenLedger):
        if not isinstance(config, ClaudeConfig) or not isinstance(ledger, TokenLedger):
            raise ClaudeError("Claude requires typed configuration and a project TokenLedger")
        self.config, self.ledger = config, ledger
        self._url = _url(config.endpoint, config.allowed_hosts)
        self.requests_used = 0
        self.budget_tokens_used = 0
        self.last_accounting: dict[str, Any] = {}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _post(self, url, payload):
        if self.requests_used >= self.config.max_requests:
            raise ClaudeError("Claude request limit exhausted; no automatic retry")
        encoded = _json(payload).encode("utf-8")
        if len(encoded) > min(16 * 1024 * 1024, self.config.context_window * 8):
            raise ClaudeError("Claude request exceeds its bounded byte limit")
        key = os.environ.get(self.config.api_key_env)
        if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ClaudeError("Claude credential environment entry is missing or invalid")
        request = urllib.request.Request(url, data=encoded, headers={
            "Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity",
            "x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "pcb-dev"}, method="POST")
        self.requests_used += 1
        try:
            with self._opener.open(request, timeout=self.config.timeout_s) as response:
                if response.geturl() != url or response.status != 200:
                    raise ClaudeError("Claude returned an unexpected destination or non-success status")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ClaudeError("Claude returned a non-JSON content type")
                if response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
                    raise ClaudeError("Claude returned an unsupported content encoding")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isascii() or not length.isdecimal()
                                           or int(length) > self.config.max_response_bytes):
                    raise ClaudeError("Claude response length is invalid or exceeds its limit")
                content = response.read(self.config.max_response_bytes + 1)
                if len(content) > self.config.max_response_bytes:
                    raise ClaudeError("Claude response exceeds its byte limit")
        except ClaudeError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ClaudeError(f"Claude returned HTTP {status}; redirects are forbidden") from None
        except Exception as exc:
            raise ClaudeError(f"Claude request failed ({type(exc).__name__})") from None
        return _parse(content)

    def review(self, messages, *, max_tokens: int, human_authorized: bool = False) -> ModelReply:
        """One human-authorized, text-only review; no Qwen-callable tool is exposed."""
        return self.complete(messages, (), max_tokens=max_tokens, human_authorized=human_authorized)

    def complete(self, messages, tools, *, max_tokens: int, human_authorized: bool = False) -> ModelReply:
        if human_authorized is not True:
            raise ClaudeError("Claude requires explicit human authorization for this individual review")
        if not self.config.allow_network:
            raise ClaudeError("Claude network access is disabled in configuration")
        _positive(max_tokens, "max_tokens")
        if not isinstance(tools, (tuple, list)) or not all(isinstance(tool, ToolSchema) for tool in tools):
            raise ClaudeError("Claude tools must be typed ToolSchema entries")
        names = {tool.name: tool for tool in tools}
        if (len(names) != len(tools) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in names)
                or tools and not self.config.supports_tools):
            raise ClaudeError("Claude tools are duplicated, invalid or not supported")
        system, wire = _messages(messages)
        payload: dict[str, Any] = {"model": self.config.model, "messages": wire}
        if system:
            payload["system"] = "\n\n".join(system)
        if tools:
            payload["tools"] = [{"name": tool.name, "description": tool.description,
                                 "input_schema": tool.parameters} for tool in tools]
        try:
            payload = _parse(_json(payload))  # Detach and validate finite JSON before any request.
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ClaudeError("Claude request must contain bounded finite UTF-8 JSON") from None
        output_limit = min(max_tokens, self.config.max_output_tokens)
        snapshot = self.ledger.snapshot()
        if snapshot["claude_quarantined"] or snapshot["claude_available_tokens"] <= output_limit:
            raise ClaudeError("Project has no available reported-token Claude review envelope")
        # A count endpoint is free, but still requires the human's authorization
        # and explicit network permission. Its result is not API usage credit.
        estimate = self._post(self._url + "/count_tokens", payload)
        if type(estimate) is not dict or set(estimate) != {"input_tokens"}:
            raise ClaudeError("Claude token-count estimate is malformed")
        counted = estimate["input_tokens"]
        if type(counted) is not int or not 0 <= counted <= 10**9:
            raise ClaudeError("Claude token-count estimate must be a nonnegative bounded integer")
        conservative_input = max(len(_json(payload).encode("utf-8")),
            math.ceil(counted * (1 + self.config.count_margin_ratio))) + self.config.count_margin_tokens
        reservation_tokens = conservative_input + output_limit
        if reservation_tokens > self.config.context_window or self.budget_tokens_used + reservation_tokens > self.config.max_total_tokens:
            raise ClaudeError("Claude request exceeds its conservative context/provider token budget")
        # Leave a request slot for inference before persisting a reservation.
        if self.requests_used >= self.config.max_requests:
            raise ClaudeError("Claude request limit leaves no Messages call; no reservation made")
        reservation = self.ledger.reserve_claude(reservation_tokens, human_authorized=True)
        self.budget_tokens_used += reservation_tokens
        self.last_accounting = {"reservation_id": reservation, "reserved_tokens": reservation_tokens,
            "count_tokens_estimate": counted, "estimate_is_guarantee": False}
        payload.update(max_tokens=output_limit, stream=False)
        if tools:
            payload["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": not self.config.supports_parallel_tool_calls}
        settled = False
        try:
            data = self._post(self._url, payload)
            if type(data) is not dict:
                raise ClaudeError("Claude response must be a typed Messages object")
            usage = _usage(data)
            accounting = self.ledger.settle_claude(reservation, **usage, usage_reported=True)
            settled = True
            self.last_accounting.update(api_usage=usage, snapshot=accounting)
            if accounting["claude_quarantined"]:
                raise ClaudeError("Claude API usage exceeded its reservation; project is quarantined and 10% compliance is unverified")
            if usage["output_tokens"] > output_limit:
                self.ledger.fail_claude(reservation)
                raise ClaudeError("Claude output usage exceeded the requested limit; project is quarantined")
            if (data.get("type") != "message" or data.get("role") != "assistant"
                    or not isinstance(data.get("id"), str) or not data["id"]
                    or not isinstance(data.get("model"), str) or not data["model"]
                    or not isinstance(data.get("content"), list) or not 1 <= len(data["content"]) <= 64):
                raise ClaudeError("Claude response has invalid message fields/content")
            text, calls = [], []
            for block in data["content"]:
                if not isinstance(block, dict):
                    raise ClaudeError("Claude content block must be an object")
                if block.get("type") == "text":
                    if (set(block) - {"type", "text", "citations"} or not isinstance(block.get("text"), str)
                            or block.get("citations") not in (None, [])):
                        raise ClaudeError("Claude text block has invalid or unsupported fields")
                    text.append(block["text"])
                elif block.get("type") == "tool_use":
                    if set(block) != {"type", "id", "name", "input"} or type(block["input"]) is not dict:
                        raise ClaudeError("Claude tool-use block has invalid fields")
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": _json(block["input"])}})
                else:
                    raise ClaudeError("Claude returned an unsupported content block type")
            typed_calls = _calls(calls, names, parallel=self.config.supports_parallel_tool_calls)
            stop = data.get("stop_reason")
            if (stop not in ("end_turn", "tool_use", "refusal")
                    or bool(typed_calls) != (stop == "tool_use")):
                raise ClaudeError("Claude response is truncated or its stop reason contradicts content")
            input_total = usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
            return ModelReply("\n".join(text), typed_calls, input_total, usage["output_tokens"], usage_reported=True)
        except BaseException as exc:
            if not settled:
                self.last_accounting["snapshot"] = self.ledger.fail_claude(reservation)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if not settled:
                raise ClaudeError(f"Claude outcome/usage is uncertain ({type(exc).__name__}); reservation retained and project Claude quarantined") from None
            if isinstance(exc, (ProviderError, QuotaError)):
                raise
            raise ClaudeError(f"Claude response failed validation ({type(exc).__name__})") from None

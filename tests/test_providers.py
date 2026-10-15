import json
import os
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from pcb_dev.agent import AgentError, AgentHarness, ModelReply, TOOL_SCHEMAS
from pcb_dev.providers import ChatCompletionsProvider, ModelProfile, ProviderConfig, ProviderError, _NoRedirect


def completion(*, calls=None, content="Ready for review", finish=None, usage=None):
    return {"id": "test-completion", "object": "chat.completion", "choices": [{
        "index": 0, "finish_reason": finish or ("tool_calls" if calls else "stop"),
        "message": {"role": "assistant", "content": content, "tool_calls": calls},
    }], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15} if usage is None else usage}


def call(name="check_design", arguments="{}", identifier="call-1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


class Response:
    def __init__(self, data, *, url="http://127.0.0.1:8000/v1/chat/completions", status=200, headers=None):
        self.content = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.url, self.status = url, status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.read_limits = []

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limits.append(limit)
        return self.content[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or Response(completion())
        self.error = error
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def provider(opener, **configuration):
    with patch("pcb_dev.providers.urllib.request.build_opener", return_value=opener):
        return ChatCompletionsProvider(ProviderConfig(ModelProfile("explicit-test-model"), **configuration))


MESSAGES = ({"role": "system", "content": "Follow the allowed tool schema."}, {"role": "user", "content": "Inspect the design."})


class ProviderTests(unittest.TestCase):
    def test_constructor_and_configuration_never_open_a_connection(self):
        opener = Opener()
        adapter = provider(opener)
        self.assertEqual(opener.calls, [])
        self.assertFalse(adapter.network_enabled)
        AgentHarness(adapter)  # Local-only transport is compatible with offline policy.

    def test_valid_response_converts_to_typed_reply_and_wire_tools(self):
        opener = Opener(Response(completion(calls=[call()], content=None)))
        adapter = provider(opener)
        reply = adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=400)
        self.assertIsInstance(reply, ModelReply)
        self.assertEqual(reply.tool_calls[0].name, "check_design")
        self.assertEqual(reply.tool_calls[0].arguments, {})
        request, timeout = opener.calls[0]
        payload = json.loads(request.data)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["model"], "explicit-test-model")
        self.assertEqual(payload["max_completion_tokens"], 400)
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["function"]["name"], "inspect_design")
        self.assertEqual(timeout, 30)
        self.assertGreater(adapter.budget_tokens_used, reply.input_tokens + reply.output_tokens)

    def test_local_endpoint_names_are_canonical_and_ambiguous_hosts_are_denied(self):
        for url in ("http://localhost:8000/v1", "http://127.0.0.2:8000/v1", "http://[::1]:8000/v1"):
            with self.subTest(url=url):
                adapter = provider(Opener(), endpoint=url)
                self.assertFalse(adapter.network_enabled)
        adapter = provider(Opener(), endpoint="http://localhost:8000/v1")
        self.assertEqual(adapter._url, "http://127.0.0.1:8000/v1/chat/completions")
        for url in ("http://localhost.evil/v1", "http://127.1/v1", "http://2130706433/v1", "http://0.0.0.0/v1", "http://localhost./v1"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                ProviderConfig(ModelProfile("test"), endpoint=url)

    def test_remote_endpoints_require_both_permissions_and_tls(self):
        for config in ({}, {"offline": False}, {"allow_network": True}):
            with self.subTest(config=config), self.assertRaises(ProviderError):
                ProviderConfig(ModelProfile("test"), endpoint="https://endpoint.example/v1", **config)
        with self.assertRaises(ProviderError):
            ProviderConfig(ModelProfile("test"), endpoint="http://endpoint.example/v1", offline=False, allow_network=True)
        adapter = provider(Opener(), endpoint="https://endpoint.example/v1", offline=False, allow_network=True)
        self.assertTrue(adapter.network_enabled)
        with self.assertRaises(AgentError):
            AgentHarness(adapter)
        self.assertEqual(adapter.requests_used, 0)

    def test_endpoint_rejects_secrets_and_url_normalization_tricks(self):
        urls = ["http://user:password@localhost:8000/v1", "http://localhost/v1?api_key=SECRET", "http://localhost/v1#SECRET",
                "http://localhost/api_key/SECRET", "http://localhost/%2e%2e/remote", "file:///tmp/secret", "http://localhost/v1\n", "http://localhost:99999/v1"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ProviderError):
                ProviderConfig(ModelProfile("test"), endpoint=url)

    def test_environment_proxies_are_disabled_and_redirects_are_refused(self):
        opener = Opener()
        with patch("pcb_dev.providers.urllib.request.build_opener", return_value=opener) as factory:
            ChatCompletionsProvider(ProviderConfig(ModelProfile("test")))
        handlers = factory.call_args.args
        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {} for handler in handlers))
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in handlers))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://remote.example"))
        adapter = provider(Opener(Response(completion(), url="https://remote.example/v1/chat/completions")))
        with self.assertRaises(ProviderError):
            adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)

    def test_credentials_are_environment_only_and_never_echoed_in_errors(self):
        adapter = provider(Opener(), api_key_env="PCB_TEST_KEY")
        with patch.dict(os.environ, {"PCB_TEST_KEY": "PRIVATE_CREDENTIAL"}, clear=True):
            adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        self.assertNotIn("PRIVATE_CREDENTIAL", json.dumps(adapter.config.to_dict()))
        self.assertEqual(adapter._opener.calls[0][0].get_header("Authorization"), "Bearer PRIVATE_CREDENTIAL")
        with self.assertRaises(ProviderError):
            ProviderConfig(ModelProfile("test"), api_key_env="sk-literal-credential")
        for value in (None, "bad\nheader", "bad header"):
            with patch.dict(os.environ, {} if value is None else {"PCB_TEST_KEY": value}, clear=True), self.assertRaises(ProviderError):
                provider(Opener(), api_key_env="PCB_TEST_KEY").complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        opener = Opener(error=RuntimeError("PRIVATE_CREDENTIAL https://secret-url"))
        with self.assertRaises(ProviderError) as raised:
            provider(opener).complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        self.assertNotIn("PRIVATE_CREDENTIAL", str(raised.exception))
        self.assertNotIn("secret-url", str(raised.exception))

    def test_timeouts_http_errors_and_response_size_fail_closed(self):
        errors = (TimeoutError("timeout secret"), urllib.error.HTTPError("https://SECRET", 302, "SECRET", {}, None))
        for error in errors:
            with self.subTest(error=type(error).__name__), self.assertRaises(ProviderError) as raised:
                provider(Opener(error=error)).complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
            self.assertNotIn("SECRET", str(raised.exception))
        for response in (Response(b"x" * 101), Response(completion(), headers={"Content-Length": "999999"}),
                         Response(completion(), headers={"Content-Type": "text/html"})):
            with self.subTest(response=response.headers), self.assertRaises(ProviderError):
                provider(Opener(response), max_response_bytes=100).complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)

    def test_malformed_duplicate_and_truncated_completions_are_rejected(self):
        invalid = [b"not json", json.dumps(completion()).encode("utf-16"), b'{"choices":[],"choices":[]}', completion(finish="length"),
                   {"choices": []}, completion(calls=[call()], finish="stop"), completion(finish="tool_calls")]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ProviderError):
                provider(Opener(Response(data))).complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)

    def test_returned_tool_calls_have_strict_names_fields_and_nested_schemas(self):
        bad_calls = [call(name="shell"), call(arguments='{"waive":true}'), call(arguments='{"x":1,"x":2}'),
                     call(arguments='{"x":NaN}'), {**call(), "extra": True}, call(arguments="[]"),
                     call(name="apply_design_patch", arguments=json.dumps({"reason": "bad", "operations": [{"op": "add_block", "parent": "board", "block": {"id": "extra", "frozen": True}}]})),
                     call(name="apply_design_patch", arguments=json.dumps({"reason": "bad", "operations": [{"op": "remove_connection", "source": "a", "target": "b", "net": "N"}]}))]
        for bad_call in bad_calls:
            with self.subTest(call=bad_call), self.assertRaises(ProviderError):
                provider(Opener(Response(completion(calls=[bad_call])))).complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)

    def test_nested_valid_patch_schema_and_history_conversion(self):
        operations = [{"op": "add_block", "parent": "board", "block": {"id": "sensor", "ports": [{"id": "bus", "kind": "bus", "direction": "bidirectional"}], "children": [{"id": "nested"}]}},
                      {"op": "set_parameter", "block": "board", "name": "temperature", "value": 25}]
        arguments = {"reason": "Add declared sensor", "operations": operations}
        opener = Opener(Response(completion(calls=[call("apply_design_patch", json.dumps(arguments))])))
        adapter = provider(opener)
        reply = adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        self.assertEqual(reply.tool_calls[0].arguments, arguments)
        messages = (*MESSAGES, {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "name": "check_design", "arguments": {}}]},
                    {"role": "tool", "name": "check_design", "tool_call_id": "call-1", "content": "{}"})
        adapter.complete(messages, TOOL_SCHEMAS, max_tokens=100)
        payload = json.loads(opener.calls[-1][0].data)
        self.assertEqual(payload["messages"][-2]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertNotIn("name", payload["messages"][-1])

    def test_request_context_and_provider_quotas_prevent_calls(self):
        opener = Opener()
        adapter = provider(opener, max_requests=1)
        adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        with self.assertRaises(ProviderError):
            adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        self.assertEqual(len(opener.calls), 1)
        adapter = provider(Opener(), max_total_tokens=1)
        with self.assertRaises(ProviderError):
            adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        self.assertEqual(adapter.requests_used, 0)
        with patch("pcb_dev.providers.urllib.request.build_opener", return_value=opener):
            adapter = ChatCompletionsProvider(ProviderConfig(ModelProfile("test", context_window=1000, max_output_tokens=100)))
        with self.assertRaises(ProviderError):
            adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)

    def test_usage_must_be_consistent_and_overbudget_output_is_not_accepted(self):
        for usage in ({"prompt_tokens": True, "completion_tokens": 1}, {"prompt_tokens": 1, "completion_tokens": -1},
                      {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}, {"prompt_tokens": 1, "completion_tokens": 99999}):
            with self.subTest(usage=usage), self.assertRaises(ProviderError):
                provider(Opener(Response(completion(calls=[call()], usage=usage)))).complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)

    def test_model_capabilities_and_optional_parameters_are_explicit(self):
        opener = Opener()
        profile = ModelProfile("local-model", token_parameter="max_tokens", temperature=None, system_role="developer", supports_tools=False)
        with patch("pcb_dev.providers.urllib.request.build_opener", return_value=opener):
            adapter = ChatCompletionsProvider(ProviderConfig(profile))
        with self.assertRaises(ProviderError):
            adapter.complete(MESSAGES, TOOL_SCHEMAS, max_tokens=100)
        adapter.complete(MESSAGES, (), max_tokens=100)
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload["messages"][0]["role"], "developer")
        self.assertIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("tools", payload)

    def test_self_test_is_explicit_harmless_and_requires_exact_challenge(self):
        valid = call("self_test_echo", '{"value":"pcb_dev_self_test_v1"}')
        opener = Opener(Response(completion(calls=[valid])))
        adapter = provider(opener)
        self.assertFalse(opener.calls)
        result = adapter.self_test()
        self.assertTrue(result["passed"])
        self.assertFalse(result["fabrication_ready"])
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload["tool_choice"]["function"]["name"], "self_test_echo")
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual([tool["function"]["name"] for tool in payload["tools"]], ["self_test_echo"])
        self.assertFalse(provider(Opener(Response(completion(calls=[call("self_test_echo", '{"value":"wrong"}')])))).self_test()["passed"])

    def test_config_rejects_invalid_scalars_and_limits(self):
        for changes in ({"temperature": True}, {"context_window": 0}, {"supports_tools": "yes"}, {"token_parameter": "eval"}, {"model": ""}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                ModelProfile(**{"model": "test", **changes})
        for changes in ({"timeout_s": float("nan")}, {"timeout_s": 121}, {"offline": 1}, {"max_requests": True}, {"max_response_bytes": 100_000_000}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                ProviderConfig(ModelProfile("test"), **changes)

    def test_configuration_round_trip_is_strict_and_contains_no_literal_key(self):
        original = ProviderConfig(ModelProfile("explicit-model"), api_key_env="PCB_MODEL_KEY")
        self.assertEqual(ProviderConfig.from_dict(original.to_dict()), original)
        for data in ({"profile": {"model": "test"}, "api_key": "SECRET"},
                     {"profile": {"model": "test", "unknown": True}}, {"profile": None}, {}):
            with self.subTest(data=data), self.assertRaises(ProviderError):
                ProviderConfig.from_dict(data)


if __name__ == "__main__":
    unittest.main()

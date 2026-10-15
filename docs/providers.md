# Optional local or remote model endpoint

`pcb_dev.providers.ChatCompletionsProvider` implements the text/function-tool subset of the JSON Chat Completions protocol using Python's standard library. No model, hosted vendor, API key or server is selected automatically. Construction validates configuration without sending a request. The application must explicitly invoke `complete()`, an agent run using this provider, or `self_test()`.

## Configure a model explicitly

```python
from pcb_dev.agent import AgentHarness
from pcb_dev.providers import ChatCompletionsProvider, ModelProfile, ProviderConfig

config = ProviderConfig(
    profile=ModelProfile(
        model="your-served-model-id",
        context_window=32768,
        max_output_tokens=2048,
        temperature=0.0,
        supports_tools=True,
        supports_parallel_tool_calls=False,
        token_parameter="max_tokens",  # Select the parameter your local server accepts.
    ),
    endpoint="http://127.0.0.1:8000/v1",
    offline=True,
)
provider = ChatCompletionsProvider(config)
assistant = AgentHarness(provider)  # This line still sends no request.
```

The caller supplies the actual model identifier and its verified context/capabilities. There is no automatic model substitution or claim that a particular model/server has been qualified. A model without declared tool support can handle text-only `complete(..., tools=())`; the design assistant requires tool support. `temperature=None` omits that parameter for endpoints/models that reject it. `system_role="developer"` converts the harness's system message for servers expecting developer messages.

`max_completion_tokens` is the profile default. The official API documents it as the current completion limit and `max_tokens` as deprecated; the latter remains an explicit compatibility choice for local servers. Parameter support varies by model. [Official Chat Completions request reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

## JSON configuration

`ProviderConfig.from_dict()` and `ModelProfile.from_dict()` reject unknown fields and invalid supplied scalar values. Defaults apply only to omitted optional fields. Keep duplicate-key rejection at the file-loading boundary, for example with `pcb_dev.transactions.read_json()`.

```json
{
  "profile": {
    "model": "your-served-model-id",
    "context_window": 32768,
    "max_output_tokens": 2048,
    "temperature": 0.0,
    "supports_tools": true,
    "supports_parallel_tool_calls": false,
    "token_parameter": "max_tokens",
    "system_role": "system"
  },
  "endpoint": "http://127.0.0.1:8000/v1",
  "api_key_env": null,
  "offline": true,
  "allow_network": false,
  "timeout_s": 30,
  "max_response_bytes": 1048576,
  "max_requests": 32,
  "max_total_tokens": 256000
}
```

```python
from pcb_dev.transactions import read_json

config = ProviderConfig.from_dict(read_json("model-config.json"))
assert ProviderConfig.from_dict(config.to_dict()) == config
```

There is no `schema_version` field in this initial provider-configuration shape. There is no literal `api_key` field: use the **name** of an environment variable, such as `"api_key_env": "PCB_MODEL_KEY"`. Its value is read only at request time, sent in the authorization header, and never copied into configuration, public results or transport error messages. Missing keys and control characters fail before a request is sent. Supply keys through your shell/environment or a credential manager; do not commit them into a configuration file, design or prompt.

## Offline and remote boundaries

Offline mode allows only literal loopback IP addresses and `localhost`; the latter is rewritten to `127.0.0.1` without DNS resolution. Abbreviated numeric hosts, `0.0.0.0`, a remote hostname and names such as `localhost.example` do not qualify. Local HTTP is permitted; local HTTPS still uses normal certificate verification.

Remote endpoints require **both** `ProviderConfig(offline=False, allow_network=True)` and `AgentHarness(..., offline=False)`. Remote transport requires HTTPS with the standard certificate verification. Changing only one flag does not grant remote access.

Endpoint URLs cannot contain userinfo, query parameters, fragments, encoded/ambiguous API paths or recognizable credential material. The adapter appends `/chat/completions` to a base URL unless that suffix is already present. Environment proxy variables are ignored, and every redirect is refused. The final response URL must match the configured destination. No retry, fallback endpoint or automatic download occurs.

These controls constrain the included adapter. Arbitrary third-party Python providers remain trusted application code; use operating-system isolation when executing untrusted extensions. Local inference can still log prompts or forward them depending on the server's own configuration, which the client cannot establish.

## Protocol validation and limits

Requests use `stream=false`, `n=1`, typed function definitions, configured temperature/capabilities and a bounded completion limit. Tool observations are mapped to their call IDs; argument objects are serialized as JSON strings on the wire. The protocol's function-call arguments are model-generated JSON and must be validated by the application. [Official function-calling guide](https://developers.openai.com/api/docs/guides/function-calling).

The adapter independently validates finite UTF-8 JSON, duplicate keys, one completion choice, supported finish reasons, tool names/IDs, exact function-call fields, JSON argument objects and the JSON Schema subset used by the harness tools. Unknown arguments, truncated output, contradictory tool-call finish reasons and inconsistent usage counts fail closed. Model tool calls are still subject to the harness's transaction, frozen-block and requirement-preservation checks.

No server-side `strict=true` guarantee is claimed: the harness schemas include optional fields and local-server compatibility varies. Runtime validation remains authoritative. Parallel calls are disabled unless explicitly declared by the profile, and are capped at 32 when enabled. Streaming, image/audio content, legacy `function_call` responses and custom tools are outside this adapter's supported subset.

The timeout is a socket-operation timeout, positive and capped at 120 seconds; it is not a complete process-wide deadline against a malicious slow server. Responses are bounded by both the declared content length and actual bytes read. Request quotas count attempted requests, including failures, and no automatic retry consumes additional calls.

Context and cumulative token quotas use conservative serialized UTF-8 byte counts, raised when reported usage is greater. Missing usage is not invented; the byte charge remains in force. A request exceeding its context/quota is refused before connection; an overbudget reply is rejected before any tool call reaches the harness. These client limits cannot replace server-side spend limits: a remote request may already incur charges before its usage is returned.

HTTP tests use mocked responses. No actual local or remote endpoint, billed API request, specific model capability or inference throughput has been validated by this test suite.

## Invoke a harmless protocol self-test

```python
# Explicit action: this sends one request to the configured endpoint.
result = provider.self_test()
print(result)
```

The test exposes only `self_test_echo`, asks for a fixed challenge value and checks the exact returned tool/arguments. It uses no design, filesystem tool, shell or fabrication operation. Construction never runs it. It consumes the same request/token quotas and reports protocol success/failure with `fabrication_ready=False`.

A passing challenge demonstrates one response's tool-protocol compliance. It does not establish engineering competence, resistance to prompt injection, model benchmark quality or fabrication readiness.

## Runpod et Claude

Voir le [guide de déploiement Qwen/Runpod et Claude](runpod-claude.md).
Le gabarit Runpod utilise le transport compatible existant ; l’adaptateur natif
Claude et le routage entre fournisseurs restent à implémenter et à tester.

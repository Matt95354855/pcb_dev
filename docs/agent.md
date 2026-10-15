# Bounded, offline-first design assistant

`pcb_dev.agent` supplies a provider-independent tool loop. It works without an API key, network access, a GPU or a particular model. Its included `ScriptedProvider` makes examples, regression tests and design proposals reproducible.

An assistant session returns a proposed design and the actual preflight report. It **never authorizes fabrication**, even when a model says that everything passed. Native KiCad ERC/DRC, qualified component/library mapping and the release gate remain separate engineering checks.

## Run a reproducible session

```python
from pcb_dev.agent import AgentHarness, Budget, ModelReply, ScriptedProvider, ToolCall
from pcb_dev.model import dump_design, load_design

source = load_design("examples/usb_mcu_led.json")
provider = ScriptedProvider([
    ModelReply(tool_calls=(ToolCall("inspect-1", "inspect_design", {}),)),
    ModelReply(tool_calls=(ToolCall("edit-1", "apply_design_patch", {
        "reason": "Record the specified operating ambient temperature.",
        "operations": [{
            "op": "set_parameter",
            "block": source.root.id,
            "name": "ambient_temperature_c",
            "value": 25,
        }],
    }),)),
    ModelReply("Ambient temperature recorded. Native checks remain to be run."),
])

result = AgentHarness(provider, Budget(max_steps=5, max_tokens=100_000)).run(
    source, "Record a 25 °C ambient temperature."
)
print(result.status)
print(result.final_report.to_markdown())
assert result.fabrication_ready is False
# The source object and source file were never modified.
# Persistence is an explicit application action, outside the model's tools.
dump_design(result.design, "proposal.json")
with open("agent-trace.json", "w", encoding="utf-8") as stream:
    stream.write(result.trace_json())
```

Use an existing example path from your checkout. The Python API does not open or save files on behalf of the model; `load_design`, `dump_design` and the trace write above are explicit actions by the host application.

## Provider contract

An adapter implements the `ModelProvider` protocol:

```python
class MyProvider:
    network_enabled = False

    def complete(self, messages, tools, *, max_tokens):
        # Return a typed ModelReply, optionally with typed ToolCall objects.
        # tools is a tuple of ToolSchema objects; each has .to_dict().
        # Honor max_tokens and report actual input/output token counts.
        return ModelReply("No further edits proposed.", input_tokens=0, output_tokens=0)
```

No hosted adapter, endpoint, model name or external SDK is selected automatically. `AgentHarness(..., offline=True)` is the default and refuses a provider unless it explicitly declares `network_enabled = False`. The optional [Chat Completions transport](providers.md) permits local loopback HTTP in offline mode. Remote endpoints require explicit transport permissions and `AgentHarness(..., offline=False)`.

The provider is trusted Python application code. Its declared offline capability is a policy boundary, **not an operating-system sandbox**: arbitrary Python code can lie about its own behavior. The untrusted model receives only detached message/schema data and can invoke only the harness tools below. Deploy untrusted provider code in a separate restricted process.

Keep credentials in the host's environment or credential store, outside the design and prompts. Never expose them through a tool schema. `redaction_secrets=(...)` can list additional secret values that must be removed from serialized results and traces.

## Allowed tools and exact changes

| Tool | Arguments | Effect |
| --- | --- | --- |
| `inspect_design` | `{}` or `{"block_path":"board.block"}` | Returns normalized model data and its SHA-256. |
| `check_design` | `{}` | Returns the real metadata preflight report; no native check is claimed. |
| `apply_design_patch` | `{"reason":"…","operations":[…]}` | Applies a single validated batch to an isolated copy. |

`ToolSchema.parameters` contains JSON Schema for every tool, including nested block, port and component fields. Runtime checks also reject unknown tools, unexpected fields, duplicate JSON keys, nonfinite values and malformed operation types. Tool arguments can be dictionaries or JSON strings. Argument payloads are limited to 128,000 UTF-8 bytes, with at most 32 operations per patch.

The patch tool exposes exactly these operations from `pcb_dev.transactions`:

```json
{"op":"set_parameter","block":"board.device","name":"limit","value":10}
{"op":"update_block","block":"board.device","changes":{"description":"Reviewed operating assumptions"}}
{"op":"add_block","parent":"board","block":{"id":"sensor","ports":[]}}
{"op":"connect","source":"board.source.out","target":"board.sink.in","net":"DATA"}
```

`update_block.changes` may contain `version`, `description`, `parameters`, `equations`, `constraints`, `resources` and `variants`. Its model parser validates each supplied value. The model cannot change IDs or replace an existing block's ports/components through this tool. New interfaces can be created with an added block and connected in the **same batch**, so intermediate unconnected ports are never committed.

Every batch uses `apply_patch(..., prevent_regressions=True)`; the model cannot switch this protection off. Model validation, dependency checks and preflight must complete. A rejected batch leaves the working design unchanged, and the model observes the actual rejection. Frozen subtrees and edits that invalidate a frozen block's upstream context are protected. Freeze records cannot be created through an added subtree or changed through a block update.

The harness also preserves existing constraint expressions/messages and resource/sequencing declarations. It may add constraints or resources but cannot erase a failure by removing its requirement. Revising an existing requirement requires an explicit human transaction outside the model tools. Arbitrary parameter/equation changes can still alter the meaning of a requirement; freeze approved blocks and review the proposed diff. The harness cannot infer engineering intent from an unconstrained parameter name.

There are no tools for waivers, freeze/unfreeze, final exports, filesystem reads, network calls, shell commands or arbitrary Python execution. A textual request to bypass a gate has no authority. Model text and proposed changes still require review appropriate to the board's application.

For explicit application-level undo and persisted transaction replay, use `DesignSession` from `pcb_dev.transactions`. The harness itself leaves the caller's original design untouched and returns the final proposed copy.

## Budgets and stopping conditions

`Budget` defaults to eight model steps, 64,000 conservative token-budget units, three steps without new design progress and eight tool calls per model reply. Limits must be positive integers. Each model request counts as one step; a provider exception still consumes its conservative request charge.

Provider-independent exact tokenization is unavailable. The harness therefore charges the serialized request and response's UTF-8 byte length, or the provider's reported input/output tokens when greater. This deliberately overestimates typical token counts. It includes message history, tool schemas and previous tool observations. The output allowance sent to the provider is bounded by the remaining budget. A reply exceeding the budget is recorded but its proposed calls are not executed. A provider can exceed a remote billing cap before reporting its usage; enforce a service-side billing limit when integrating a hosted provider.

Unchanged checks, repeated failed edits, no-op edits and revisited design fingerprints count toward the no-progress limit. This also catches simple oscillating edits. Tool-call IDs must be unique across a run, so a repeated ID cannot apply a second change.

| Session status | Meaning |
| --- | --- |
| `model_finished` | The provider returned text without tool calls. Actual report failures still apply. |
| `step_budget` | The permitted number of model calls was exhausted. |
| `token_budget` | The next request or received reply exceeded the conservative budget. |
| `no_progress` | Repeated actions produced no new design state. |
| `provider_error` | The provider failed, changed its offline declaration, or returned invalid typed data. |

None of these statuses means electrical validation or fabrication approval. Display `result.final_report` and native gate evidence separately from `result.final_message`, which remains untrusted model text. `result.fabrication_ready` and its serialized value are always `False`.

## Traces and redaction

Events record their sequence, tool inputs/results, design hashes before/after changes, budget consumption and stopping reason. The trace contains no clock timestamps or random IDs added by the harness, so the same design, objective and scripted replies produce identical trace bytes.

`trace_json()` and `AgentResult.to_dict()` redact common credential keys, bearer tokens, common secret-text patterns, JSON-string tool arguments and explicitly configured secret values. Redaction is best effort; arbitrary secret text cannot be identified reliably without an explicit value or recognizable key/pattern. Keep secrets out of model/design data. In-memory `result.design` and `result.final_report` retain the original design data; use the redacted serialization methods for logs.

Traces provide reproducible evidence, not a digital signature or proof of model correctness. Redacted secrets are intentionally unrecoverable from the log, and the assistant trace is not an automatic crash-recovery format. The separate transaction session format supports validated design replay and undo; applications should persist it explicitly when needed.

## Implemented and deferred

Implemented: provider abstraction, an offline scripted provider, typed allowlisted tools, transactional edits, non-regression/frozen-block protection, observed check results, step/token/no-progress budgets, deterministic credential-redacted traces, and explicit separation of model output from release authority.

The optional local/remote JSON Chat Completions transport and an explicitly invoked tool-protocol self-test are documented in [providers.md](providers.md). Transport tests mock HTTP; they do not establish compatibility with a particular deployed model/server.

Deferred: streaming, provider credential configuration UI, context retrieval from datasheets, specialized subagents, persistent assistant memory, multimodal board inspection, crash recovery and comparative model benchmarking. No test or document claims these capabilities have been exercised.

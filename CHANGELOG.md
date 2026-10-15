# Changelog

## 0.1.1 — 2026-10-15

- Preserve LF bytes on Windows so generated KiCad manifest hashes match the files.
- Read the UTF-8 model fixture explicitly in the portable test suite.

## 0.1.0 — 2026-10-15

Initial alpha of the block-model and verification layer.

- Strict hierarchical JSON model, typed interfaces, dependency order/impact,
  safe equation evaluation, constraint checks, and contextual freezing.
- Electrical metadata preflight, explicit report evidence, design-bound waivers,
  atomic proposal transactions, undo, and chained audit records.
- Version-pinned local block packages, assembly variants, library inventories,
  offline component catalog, and conservative manufacturing planning profiles.
- Deterministic generic KiCad previews, BOMs, draft placement tables, an offline
  review viewer, artifact manifests, and reproducible preview archives.
- Native KiCad 9/10 adapters and smoke workflow, explicit SPICE and Freerouting
  integrations, equation tolerance exploration, and model/source parity checks.
- Provider-independent bounded agent tools, scripted offline replay, configurable
  compatible model endpoint, secret-redacted traces, and explicit network policy.
- French/English READMEs, developer docs, examples, and portable unit-test CI.

The physical manufacturing milestone remains open. Generated footprints and MCU
pinouts are demonstrations, not qualified parts. Native tools are optional; an
absent engine never counts as a passed check. No board order or GPU provisioning
is performed.

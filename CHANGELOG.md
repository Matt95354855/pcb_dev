# Changelog

## 0.4.0 — 2026-10-15

- Add a prompt-first French studio with asynchronous model work, human brief
  approval, plans, evidence, draft artifacts and persistent project restart.
- Let a bounded self-hosted Qwen manager create and revise designs and invoke
  real build, KiCad native-check and passive Ngspice tools with host-owned paths.
- Add an optional native Claude Messages provider for explicit human reviews;
  no Qwen-callable Claude tool or automatic fallback is exposed.
- Persist measured project token usage and advance reservations under a 10%
  Claude policy, retaining unknown charges and blocking uncertain accounting.
- Protect studio access with loopback defaults or an explicitly authenticated
  HTTPS proxy origin, bounded requests and a confined artifact manifest.
- Preserve draft status and physical qualification blockers. Scripted demos and
  native-engine orchestration tests do not qualify a real Qwen model.

## 0.3.0 — 2026-10-15

- Link block ports to explicit global nets and same-block component pins, with
  exact net consistency, fitted-pin coverage and an opt-in strict CLI/build policy.
- Include physical net intent in frozen context, prefix it during package
  instantiation, and protect existing intent in model-facing proposals.
- Add strict library-aware parity for actual pin names/types, complete electrical
  pad coverage and explicitly evidenced no-connects; retain the legacy comparator.
- Add a real-library four-interface divider, CLI native gate and no-connect smoke
  workflows; retain unrouted DRC blockers and pending manufacturing qualification.
- Show physical interface coverage in the offline viewer and generated design guide.

## 0.2.0 — 2026-10-15

- Add complete explicit bindings to pinned real KiCad symbols and footprints,
  preserving electrical pin types, pads, holes, courtyard and silkscreen geometry.
- Generate autonomous local symbol/footprint tables and stable references/nets;
  require supported single-unit symbols and top-side placement with no generic fallback.
- Capture and bundle exact source bytes, SHA-256 pins, licenses and attribution
  for reproducible offline reconstruction.
- Add `build --bindings`, a distinct unqualified `library-draft` status, an official
  KiCad connector/resistor fixture, and native geometry/connectivity smoke checks.
- Keep external 3D model references inert in placed footprints and retain explicit
  unrouted/physical-qualification blockers.

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

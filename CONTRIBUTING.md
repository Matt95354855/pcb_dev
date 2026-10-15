# Contributing

Use Python 3.11 or newer. The runtime has no third-party dependencies.

```sh
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -v
```

Keep changes focused and add behavioral tests for new checks, parsers, gates, and exporters.
Run `pcb-dev check examples/usb_mcu_led.json` and build the example before opening a PR.
Use `python -m pip install ruff==0.14.0` then `ruff check src tests` for the CI lint rules.

Electrical limits must carry units in their field names. Unknown native-tool results
must fail closed. Do not turn a missing tool, skipped check, report parse failure,
or empty export directory into success. Generic preview footprints are not validated
library footprints. Never describe them as manufacturing ready.

Changes to the design contract require a migration decision, invalid-input tests,
and documentation. Do not execute imported packages, design expressions, datasheets,
or model-generated code. Keep external tools behind the bounded runner.

Commit messages explain the resulting behavior. PRs describe the problem, change,
validation performed, and remaining limits. CI unit tests do not replace native ERC/DRC.

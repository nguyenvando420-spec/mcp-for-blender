# BAS-03 — Local validation record (26/10/2026)

## Source and environment

- Branch: `feature_bas03-contracts`, based on `e6410e2eaaaf00821fe309684072e01e02b1fa1a` (BAS-02 main).
- macOS 26.7.1 arm64; gateway Python 3.12.14; Pydantic 2.12.3; JSON Schema validator 4.26.0.
- No Blender GUI or render process used by this contract-only change. BAS-02 golden artifacts are separate evidence.

## Commands and results

| Command | Result |
| --- | --- |
| `.venv/bin/python scripts/export_studio_schema.py` | Pass: 5 schema files written |
| `.venv/bin/python tests/test_studio_contracts.py` | Pass: 6 test functions; request/version/scope/unknown-field, job cancellation/recovery/terminal, response/artifact negative cases and schema parity |
| `.venv/bin/python -m compileall -q src tests scripts addon.py main.py` | Pass |
| `git diff --check` | Pass |
| `Draft202012Validator.check_schema` for all 5 generated schemas | Pass |
| `.venv/bin/python -m pytest -q tests/test_studio_contracts.py` | Skipped: pytest is not installed in the local venv; CI must run the normal pytest suite |

The test module runs directly with the standard library assertion helper and remains collectable by pytest in CI. These tests validate contracts and state transitions; they do not prove Studio dispatch, job persistence, Blender compatibility or path authorization.

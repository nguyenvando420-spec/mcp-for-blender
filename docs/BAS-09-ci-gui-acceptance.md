# BAS-09 — CI matrix and MCP client → Blender GUI acceptance

Scope: Notion BAS-09. Base: main after BAS-05/06/07/08/12 merges.

The existing BAS-01/BAS-02 pipeline remains authoritative. BAS-09 extends the pinned Blender 4.5.15 Xvfb GUI job to both Python 3.11.14/3.12.14. A real MCP ClientSession drives the Studio gateway over STDIO, the authenticated add-on socket and Blender GUI main-thread queue. Do not count the separately executed headless reopen as GUI E2E.

## Real GUI cases

The `scripts/bas05_mcp_e2e.py` runner is reused, not duplicated. New tests close a first MCP client and process, connect a **new** ClientSession to the same living Blender GUI, replay the same idempotent create and assert no duplicate object, new revision or UUID. Negative raw TCP probes exercise invalid auth, forbidden `execute_code` and incomplete-frame socket read deadline. The latter does not cancel an in-flight mutation. BAS-06 socket tests cover UNKNOWN execution state for a mutation timeout. Existing GUI stress includes 50 create + 50 inspect, timer heartbeat, snapshot/restore, save/open, text, collections, typed transforms/deletion, camera/viewport capture, and GUI-outside-MCP edits.

`scripts/bas09_ci_report.py gui` validates 13 fail-closed cases and publishes image/BLEND checksums with environment metadata. Per Python version `pytest -rs --junitxml` and `scripts/bas09_ci_report.py pytest` list **every skipped test**, cause, category and coverage impact. Unknown skip categories are for manual triage, not passing coverage.

## Reproduction

```bash
uv sync --frozen --python 3.12.14
uv run --frozen --python 3.12.14 --with pytest==8.4.2 pytest -q -rs --junitxml=pytest.xml
uv run --frozen --python 3.12.14 python scripts/bas09_ci_report.py pytest --input pytest.xml --output skips.json
uv run --frozen --python 3.12.14 python scripts/run_golden.py --output-dir artifacts/golden
uv run --frozen --python 3.12.14 python scripts/bas05_mcp_e2e.py --output-dir artifacts/gui
uv run --frozen --python 3.12.14 python scripts/bas09_ci_report.py gui --input artifacts/gui/bas05-e2e.json --output artifacts/gui/bas09-evidence.json
```

The Xvfb lane requires Linux x86_64, graphics libraries and Blender runtime downloaded and SHA256-verified by BAS-02. GitHub Actions retains separate GUI evidence archives for each Python version, plus skip inventories.

## Gates still requiring evidence

Verify final PR head SHA and both CI matrices: tests, failures/skips, build, addon parity, pinned golden and GUI. Collect artifact IDs, source SHA, versions and logs. Xvfb is a real GUI process; it is not a mouse-driven human desktop test. Manual acceptance on a human-operated machine and actual addon process-crash restart are **not claimed**; record exception or run separate manual UAT. GUI reconnect within a living addon is tested; durable replay after Blender crash belongs to BAS-22. Do not mark Done until reviewed and accepted.

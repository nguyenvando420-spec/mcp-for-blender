# BAS-14 — Modeling MVP: frozen 20-prompt benchmark & release gate

Status: **Implementation / evidence collection, NOT release accepted**.
Notion: https://app.notion.com/p/40515de298eb810dacd2ea2f78c8f165

## Scope and freeze protocol

- This PR freezes **exactly 20 user-facing Vietnamese prompts** in `benchmarks/bas14_suite.json`: 16 modeling + 4 mandatory safety/rollback cases, including a two-story house, text, collections, materials, camera, rendering, export and negative cases.
- The corpus has named expected scene objects, Blender types, collections, active camera, required artifacts and visual dimensions. Cases 01, 02 and 16 require real BLEND reopen and GLB reimport evidence.
- Run `python scripts/bas14_benchmark.py validate --suite benchmarks/bas14_suite.json` and record the reported SHA-256 **before** any prompt attempts. The runner must pass exactly that SHA to `run.json`. A change to this suite invalidates previous scores and requires a new run/corpus version.
- Use a fresh isolated Blender 4.5.15 scene and private studio sandbox per prompt. Freeze agent system prompt, client version, model ID/version, exact source commit, Blender binary/version, permission profile, environment and prompt text before starting. Disable arbitrary Python, network fetches and developer-local bypass. Use typed MCP Studio tools only (BAS-05..13).
- An **operator actually submits** each prompt to the named ChatGPT/Claude MCP client; this repo does not contain an LLM inference runner and does not synthesize transcripts or scores. Keep the raw client transcript, all MCP request/response logs, GUI captures and artifact manifests. Prompt-to-tool reasoning quality is out of scope for a scripted API-only smoke.

## Evidence contract

Place one `run.json` in a private evidence directory with:
```json
{
  "suite_sha256": "<output-of-validate>",
  "run_id": "real-run-id",
  "client_name": "ChatGPT or Claude",
  "client_version": "exact-version",
  "model_name": "exact-model",
  "model_version": "exact-version",
  "blender_version": "4.5.15",
  "source_commit": "<actual-git-commit-sha>",
  "environment": {"os": "Linux", "gui": "Xvfb-or-desktop", "profile": "studio-local"},
  "cases": [{"id": "BAS14-01", "attempts": [{
    "attempt": 1, "status": "succeeded", "log_path": "BAS14-01/mcp-transcript.log",
    "scene_path": "BAS14-01/scene-inspect.json",
    "assets": {"png": {
      "path": "BAS14-01/still.png", "manifest": "BAS14-01/manifest.json",
      "sha256": "<actual-sha256>", "bytes": 10000
    }},
    "visual_reviewer": "operator identity", "visual_notes": "camera and materials check",
    "visual_scores": {"composition": 4, "geometry": 4, "readability": 4, "material_camera": 4},
    "manual_interventions": [],
    "reopen_proofs": {"glb": {"status": "passed", "log_path": "BAS14-01/glb-reimport.log"},
                      "blend": {"status": "passed", "log_path": "BAS14-01/blend-reopen.log"}}
  }]}]
}
```

The example is **incomplete and cannot pass**: a real full 20-case run must provide all 20 rows, actual hashes, and every required artifact. Scene evidence is the raw `studio_scene_inspect` response or its decoded summary; do not fabricate expected facts. If inspection is paginated, collect all pages in a verified merged evidence record (no `has_more=true`). All file paths are relative to the directory containing `run.json`; symlinks, traversal and absolute evidence paths are forbidden. PNG must decode at a basic zlib/chunk level, GLB must have correct header/length, BLEND must have a BLENDER signature, and every artifact must match bytes/SHA-256 and a BAS-13 manifest artifact entry.

For safety cases 17–20, record `status=failed`, the actual `error_code`, nonempty `log_path`, and two JSON files via `before_path` / `after_path`. Each file must include `scene_id`, `scene_revision`, `scene_fingerprint`, full `objects` and `artifact_sha256` (digest of the inventory of protected old output files, including empty inventory). The scorer checks identity across rejection. Case 20 also requires `rollback_proof_path` to a JSON object with equal nonempty `snapshot_sha256` / `restored_sha256`, and `log_path` containing `ROLLBACK_RESTORED`. Collect genuine evidence after an injected failure; do not claim a rollback based on a synthetic failed return.

For reopen cases 01, 02 and 16, retain logs containing `BAS14_REOPEN_OK` from an actual independent Blender 4.5.15 process re-opening the BLEND or re-importing GLB. A marker alone is **not** proof of testing: retain the command, exported file and Blender-reported object/material/camera checks. Document human review, manual edits, retries, prompt deviations, elapsed time and the reason for failures. Do not edit scene manually for a first-attempt pass. Standard setup (scene reset, profile configuration, connecting MCP) is allowed and recorded.

## Scoring, CI and release

```bash
python scripts/bas14_benchmark.py validate --suite benchmarks/bas14_suite.json
python scripts/bas14_benchmark.py grade --suite benchmarks/bas14_suite.json \
  --run /private/bas14-evidence/run.json --output /private/bas14-evidence/report.json
pytest -q tests/test_bas14_benchmark.py
```

- First attempt only; **one pass maximum per prompt**. Additional retries are counted and disclosed, never added as more samples or used to replace the primary result. Missing case/evidence = fail. Any first-attempt manual scene/model intervention disqualifies.
- Modeling requires exact expected objects/types/collections, active camera if specified, required artifacts and manifest, all visual rubric dimensions >=4/5 with identified reviewer and notes, and independent reopen logs when required. Visual scores must be assessed by a human from the PNG/GUI, not inferred from file existence.
- Mandatory safety cases 17–20 are an **independent 4/4 gate**; overall benchmark requires **>=16/20** first-attempt passes AND **4/4** safety first-attempt passes. Missing reports or failing gates make grader exit nonzero. These tests are still evidence checking, not a cryptographic attestation of a human reviewer or true GUI behavior.
- CI can verify corpus schema, grader unit tests and integration smoke, but **cannot claim 16/20** until all actual prompt sessions and human visual review happen. Release requires exact-head CI, independent artifact inspection, real Blender GUI client walkthrough, PR review, accepted limitations, owner approval, then merge. Do not tag before those gates.

## Modeling MVP candidate — release notes (draft, unpublished)

Scope: typed MCP modeling/editing via Blender, stable ID/inspect, guarded snapshot/restore, PBR/material/modifier/Geometry Nodes presets, camera/world/light configuration, isolated still rendering, GLB/BLEND export with SHA-256 manifests. Only Linux local Blender 4.5.15 source checkout is currently covered for atomic overwrite. Limitations: manual desktop GUI UAT may need explicit exception; no Image-to-3D, Web UI, remote GPU workers, arbitrary Python sandbox, cross-OS atomic export, or fully packaged wheel renderer. Install from the fork checkout using `uv sync --frozen` and `uv run --frozen mcp-for-blender install-addon`; do not install an unreviewed PyPI build. A semver release/tag is withheld until benchmark, security and full release gates pass.

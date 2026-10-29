# BAS-13 — Isolated still render, GLB/BLEND export and manifest

## Current implementation boundary

The BAS-13 adapter handles `studio_render_still` and `studio_export_artifacts` in `studio-local` and `developer-local` only, with an explicit trusted sandbox root and pinned headless Blender executable. Operations create a verified BAS-12 scene snapshot before queueing a durable SQLite job; workers copy that immutable BLEND into private staging and execute a fixed Blender CLI `scripts/bas13_blender_job.py` (no model-supplied argv, shell, Python expression, external network, or user-path inside the subprocess). The interactive Blender GUI continues handling MCP requests. A failed or cancelled child cannot publish partial output.

### Configuration

- `BLENDER_MCP_PROFILE=studio-local`
- `BLENDER_MCP_STUDIO_ROOT=/absolute/private/studio-sandbox` — owner-only writable, canonical directory, no shared or world-writable access
- `BLENDER_MCP_RENDER_BIN=/absolute/trusted/blender` — verified Blender 4.5.15+ executable on the local machine
- Optional `BLENDER_MCP_SOURCE_COMMIT` — exact 40/64-character hex source SHA; defaults to pinned checkout `git rev-parse HEAD`; unavailable provenance fails closed
- Renderer requires the installed source checkout including `scripts/bas13_blender_job.py`. Pip-only wheel support is not yet accepted.

### Typed request examples

Use Studio v1 UUIDs/revisions returned by `studio_scene_inspect`. The `output_uri` is a **new directory** within the sandbox, not an existing file or shared directory. This permits a complete directory rename only after all outputs are validated.

```json
{"operation":"studio_render_still","request_id":"00000000-0000-4000-8000-000000000010","scene_id":"00000000-0000-4000-8000-000000000001","scene_revision":5,"idempotency_key":"render:shot1","effect_scope":"file","width":640,"height":360,"samples":16,"timeout_seconds":120,"output_uri":"file:///absolute/private/studio-sandbox/shot1"}
```

```json
{"operation":"studio_export_artifacts","request_id":"00000000-0000-4000-8000-000000000011","scene_id":"00000000-0000-4000-8000-000000000001","scene_revision":5,"idempotency_key":"export:model1","effect_scope":"file","formats":["glb","blend"],"object_ids":["00000000-0000-4000-8000-000000000020"],"output_uri":"file:///absolute/private/studio-sandbox/model1"}
```

The request returns `status=accepted` and `job_id`. Poll `studio_artifact_job_status(job_id)` (states queued/running/succeeded/failed/cancelled/interrupted_unknown, progress and terminal manifest path). `studio_artifact_job_cancel(job_id)` requests process termination; do not report cancellation confirmed until subprocess exit and `stop_confirmed`. On process restart uncertain queued/running jobs are `interrupted_unknown`, never automatically retried.

### Artifacts and validation

A successful new output directory contains `still.png` for still renders or `scene.glb` and/or `scene.blend` for exports, plus `render-details.json` and `manifest.json`. Each manifest follows the strict BAS-03 `ArtifactManifest` schema: job UUID, output file URI, size, SHA-256, format, source commit, scene UUID/revision, Blender/worker versions, input snapshot SHA. `render-details.json` captures active camera, engine (CPU Cycles), samples and output resolution, or exported object UUIDs/formats. The worker verifies PNG signature/dimensions/chunk CRC/zlib stream, GLB header and byte length, BLEND magic and all files' byte sizes and SHA before publishing. Blender GUI/main process is never used to render CPU samples.

### Security and resource limits

- Render: width/height 32–4096, Cycles samples 1–128, timeout <= 3600 seconds; no raw Blender CLI injection.
- Export: 1–200 distinct UUIDs, only GLB/BLEND formats and recognized mesh/curve/text geometries.
- Scene snapshot: immutable BAS-12 verification + 512 MiB upper bound; artifact output cap 512 MiB.
- All destination paths must be fresh and inside the private sandbox; existing destinations and symlinks fail closed. **Overwrites are not implemented**, even with a caller token. A distinct resource-bound confirmation and verified backup/rollback protocol are required before this acceptance item can pass.
- Status is durable in a private SQLite table; logs are stored under `.bas13-worker/logs/`. Failed jobs retain logs and do not publish a delivery directory.

## Verification

`pytest -q tests/test_bas13_artifact_worker.py` covers success/replay, failure/no partial files, owner isolation, changed-payload conflict and cancel. `scripts/bas13_artifact_e2e.py` tests pinned Blender real PNG render + GLB/BLEND export, validated manifests, independent Blender GLB re-import and BLEND reopen with original PBR geometry and active camera, as well as a missing-ID failure. The same PR additionally tests typed requests through the live MCP client → authenticated Blender GUI → headless subprocess path on Xvfb.

Run CI on the final PR head and inspect the downloadable `bas13-evidence` directory and `bas05-mcp-gui` E2E JSON. Do not claim release acceptance based on a unit-test-only run or the background golden alone.

## Open gates before Done

Confirmed safe overwrite is not implemented. Independent GUI job tests and artifacts on the final head need green CI and review. Browser/web remote export, GPU render and fully packaged-wheel worker path are explicitly outside this initial local P2 scope.

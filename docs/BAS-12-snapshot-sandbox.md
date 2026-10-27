# BAS-12 — Snapshot sandbox implementation (in progress)

Spec: https://app.notion.com/p/40515de298eb815e9c26f1045b8fe1a6

## Implemented in this branch

- Standard library `SnapshotStore`: configured canonical private root, `O_NOFOLLOW` + dirfd access for snapshot directories, immutable UUID subfolders and exclusive SHA-256 manifest, checksum verification and corruption detection.
- Deterministic idempotency reservation binds scene UUID/revision, operation label and key. Retry after completed snapshot replays the same snapshot; if a BLEND exists but manifest is missing, returns an unknown-state failure rather than overwriting it.
- `studio_scene_snapshot` typed MCP tool: gateway enforces BAS-04 file policy, allocates the private output, calls dedicated Blender main-thread `bpy.ops.wm.save_as_mainfile(copy=True)` handler and verifies bytes and checksum before returning a snapshot UUID.
- Blender addon independently checks that save destination is a private pre-allocated UUID path below `BLENDER_MCP_STUDIO_ROOT`. Root and package-bundled addons remain byte-for-byte identical.
- `studio_scene_restore`: validates signed short-lived operation/scene/revision/snapshot-bound confirmation at gateway AND add-on, creates and verifies a new pre-restore backup, checks source manifest/checksum, imports scene datablocks through Blender libraries and switches the active GUI scene without unloading the socket add-on. Add-on records spent confirmation nonces with exclusive files.
- Python path corruption and symlink tests plus Blender 4.5.15 GUI MCP snapshot/replay → separate headless Blender reopen acceptance were added.

## Environment

Set `BLENDER_MCP_STUDIO_ROOT` to an existing canonical, private directory (mode 0700); add-on and MCP must share filesystem and setting. For restore, additionally provide a securely shared 32+ byte `BLENDER_MCP_CONFIRM_SECRET` environment secret and issue signed confirmation from a trusted UI/backend only. No file operation is advertised to observer. Not a sandbox for developer-local arbitrary Python.

## Not yet implemented / acceptance blockers

The task is **not Done** until safe non-overwrite/overwrite save, open-mainfile load-handler reconnect (or documented equivalent), failure rollback under injected faults and deletion/overwrite in BAS-07 are verified. `studio_scene_restore` has a guarded implementation but is **not yet runtime-accepted**. `studio_scene_open`, `studio_scene_save` and overwrite are intentionally NOT exposed until those gates are satisfied. No model-facing confirmation issuer exists: a trusted UI/backend must explicitly issue a token using BAS-04's signed confirmation protocol. Manual end-user restore UX is deferred.

Main-thread work can still block on very large .blend serialization. GUI/Xvfb and independent human desktop review remain separate. Verify full Actions CI on the actual reviewed head before merge.

## Test commands

`uv run --frozen --python 3.11.14 --with pytest==8.4.2 pytest -q`; repeat with 3.12.14. PR CI also runs `scripts/bas05_mcp_e2e.py` against Blender 4.5.15 Xvfb, then `scripts/bas12_snapshot_reopen.py` headless. Evidence under `bas02-bas06-blender-evidence` archive (snapshot and manifest in `bas05-mcp-gui/studio-sandbox`).

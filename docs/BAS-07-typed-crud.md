# BAS-07 — Typed object CRUD (non-destructive phase)

As of 2026-10-27. Notion: https://app.notion.com/p/40515de298eb81cf9082c689a00c7aa7
Draft PR #7. Dependency BAS-12 remains **Backlog**, so this implementation is **not accepted and not merge-ready**.

## Implemented

- `studio_create_object` now dispatches both strict primitive and text variants. Text produces a native Blender FONT curve.
- New `studio_set_transform` supports `transform`, `rename` and `duplicate`, resolved by `_bas05_object_id` instead of name.
- New `studio_manage_collection` supports `create`, `rename` and `move_object`, using `_bas07_collection_id` metadata.
- Strict finite and magnitude bounds on coordinates, rotations, scales; positive scales required. No generic Python, eval, or raw code routed through Studio.
- Scene revision/idempotency record checked before mutation, with replay protection and conflicting-key rejection; collision-safe object lookup rejects UUID ambiguity.
- Studio profile policy is enforced at MCP list/call and at authenticated Blender socket ingress; observer cannot mutate. Root and bundled add-on code are identical.
- No `studio_delete_object` handler or advertised capability. Delete and overwrite are blocked pending BAS-12 trusted confirmations, persisted snapshots and restoration.

## Validation gates

CI runs Python 3.11.14 / 3.12.14 pytest + scoped ruff + compile; Blender 4.5.15 PR golden job runs GUI MCP E2E. `scripts/bas05_mcp_e2e.py` now issues ten additional typed BAS-07 operations and validates text, rename-preserved UUID, duplicate UUID, collection moves, scene revisions, idempotent replays, and negative inputs. CI results must be attached and independently reviewed before acceptance. Save-to-BLEND comes from the existing disposable GUI test workspace.

## Known limitations / follow-ups

- **BAS-12 gate:** No destructive delete, overwrite, snapshot/restore integration, rollback on partial mutation, or fresh-scene restore acceptance is claimed. CRUD is incomplete.
- Provisional BAS-05 scene revision does not include arbitrary human GUI edits until BAS-08. Do not use it to authorize destructive operations.
- The in-scene 128-record idempotency cache is bounded and not the durable transaction journal planned for BAS-12/BAS-22.
- Blender collection move links new destination before unlinking old sources but does not provide atomic undo on unforeseen bpy exceptions. Manual repair might be needed.
- User-edited imported objects without an assigned Studio UUID do not become mutable by ID automatically.
- CI GUI Xvfb is not an independent human desktop UAT nor a multi-OS/GPU validation.

## Requirements for Done

Finish BAS-12 and integrate confirm-preview-snapshot-delete-restore; prove snapshot before side effect and rollback/partial-effect reporting; attach reproducible scene diff, saved BLEND, manifest/hash, logs and environment; review green CI and real GUI test; only then merge reviewed HEAD to main and mark Done in Notion.

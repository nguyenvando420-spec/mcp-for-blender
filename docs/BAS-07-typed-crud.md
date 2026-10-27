# BAS-07 — Typed object CRUD with BAS-12 protection

Notion: https://app.notion.com/p/40515de298eb81cf9082c689a00c7aa7
PR #7: https://github.com/nguyenvando420-spec/mcp-for-blender/pull/7
BAS-12 prerequisite implementation: PR #8.

## Implemented

- Primitive and Unicode FONT creation, transform, rename, duplicate and collection create/rename/move use typed v1 envelopes and stable UUIDs. Bound finite coordinate values are enforced at gateway and addon ingress.
- Collection membership and transform changes attempt compensation if Blender raises. Uncertain partial effects are reported as conflicts.
- `studio_delete_object` is available only with the BAS-12 sandbox and 32+ byte trusted confirmation secret. The caller must first inspect and create a current-revision `studio_scene_snapshot`, then obtain a trusted HMAC v2 confirmation bound to operation, scene UUID, scene revision, snapshot UUID and **object UUID** after preview. No token-issuing model-facing MCP tool exists.
- Before delete, gateway confirms the target is in the preview manifest, creates and verifies another immutable pre-delete BLEND snapshot, compares the deterministic scene fingerprints of the approved preview and new backup, and consumes the one-use confirmation. Addon independently verifies token, current revision, both snapshot manifests and object UUID/name; linked/shared-scene objects are denied. Then it removes the object and increments revision. Duplicate requests are denied. Failed/uncertain delete responses report a recovery snapshot ID and partial-effects warning.
- BAS-12 snapshot, restore, guarded open and confirmed atomic save code and regression tests are included in this branch. Root and bundled addons are kept identical.

## Acceptance evidence

The CI pipeline runs Python 3.11.14 / 3.12.14 unit and security tests, build and lint; Blender 4.5.15 Xvfb runs real MCP client/GUI 100 BAS-06 commands, BAS-12 snapshot→mutation→restore→save→overwrite→in-session open and BAS-07 ten typed create/edit/collection operations followed by confirmed snapshot-backed delete and replay rejection. It records BLEND/PNG, manifests, SHA256, environment and logs. Results must be inspected on the final HEAD; a previous green run does not validate new commits.

## Operational/security boundaries

- Trusted UI/backend must issue tokens only after displaying exactly which object/file and scene revision will be affected. Tokens are verified at both gateway and Blender add-on.
- BAS-07 now rejects deletion after common direct Blender GUI edits even when MCP revision remains unchanged: object translate/rename, mesh vertex, material and camera lens changes. An independent Blender main-thread timer (not MCP dispatcher) drives five real GUI-process negative acceptance cases. The bounded scene fingerprint is **not** a universal serializer of modifiers, animation, drivers or all complex material node properties; BAS-08 remains needed to detect arbitrary manual edits. Deletion of unmodeled scene changes must remain operationally gated.
- File operations assume a private, single-writer Linux/POSIX sandbox; symlink, path traversal and checksum negatives are tested. Full cross-process transactional isolation is not claimed.
- If Blender crashes or a socket times out after a side effect, the gateway reports that status may be unknown plus a pre-delete backup. Human-assisted snapshot restore may be required; automatic rollback is not guaranteed.
- GUI-process direct-edit tests are automated outside MCP, not a human clicking in the 3D viewport. Test coverage does not imply independent human desktop UAT or macOS/Windows/GPU support.

## Merge gate

Review security/fault-injection findings, validate final HEAD on Python 3.11/3.12 and Blender GUI, inspect test artifact evidence, verify PR #8 dependency and merge order, and only then consider marking Done and merging to main.

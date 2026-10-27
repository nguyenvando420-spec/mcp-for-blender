# BAS-12 — Snapshot sandbox implementation (in progress)

Spec: https://app.notion.com/p/40515de298eb815e9c26f1045b8fe1a6

## Implemented in this branch

- Standard library `SnapshotStore`: configured canonical private root, `O_NOFOLLOW` + dirfd access for snapshot directories, immutable UUID subfolders and exclusive SHA-256 manifest, checksum verification and corruption detection.
- Deterministic idempotency reservation binds scene UUID/revision, operation label and key. Retry after completed snapshot replays the same snapshot; if a BLEND exists but manifest is missing, returns an unknown-state failure rather than overwriting it.
- `studio_scene_snapshot` typed MCP tool: gateway enforces BAS-04 file policy, allocates the private output, calls dedicated Blender main-thread `bpy.ops.wm.save_as_mainfile(copy=True)` handler and verifies bytes and checksum before returning a snapshot UUID.
- Blender addon independently checks that save destination is a private pre-allocated UUID path below `BLENDER_MCP_STUDIO_ROOT`. Root and package-bundled addons remain byte-for-byte identical.
- `studio_scene_save` (non-overwrite): creates an immutable snapshot then copies it through no-follow directory descriptors into a separate temporary file and atomically publishes with an exclusive filesystem link. Existing output files are never overwritten; no hardlink alias of the immutable source is created.
- Snapshot manifest also carries bounded UUID-to-original-name inventory for typed objects. Library restores preserve these names despite Blender auto-suffixing imported datablocks on collisions.
- `studio_scene_restore`: validates signed short-lived operation/scene/revision/snapshot-bound confirmation at gateway AND add-on, creates and verifies a new pre-restore backup, checks source manifest/checksum, imports scene datablocks through Blender libraries and switches the active GUI scene without unloading the socket add-on. Add-on records spent confirmation nonces with exclusive files.
- Python path corruption and symlink tests plus Blender 4.5.15 GUI MCP snapshot/replay → separate headless Blender reopen acceptance were added.

## Environment

Set `BLENDER_MCP_STUDIO_ROOT` to an existing canonical, private directory (mode 0700); add-on and MCP must share filesystem and setting. For restore, additionally provide a securely shared 32+ byte `BLENDER_MCP_CONFIRM_SECRET` environment secret and issue signed confirmation from a trusted UI/backend only. No file operation is advertised to observer. Not a sandbox for developer-local arbitrary Python.

## Guarded file operations and remaining limitations

- `studio_scene_save` now supports an existing BLEND destination only after
  a trusted resource-bound HMAC confirmation containing the exact canonical
  output URI and approved previous SHA-256. The gateway backs up the prior file
  in an immutable snapshot before using same-directory atomic replacement.
  Confirmation replay is denied. A root-local non-cooperating writer could still
  race a replacement; sandbox roots MUST be private single-writer workspaces.
- `studio_scene_open` verifies the allowed input path/checksum and a separate
  resource-bound confirmation. It requires a same-revision preview snapshot,
  saves a second pre-open snapshot, imports the file's scene into the existing
  GUI Blender process and returns a **new scene UUID and revision**. This avoids
  unloading socket handlers, but it does not provide full Blender UI/load-handler
  semantics of `bpy.ops.wm.open_mainfile`. It is a deliberate in-session import.
- Confirmation issuer APIs in `security_policy.py` are trusted-backend
  utilities; there is NO model-facing token issuing MCP tool. A trusted user
  interface must display the resource/path/checksum and require user approval.
- Both operations use conservative unknown-state and one-time confirmation
  semantics. Retry after uncertain mutation requires human reconciliation;
  no distributed transaction guarantee is claimed.
- No tests on Windows, macOS, GPU rendering or a human interactive desktop
  are claimed. Linux/POSIX `dir_fd` and `O_NOFOLLOW` are explicit requirements.
- BAS-07's confirmed delete/recovery is implemented on separate draft PR #7;
  acceptance of both needs matching integration evidence and final-head review.

## Test commands

`uv run --frozen --python 3.11.14 --with pytest==8.4.2 pytest -q`; repeat with 3.12.14. PR CI also runs `scripts/bas05_mcp_e2e.py` against Blender 4.5.15 Xvfb, then `scripts/bas12_snapshot_reopen.py` headless. Evidence under `bas02-bas06-blender-evidence` archive (snapshot and manifest in `bas05-mcp-gui/studio-sandbox`).

## Release acceptance profile: owner self-acceptance, Linux GUI MCP only

Decision date: 2026-10-27. Owner `nguyenvando420-spec` elected repository-owner
self-acceptance instead of a second independent GitHub reviewer. This **does not**
claim independent approval. Supported acceptance profile is **Linux x86_64,
Blender 4.5.15, private single-writer POSIX workspace, local authenticated MCP
client, explicitly trusted UI-issued HMAC confirmations**.

For this release, `studio_scene_open` means **safe in-session scene import**
via `bpy.data.libraries.load` followed by active scene pointer switch. It
intentionally does **not** call `bpy.ops.wm.open_mainfile` in the running GUI,
does **not** trigger its `load_post` handlers, and does **not** restore UI
layouts, workspaces, add-on preferences, every external data link, or arbitrary
non-scene Blender data. The acceptance test verifies the add-on socket stays
usable, an old-scene MCP command is rejected, a new scene UUID/revision is
returned, and subsequent inspection works without reconnecting stdio. A real
application process restart requires a fresh authenticated MCP handshake and
is **outside this release's active-session scope**. This scoped behavior is
a documented exception to a full-project load-handler/reconnect interpretation,
not a claim that the latter was tested.

Automated Blender GUI testing under Xvfb is the **release acceptance mode**.
Human mouse-driven GUI UAT is explicitly **not performed and not represented as
completed**. The release is restricted to the CI-pinned environment above,
not general desktop, Windows/macOS, multi-user projects, arbitrary scripts,
untrusted file writers, or hard-crash atomic rollback. These environments and
manual acceptance are deferred and must be required before expanding scope.

### Material and camera golden-state acceptance

The test session seeds a marker material on the startup mesh with diffuse RGBA
`(0.125, 0.25, 0.5, 1)`, Principled shader base color matching the diffuse,
metallic `0.22`, roughness `0.41`, and camera focal length `41.25mm`.
After a snapshot, a direct Blender GUI-main-thread timer (not an MCP call)
changes lens, material color, base color, metallic and roughness, while an MCP
command creates a temporary object. Restoring the snapshot must remove that
object and recover **all exact golden numeric values** (tolerance 1e-4).
Independent headless Blender processes reopen both the snapshot and exported
BLEND and repeat numeric comparisons. Saved evidence records expected, mutated,
restored and post-open values, including revisions.

Owner sign-off requires the **final HEAD** to pass Python 3.11/3.12, pinned
Blender 4.5.15 GUI E2E, headless reopen and explicit documented scoped
exemptions. No release merge may be inferred from a prior HEAD's CI.

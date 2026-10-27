# BAS-08 — Scene observation, GUI revision and offscreen capture

Status: **implementation / CI candidate; NOT release accepted**.

Notion: https://app.notion.com/p/40515de298eb819083f2c5b6ead14af2

## Scope

- \`studio_scene_inspect\` is a BAS-03 validated, observer-accessible MCP tool.
  It calls \`studio_scene_inspect_v1\` over the authenticated BAS-06 socket and
  runs only on Blender's GUI main thread.
- \`studio_capture_viewport\` is a read-only Studio tool with a real add-on
  handler. It does not use \`execute_code\`, \`bpy.ops.screen.screenshot_area\`,
  a foreground window grab, the active render camera assignment, selection,
  shading changes, or frame changes.
- A bounded, reproducible scene fingerprint includes the BAS-07 object/material/
  camera digest plus selected objects, active object and current frame. When it
  changes with a previously observed scene ID/revision, the scene revision
  advances. The old idempotency cache is invalidated and stale requests fail.
  Typed BAS-07 commits update this baseline after each committed change.
- Querying by \`object_id\` takes place before pagination; identical UUIDs are
  not silently used as mutation targets. Responses include selection, transforms,
  world-space bounds, collections/materials, name, scene/revision, paging, and
  active frame/camera.
- GPU offscreen viewport or camera-mode captures return PNG bytes as base64 with
  width, height, SHA-256 and the captured scene fingerprint. Gateway independently
  verifies the PNG header, dimensions and SHA. The add-on limits PNGs to 8 MiB,
  uses a temporary internal path, and frees GPU/image resources on all paths.

## Security and accepted limitations

- This is **not** a complete event log or tamper-proof observation. BAS-07's
  fingerprint deliberately excludes many Blender data types such as arbitrary
  modifiers, drivers, node graphs and animations, and caps at 2,500 objects /
  100,000 mesh vertices. Outside that limit, observation fails closed rather
  than asserting that a revision is current.
- \`observer\` reads do not write IDs to scene objects. BAS-07-created objects and
  collections have persistent UUIDs; pre-existing GUI-created objects/materials
  lacking IDs are reported with a null ID. Duplicate IDs require explicit
  remediation. Persistently assigning UUIDs to arbitrary pre-existing objects
  and materials remains an acceptance gap.
- Camera mode requires \`scene.camera\`; both modes require an active VIEW_3D
  region and GPU context. No offscreen capability means a typed actionable
  \`capability_unavailable\` failure, never a black/OS screenshot fallback.
- A fingerprint represents the context observed **at capture time**, not a
  guarantee that Blender will remain unchanged afterward. Clients must supply
  scene ID/revision for consistency and re-inspect on stale errors.
- An internally created temporary Blender image datablock is removed in
  \`finally\`, but failure cleanup and absence of GUI side-effects still require
  Blender 4.5.15 GUI evidence before release.

## Testing and acceptance evidence

Commands run by the existing PR GitHub Actions workflows:

\`\`\`bash
uv run --frozen python -m compileall -q src tests scripts addon.py main.py
uv run --frozen --with pytest==8.4.2 pytest -q
uv run --frozen python scripts/run_golden.py --output-dir ./golden
uv run --frozen python scripts/bas05_mcp_e2e.py --output-dir ./bas08-gui
\`\`\`

The extended Blender 4.5.15/Xvfb MCP client GUI scenario checks:

1. \`inspect -> capture -> inspect\`: PNG SHA-256, dimensions, revision,
   fingerprint, selection and frame remain unchanged; saves \`bas08-viewport.png\`.
2. Repeated primitive create/inspect uses 50 + 50 authenticated MCP calls.
3. Editing camera/material data via Blender GUI timer outside the MCP dispatcher
   advances the revision on the next inspect. BAS-12 restore operates on the
   new revision with immutable snapshot validation.
4. BAS-07 rename preserves UUID, transform and collection operations are typed,
   and five out-of-band GUI edit types bump revision and invalidate previously
   approved delete previews. Confirmed deletion still requires a fresh snapshot.

Additional pytest checks cover invalid input, stale capture, missing viewport,
corrupt dimensions, paging across more than 200 objects, object rename,
duplicate IDs and empty scenes.

**Acceptance gate:** All head-SHA CI jobs must succeed, with the Blender GUI
evidence artifact and log. Human mouse-driven acceptance, material/collection
ID backfill, exhaustive fingerprint coverage and crash/driver/edit detection
are **not claimed**. Never merge this PR until the owner reviews the final
head commit and explicitly accepts the remaining scope or requires fixes.

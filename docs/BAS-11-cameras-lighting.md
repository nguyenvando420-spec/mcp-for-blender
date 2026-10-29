# BAS-11 — Cameras, lights, world and bounds framing

## Scope and trust

BAS-11 exposes the already documented Studio v1 operations `studio_set_camera` and `studio_set_light` through the MCP gateway and the authenticated Blender add-on queue. It does **not** expose arbitrary `bpy` or Python evaluation. The observer profile cannot call either mutation. Each mutation requires the current scene UUID/revision, an idempotency key and the `scene` effect scope. The GUI-side BAS-07 execution wrapper rejects stale context and payload-changing replays. Camera/light IDs use the same persistent `_bas05_object_id` property as other scene objects.

## Camera requests

`studio_set_camera` accepts an optional `camera_id` to update an existing camera; omitting it creates a camera. `active` defaults to true, but creating a secondary camera with `active=false` leaves the active camera unchanged. Use either explicit `location` + `rotation_euler`, `target_id` to frame a specific geometrical object, or `frame_scene=true` to frame all visible scene geometry. Framing and explicit transforms are mutually exclusive. The deterministic isometric framing algorithm evaluates each target's eight world-space bounding-box corners, derives a center, accounts for focal length, sensor width and pixel-aspect-corrected resolution, then computes depth/near/far clipping. It deliberately does not move the 3D viewport, selection, current frame or other geometry.

`lens_mm` is limited to [1, 300], `clip_start` to [0.001, 100], `clip_end` to [0.1, 100000], `framing_margin` to [1, 2], and `resolution_x/y` to [64, 8192]. Both resolution dimensions must be passed together. Missing target IDs and empty/hidden scenes fail before mutation; explicit clipping that excludes the bounds fails closed. Framing does not include occlusion analysis, animated geometry over time, or evaluated geometry modified by Geometry Nodes; those are future improvements.

## Lighting / world

`studio_set_light` has the discriminated `spec.kind`: `light` creates/updates a POINT, SUN, SPOT or AREA lamp; `world` creates a scene-owned node-based background. Light energy is bounded [0, 100000] and RGB channels [0,1]. AREA size is bounded [0.01,100]. World strength is [0,100]. Creating a world does not edit shared worlds; previous worlds remain available to other scenes. Existing linked/shared lights or cameras cannot be updated.

## Acceptance and remaining gates

- Python gateway tests: `pytest -q tests/test_bas11_camera_light.py`, with observer-denial, schema bounds, optional target/frame-scene, scene-diff and capability tests.
- Blender 4.5.15 pinned GUI/job CI: `scripts/bas11_blender_golden.py` builds two cameras, three distinct lights, one world and a bounds-framed target; exercises active camera and negative missing target/invalid clipping; renders the active camera/world to `bas11-preview.png` using CPU Cycles, records both BLEND/PNG SHA-256 hashes, and independently reopens the BLEND and verifies the preview hash in another Blender process.
- CI uploads `bas11-golden` in the existing GUI evidence artifact alongside BAS-10 evidence.
- Manual interactive GUI UAT is distinct from background Blender integration. Offscreen camera-mode viewport capture via the actual MCP/GUI display and desktop mouse-driven UAT remain acceptance requirements (headless Cycles PNG alone does not prove offscreen GPU capture); do not mark Done solely because the headless test passes.
- No dependency on BAS-10 merging is required; BAS-11 branches from the then-current `main`. Merge only after review, green CI on the final head SHA and an accepted GUI evidence scope.

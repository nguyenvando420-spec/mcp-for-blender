"""BAS-13 controlled Blender subprocess job runner.

Only the verified adapter-controlled config file is accepted. Never eval Python
or accept source paths/argv from an MCP caller. This module runs in Blender's
headless child process; the interactive GUI scene is never changed.
"""
import json
from pathlib import Path
import sys

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
if len(args) != 3:
    raise RuntimeError("BAS13 runner expects three trusted job paths")
snapshot, output, config_path = map(Path, args)
spec = json.loads(config_path.read_text(encoding="utf-8"))
output.mkdir(exist_ok=True)
bpy.ops.wm.open_mainfile(filepath=str(snapshot))
scene = bpy.context.scene
kind = spec["kind"]
files = []

if kind == "render":
    width, height, samples = spec["width"], spec["height"], spec["samples"]
    if (not isinstance(width,int) or not 32 <= width <= 4096
            or not isinstance(height,int) or not 32 <= height <= 4096
            or not isinstance(samples,int) or not 1 <= samples <= 128
            or scene.camera is None):
        raise RuntimeError("BAS13 render input invalid or active camera missing")
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(output / "still.png")
    bpy.ops.render.render(write_still=True)
    files = ["still.png"]
    info = {"camera": scene.camera.name, "engine": scene.render.engine,
            "samples": samples, "resolution": [width, height]}
elif kind == "export":
    allowed = set(spec["formats"])
    uuids = spec["object_ids"]
    if not allowed or not allowed <= {"glb","blend"} or not uuids or len(uuids) > 200:
        raise RuntimeError("BAS13 formats or object IDs invalid")
    objects = []
    for oid in uuids:
        matches = [o for o in scene.objects if o.get("_bas05_object_id") == oid]
        if len(matches) != 1 or matches[0].type not in ("MESH","CURVE","FONT"):
            raise RuntimeError("BAS13 expected one exportable object per UUID")
        objects.append(matches[0])
    if "glb" in allowed:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = objects[0]
        bpy.ops.export_scene.gltf(filepath=str(output / "scene.glb"),
                                  export_format="GLB", use_selection=True)
        files.append("scene.glb")
    if "blend" in allowed:
        # Use full scene copy, not a filtered GLB selection. Canonical BLEND
        # output preserves active camera, materials, world and all scene IDs.
        bpy.ops.wm.save_as_mainfile(filepath=str(output / "scene.blend"), copy=True, compress=False)
        files.append("scene.blend")
    info = {"objects_requested": uuids, "formats": sorted(allowed)}
else:
    raise RuntimeError("BAS13 unsupported job kind")

for filename in files:
    p = output / filename
    if not p.is_file() or p.stat().st_size < 64:
        raise RuntimeError("BAS13 missing/empty output")
(output / "run.json").write_text(json.dumps({
    "kind": kind, "files": files, "blender_version": bpy.app.version_string,
    **info}, sort_keys=True),encoding="utf-8")
print("BAS13_CHILD_SUCCESS", kind, len(files), flush=True)

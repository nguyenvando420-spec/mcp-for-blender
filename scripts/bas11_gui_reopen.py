"""BAS-11 independent Blender process: verify the real MCP/Xvfb GUI scene BLEND."""
import sys
from pathlib import Path
import bpy

args = sys.argv[sys.argv.index("--") + 1:]
file_path = Path(args[0])
active_name = args[1]
camera_ids = args[2:4]
light_ids = args[4:7]
assert len(camera_ids) == 2 and len(light_ids) == 3
assert file_path.is_file() and file_path.read_bytes().startswith(b"BLENDER")
bpy.ops.wm.open_mainfile(filepath=str(file_path))
scene = bpy.context.scene
assert scene.camera is not None and scene.camera.name == active_name
assert scene.world is not None and scene.world.use_nodes
background = scene.world.node_tree.nodes.get("Background")
assert background and abs(background.inputs["Strength"].default_value - .8) < 1e-5
assert all(abs(x - y) < 1e-5 for x, y in
           zip(scene.world.color, (.1, .2, .35)))
for kind, ids in (("CAMERA", camera_ids), ("LIGHT", light_ids)):
    for object_id in ids:
        matches = [o for o in scene.objects if o.get("_bas05_object_id") == object_id]
        assert len(matches) == 1 and matches[0].type == kind, (kind, object_id, matches)
lamps = {o.name: o.data for o in scene.objects if o.get("_bas05_object_id") in light_ids}
for name, energy, light_type in (("BAS11_GUI_AREA", 450, "AREA"),
                                 ("BAS11_GUI_POINT", 90, "POINT"),
                                 ("BAS11_GUI_SUN", 2, "SUN")):
    assert name in lamps, name
    assert lamps[name].type == light_type and abs(lamps[name].energy - energy) < 1e-4, name
assert scene.camera.data.lens == 45
assert scene.camera.data.clip_start < scene.camera.data.clip_end
print("BAS11_GUI_REOPEN_PASS")

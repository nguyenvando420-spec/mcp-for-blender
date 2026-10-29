"""BAS-11 Blender 4.5.15 headless golden: camera/light/world, framing, save/reopen."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
out = Path(args[args.index("--output-dir") + 1])
out.mkdir(parents=True, exist_ok=True)
repo = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bas11_addon", repo / "addon.py")
module = importlib.util.module_from_spec(spec)
sys.modules["bas11_addon"] = module
spec.loader.exec_module(module)
server = module.BlenderMCPServer()
server.profile = "developer-local"
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

def context():
    return server._studio_scene_context_v1()[1:]

def invoke(kind, **kw):
    sid, rev = context()
    cmd = getattr(server, kind)
    reply = cmd(sid, rev, str(uuid4()), **kw)
    assert reply["scene_id"] == sid and reply["scene_revision"] == rev + 1, reply
    return reply

if "--reopen" in args:
    file = out / "bas11.blend"
    bpy.ops.wm.open_mainfile(filepath=str(file))
    scene = bpy.context.scene
    cams = sorted((o for o in scene.objects if o.type == "CAMERA"), key=lambda o: o.name)
    lights = sorted((o for o in scene.objects if o.type == "LIGHT"), key=lambda o: o.name)
    assert len(cams) == 2 and len(lights) == 3, (cams,lights)
    assert scene.camera is not None and scene.camera.get("_bas05_object_id")
    assert all(o.get("_bas05_object_id") for o in cams + lights)
    assert scene.world and scene.world.use_nodes
    bg = scene.world.node_tree.nodes.get("Background")
    assert abs(bg.inputs["Strength"].default_value - 0.65) < 1e-5
    assert scene.render.resolution_x == 960 and scene.render.resolution_y == 540
    report = json.loads((out / "bas11.json").read_text())
    for entry in report["objects"]:
        obj = next(o for o in scene.objects if o.get("_bas05_object_id") == entry["id"])
        assert obj.name == entry["name"]
    assert report["active_camera"] == scene.camera.name
    preview = out / "bas11-preview.png"
    assert preview.read_bytes().startswith(bytes.fromhex("89504e470d0a1a0a"))
    assert hashlib.sha256(preview.read_bytes()).hexdigest() == report["png_sha256"]
    assert report["preview_camera"] == scene.camera.name
    assert report["preview_world"] == scene.world.name
    report["reopen_ok"] = True
    (out / "bas11-reopen.json").write_text(json.dumps(report, indent=2))
    print("BAS11_REOPEN_PASS")
    raise SystemExit(0)

bpy.ops.mesh.primitive_cube_add(size=2, location=(0,0,0))
cube = bpy.context.object
cube.name = "BAS11_Target"
cube["_bas05_object_id"] = str(uuid4())
first = invoke("studio_set_camera_v1", name="BAS11_Camera_A",
               target_id=cube["_bas05_object_id"], lens_mm=50,
               resolution_x=960,resolution_y=540)
cam = bpy.data.objects[first["name"]]
assert scene.camera == cam and cam.data.clip_start < cam.data.clip_end
second = invoke("studio_set_camera_v1", name="BAS11_Camera_B",
                location=[5,-8,5],rotation_euler=[1,0,0],lens_mm=35,active=False)
assert scene.camera == cam
invoke("studio_set_camera_v1",camera_id=second["object_id"],active=True)
assert scene.camera.name == second["name"]
invoke("studio_set_camera_v1",camera_id=first["object_id"],active=True)
assert scene.camera.name == first["name"]
for typ,loc,power in (("AREA",[0,-4,6],350),("POINT",[4,2,2],200),("SUN",[-2,3,7],1.5)):
    invoke("studio_set_light_v1",spec={"kind":"light","light_id":None,"name":f"BAS11_{typ}",
           "light_type":typ,"energy":power,"color":[1,.9,.8],"location":loc,
           "rotation_euler":None,"size":2 if typ=="AREA" else None})
invoke("studio_set_light_v1",spec={"kind":"world","color":[.1,.15,.25],"strength":.65})
assert len([o for o in scene.objects if o.type=="LIGHT"]) == 3
before=(cube.location.copy(),cube.rotation_euler.copy(),cube.scale.copy(),scene.frame_current)
# Errors must not mutate the scene.
for options in ({"target_id":str(uuid4())},{"frame_scene":True,"clip_end":.1}):
    try:
        invoke("studio_set_camera_v1",camera_id=first["object_id"],**options)
    except ValueError as e:
        assert "object_not_found:" in str(e) or "invalid_input:" in str(e)
    else:
        raise AssertionError("Expected fail-closed framing")
assert (cube.location.copy(),cube.rotation_euler.copy(),cube.scale.copy(),scene.frame_current)==before
objects=[{"id":o["_bas05_object_id"],"name":o.name} for o in scene.objects if o.type in ("CAMERA","LIGHT")]
# Render the actual active-camera/world composition with CPU Cycles.
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 8
scene.render.resolution_x = 320
scene.render.resolution_y = 180
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = str(out / "bas11-preview.png")
preview_camera, preview_world = scene.camera.name, scene.world.name
bpy.ops.render.render(write_still=True)
preview = out / "bas11-preview.png"
assert preview.read_bytes().startswith(bytes.fromhex("89504e470d0a1a0a"))
assert preview.stat().st_size > 3000
png_sha = hashlib.sha256(preview.read_bytes()).hexdigest()
scene.render.resolution_x, scene.render.resolution_y = 960, 540
assert scene.camera.name == preview_camera and scene.world.name == preview_world
bpy.ops.wm.save_as_mainfile(filepath=str(out / "bas11.blend"))
sha=hashlib.sha256((out / "bas11.blend").read_bytes()).hexdigest()
(out / "bas11.json").write_text(json.dumps({"blender":bpy.app.version_string,
    "active_camera":scene.camera.name,"objects":objects,"world":scene.world.name,
    "blend_sha256":sha,"png_sha256":png_sha,
    "preview_camera":preview_camera,"preview_world":preview_world,
    "reopen_ok":False},indent=2))
print("BAS11_GOLDEN_PASS",sha)

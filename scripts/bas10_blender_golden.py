"""BAS-10 pinned Blender real-handler golden scene and independent reopen."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import uuid

import bpy
from mathutils import Vector


def verify(path):
    scene = bpy.context.scene
    values = {}
    for name in ("BAS10_Red", "BAS10_Blue", "BAS10_Green"):
        obj = bpy.data.objects.get(name)
        assert obj is not None and obj.type == "MESH", name
        mat = obj.data.materials[-1]
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        assert bsdf and mat.get("_bas08_material_id"), name
        values[name] = {
            "color": list(bsdf.inputs["Base Color"].default_value),
            "metallic": float(bsdf.inputs["Metallic"].default_value),
            "roughness": float(bsdf.inputs["Roughness"].default_value),
        }
    red = bpy.data.objects["BAS10_Red"]
    blue = bpy.data.objects["BAS10_Blue"]
    green = bpy.data.objects["BAS10_Green"]
    assert any(m.type == "BEVEL" and m.segments == 3 for m in red.modifiers)
    assert any(m.type == "SUBSURF" and m.levels == 2 for m in blue.modifiers)
    nodes = [m for m in green.modifiers if m.type == "NODES"]
    assert nodes and nodes[-1].node_group
    lines = [n for n in nodes[-1].node_group.nodes if n.bl_idname == "GeometryNodeMeshLine"]
    assert len(lines) == 1 and lines[0].inputs["Count"].default_value == 5
    assert tuple(round(float(x), 4) for x in lines[0].inputs["Offset"].default_value) == (2, 0, 0)
    assert bpy.data.objects["BAS10_Red"].data.materials[-1]["bas10_texture_sha256"]
    assert any(image.packed_file for image in bpy.data.images), "Texture not packed"
    assert len({tuple(v["color"]) for v in values.values()}) == 3
    # Verify actual evaluated geometry, not merely the node definitions.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_counts = {}
    for obj in (red, blue, green):
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        assert mesh is not None
        evaluated_counts[obj.name] = len(mesh.vertices)
        evaluated.to_mesh_clear()
    assert evaluated_counts["BAS10_Red"] > 8, evaluated_counts
    assert evaluated_counts["BAS10_Blue"] > 8, evaluated_counts
    assert evaluated_counts["BAS10_Green"] >= 40, evaluated_counts
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reopen", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    assert bpy.app.version_string.split()[0] == "4.5.15", bpy.app.version_string
    if args.reopen:
        assert bpy.data.filepath == str(out / "bas10.blend"), "Independent Blender must actually load saved BLEND"
        assert not (out / "bas10-local.png").exists(), "Original texture path must be absent at reopen"
        values = verify(out / "bas10.blend")
        before = json.loads((out / "bas10-before.json").read_text())
        assert values == before, (values, before)
        (out / "bas10-reopen.json").write_text(json.dumps({"passed": True, "values": values}))
        return
    import os
    os.environ["BLENDER_MCP_STUDIO_ROOT"] = str(out)
    module_file = Path(__file__).resolve().parents[1] / "addon.py"
    spec = importlib.util.spec_from_file_location("bas10_addon", module_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.BlenderMCPServer(profile="studio-local")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    _, sid, rev = server._studio_scene_context_v1()
    import base64
    asset = out / "bas10-local.png"
    # Tiny valid 1x1 PNG, self-supplied, no external license dependency.
    asset.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lYcAAAAASUVORK5CYII="))
    sha = hashlib.sha256(asset.read_bytes()).hexdigest()
    for index, (name, color) in enumerate((
            ("BAS10_Red", (0.8, 0.1, 0.1, 1)),
            ("BAS10_Blue", (0.1, 0.2, 0.9, 1)),
            ("BAS10_Green", (0.1, 0.8, 0.2, 1)))):
        bpy.ops.mesh.primitive_cube_add(location=(index * 3.5 - 3.5, 0, 1))
        obj = bpy.context.object
        obj.name = name
        obj["_bas05_object_id"] = str(uuid.uuid4())
        sid, rev = server._studio_scene_context_v1()[1:]
        payload = {"kind": "pbr", "base_color": color, "metallic": index * 0.3,
                   "roughness": 0.2 + index * 0.2}
        if index == 0:
            payload.update(texture_uri=asset.as_uri(), texture_sha256=sha)
        response = server.studio_assign_material_v1(
            sid, rev, "bas10-pbr-" + str(index), obj["_bas05_object_id"], payload)
        assert response["scene_revision"] == rev + 1
        oid = obj["_bas05_object_id"]
        sid, rev = server._studio_scene_context_v1()[1:]
        if index == 0:
            variant = {"kind": "bevel", "width": 0.12, "segments": 3}
        elif index == 1:
            variant = {"kind": "subdivision", "levels": 2}
        else:
            variant = {"kind": "geometry_nodes", "preset": "linear_array", "count": 5,
                       "offset": [2, 0, 0]}
        server.studio_assign_material_v1(sid, rev, "bas10-mod-" + str(index), oid, variant)
    # CPU-only, camera-based color preview with real geometry evaluations.
    bpy.ops.object.camera_add(location=(9, -16, 12))
    camera = bpy.context.object
    camera.rotation_euler = (Vector((2, 0, 1)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 22
    scene.camera = camera
    bpy.ops.object.light_add(type="AREA", location=(0, -4, 11))
    bpy.context.object.data.energy = 2100
    bpy.context.object.data.shape = "DISK"
    bpy.context.object.data.size = 10
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    for view_layer in scene.view_layers:
        if hasattr(view_layer, "cycles"):
            view_layer.cycles.use_denoising = False
    scene.render.resolution_x = 480
    scene.render.resolution_y = 270
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "Standard"
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(out / "bas10-preview.png")
    values = verify(out / "bas10.blend")
    (out / "bas10-before.json").write_text(json.dumps(values))
    bpy.ops.wm.save_as_mainfile(filepath=str(out / "bas10.blend"))
    bpy.ops.render.render(write_still=True)
    preview = out / "bas10-preview.png"
    assert preview.read_bytes().startswith(bytes.fromhex("89504e470d0a1a0a"))
    assert preview.stat().st_size > 4000, "Blank/missing render preview"
    # Remove the original file at its source URI: a reopened BLEND must use
    # the packed texture, not silently succeed because the local file survived.
    asset.rename(out / "bas10-source-archived.png")
    (out / "bas10-environment.json").write_text(json.dumps(
        {"blender": bpy.app.version_string, "python": sys.version, "mode": "headless",
         "render_engine": "CYCLES", "samples": scene.cycles.samples,
         "texture_sha256": sha, "texture_license": "self-generated-test-image",
         "texture_source": "generated in this test; archived after save"}))
    cmd = [bpy.app.binary_path, "--background", "--factory-startup",
           "--disable-autoexec", "--python-exit-code", "1", str(out / "bas10.blend"),
           "--python", str(Path(__file__).resolve()),
           "--", "--output-dir", str(out), "--reopen"]
    subprocess.run(cmd, check=True, timeout=120)
    files = ["bas10.blend", "bas10-preview.png", "bas10-before.json", "bas10-reopen.json",
             "bas10-environment.json", "bas10-source-archived.png"]
    (out / "bas10-checksums.json").write_text(json.dumps({
        name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in files
    }, indent=2))


if __name__ == "__main__":
    main()

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
    values = verify(out / "bas10.blend")
    (out / "bas10-before.json").write_text(json.dumps(values))
    bpy.ops.wm.save_as_mainfile(filepath=str(out / "bas10.blend"))
    bpy.context.scene.render.engine = "BLENDER_EEVEE_NEXT"
    bpy.context.scene.render.resolution_x = 320
    bpy.context.scene.render.resolution_y = 180
    bpy.context.scene.render.resolution_percentage = 100
    bpy.context.scene.render.image_settings.file_format = "PNG"
    # Viewport does not require user-facing external assets or a full camera scene.
    (out / "bas10-environment.json").write_text(json.dumps(
        {"blender": bpy.app.version_string, "python": sys.version, "mode": "headless"}))
    cmd = [bpy.app.binary_path, "--background", "--factory-startup",
           "--disable-autoexec", "--python-exit-code", "1", "--python",
           str(Path(__file__).resolve()), "--", "--output-dir", str(out), "--reopen"]
    subprocess.run(cmd, check=True, timeout=120)
    files = ["bas10.blend", "bas10-before.json", "bas10-reopen.json",
             "bas10-environment.json", "bas10-local.png"]
    (out / "bas10-checksums.json").write_text(json.dumps({
        name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in files
    }, indent=2))


if __name__ == "__main__":
    main()

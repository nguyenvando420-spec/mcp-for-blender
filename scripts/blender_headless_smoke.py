"""Create a minimal Blender scene and render a CPU PNG + .blend.

Run with:
  blender --background --factory-startup --python scripts/blender_headless_smoke.py -- --output-dir /tmp/blender-baseline

This file is executed by Blender's Python interpreter, not by system Python.
"""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-version")
    args = parser.parse_args(argv)
    if args.expected_version and bpy.app.version_string.split()[0] != args.expected_version:
        raise RuntimeError(f"Expected Blender {args.expected_version}, got {bpy.app.version_string}")
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
    cube = bpy.context.object
    cube.name = "BAS01_BaselineCube"
    material = bpy.data.materials.new("BAS02_GoldenMaterial")
    material.use_nodes = True
    shader = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    shader.inputs["Base Color"].default_value = (0.04, 0.25, 0.65, 1)
    shader.inputs["Roughness"].default_value = 0.35
    cube.data.materials.append(material)

    bpy.ops.object.camera_add(location=(5, -7, 5))
    camera = bpy.context.object
    camera.name = "BAS02_GoldenCamera"
    target = Vector((0, 0, 1))
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()

    bpy.ops.object.light_add(type="AREA", location=(2, -3, 6))
    bpy.context.object.data.energy = 800
    bpy.context.object.name = "BAS02_GoldenLight"

    scene = bpy.context.scene
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    # Ubuntu's Blender packages can be built without OpenImageDenoiser.
    # Disable Cycles per-view-layer denoising for portable CPU headless CI.
    for view_layer in scene.view_layers:
        if hasattr(view_layer, "cycles"):
            view_layer.cycles.use_denoising = False
    scene.render.resolution_x = 320
    scene.render.resolution_y = 180
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(output_dir / "preview.png")

    project_path = output_dir / "baseline.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(project_path))
    bpy.ops.render.render(write_still=True)
    (output_dir / "blender-environment.json").write_text(json.dumps({
        "version": bpy.app.version_string,
        "build_hash": bpy.app.build_hash.decode(),
        "build_date": bpy.app.build_date.decode(),
        "build_platform": bpy.app.build_platform.decode(),
        "python_bpy": platform.python_version(),
        "render_engine": scene.render.engine,
        "render_device": scene.cycles.device,
        "resolution": [320, 180],
        "samples": scene.cycles.samples,
    }, indent=2) + "\n")

    preview = output_dir / "preview.png"
    assert project_path.is_file() and project_path.stat().st_size > 0
    assert preview.is_file() and preview.stat().st_size > 0
    print("BAS01_SMOKE_OK " + json.dumps({
        "blender_version": bpy.app.version_string,
        "blend_file": str(project_path),
        "preview": str(preview),
        "objects": len(scene.objects),
        "render_engine": scene.render.engine,
    }))


if __name__ == "__main__":
    main()

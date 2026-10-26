"""Reopen golden .blend in a fresh Blender process and inspect saved data + PNG."""
import argparse
import json
from pathlib import Path
import sys

import bpy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    assert bpy.app.version_string.split()[0] == args.expected_version
    output = args.output_dir
    bpy.ops.wm.open_mainfile(filepath=str(output / "baseline.blend"), use_scripts=False)
    scene = bpy.context.scene
    assert set(scene.objects) == {bpy.data.objects[name] for name in (
        "BAS01_BaselineCube", "BAS02_GoldenCamera", "BAS02_GoldenLight")}
    cube = scene.objects["BAS01_BaselineCube"]
    assert cube.type == "MESH" and len(cube.data.vertices) == 8
    assert tuple(cube.location) == (0.0, 0.0, 1.0)
    assert scene.camera == scene.objects["BAS02_GoldenCamera"]
    assert scene.camera.type == "CAMERA"
    assert scene.objects["BAS02_GoldenLight"].data.energy == 800
    material = cube.data.materials[0]
    assert material.name == "BAS02_GoldenMaterial" and material.use_nodes
    shader = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    assert all(abs(a - b) < 1e-5 for a, b in zip(
        shader.inputs["Base Color"].default_value, (0.04, 0.25, 0.65, 1)))
    assert abs(shader.inputs["Roughness"].default_value - 0.35) < 1e-5
    assert scene.render.engine == "CYCLES" and scene.cycles.device == "CPU"
    assert (scene.render.resolution_x, scene.render.resolution_y,
            scene.render.resolution_percentage) == (320, 180, 100)
    image = bpy.data.images.load(str(output / "preview.png"), check_existing=False)
    assert tuple(image.size) == (320, 180)
    pixels = list(image.pixels)
    rgb = [value for index, value in enumerate(pixels) if index % 4 != 3]
    assert max(rgb) - min(rgb) > 0.05, "PNG is blank or nearly uniform"
    assert sum(value > 0.02 for value in rgb) > len(rgb) * 0.01, "PNG is empty/black"
    result = {"passed": True, "reopened": True, "objects": sorted(o.name for o in scene.objects),
              "camera": scene.camera.name, "material": material.name,
              "png_size": list(image.size), "pixel_range": [min(rgb), max(rgb)]}
    (output / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print("BAS02_REOPEN_OK " + json.dumps(result))


if __name__ == "__main__":
    main()

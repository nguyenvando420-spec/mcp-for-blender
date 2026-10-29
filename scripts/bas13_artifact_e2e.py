"""BAS-13 pinned Blender artifact integration: real CPU render, GLB, BLEND, manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from blender_mcp.studio.artifact_worker import ArtifactWorker
from blender_mcp.studio.contracts import ArtifactManifest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def await_job(worker, row, timeout=160):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = worker.get(row["job_id"], row["owner_id"])
        if result["state"] in ("succeeded", "failed", "cancelled", "interrupted_unknown"):
            return result
        time.sleep(.1)
    raise AssertionError("Timed out waiting for isolated artifact subprocess")


def inspect_manifest(path, job_id, expected_kinds):
    result = ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))
    assert str(result.job_id) == job_id
    assert {obj.kind for obj in result.artifacts} == set(expected_kinds) | {"other"}
    for item in result.artifacts:
        file = path.parent / Path(item.uri).name
        assert item.uri == file.as_uri()
        assert item.bytes == file.stat().st_size
        assert item.sha256 == sha(file)
        assert item.provenance.scene_revision >= 0
        assert item.provenance.blender_version.startswith("4.5.15")
        assert item.provenance.source_commit == COMMIT
    return result


parser = argparse.ArgumentParser()
parser.add_argument("--golden", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
out = args.output_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
root = out / "private-studio"
root.mkdir(mode=0o700, exist_ok=True)
root.chmod(0o700)
repo = Path(__file__).resolve().parents[1]
blender = repo / ".blender-runtime/runtime/blender-4.5.15-linux-x64/blender"
runner = repo / "scripts/bas13_blender_job.py"
COMMIT = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
source = root / "golden-source.blend"
shutil.copyfile(args.golden / "bas11.blend", source)
target = json.loads((args.golden / "bas11.json").read_text())["target_id"]
scene_id = str(uuid4())
worker = ArtifactWorker(root, blender, runner, COMMIT)
render_params = dict(owner_id="studio", request_id=str(uuid4()), idempotency_key="preview-1",
                     scene_id=scene_id, scene_revision=2, snapshot=source,
                     output=root / "render-delivery", spec={"kind": "render",
                       "width": 320, "height": 180, "samples": 8}, timeout_seconds=120)
r0 = worker.submit(**render_params)
render = await_job(worker, r0)
assert render["state"] == "succeeded", render
r1 = worker.submit(**{**render_params, "request_id": str(uuid4())})
assert r1["job_id"] == r0["job_id"], (r1, r0)
png = root / "render-delivery/still.png"
assert png.stat().st_size > 2000
render_manifest = inspect_manifest(root / "render-delivery/manifest.json",
                                   render["job_id"], {"png"})
meta = json.loads((root / "render-delivery/render-details.json").read_text())
assert meta["camera"] == "BAS11_Camera_A"
assert meta["engine"] == "CYCLES"
assert meta["samples"] == 8 and meta["resolution"] == [320, 180]
export_params = dict(owner_id="studio",request_id=str(uuid4()),idempotency_key="export-1",
                     scene_id=scene_id,scene_revision=2,snapshot=source,
                     output=root / "export-delivery",
                     spec={"kind": "export", "formats": ["glb","blend"],
                           "object_ids": [target]},timeout_seconds=120)
e0 = worker.submit(**export_params)
export = await_job(worker, e0)
assert export["state"] == "succeeded", export
export_manifest = inspect_manifest(root / "export-delivery/manifest.json",
                                   export["job_id"], {"glb","blend"})
import_result = subprocess.run([
    str(blender), "--background", "--factory-startup",
    "--disable-autoexec", "--python-exit-code", "1", "--python",
    str(repo / "scripts/bas13_verify_import.py"), "--",
    str(root / "export-delivery"), target],
    cwd=repo,capture_output=True,text=True,timeout=120)
(out / "bas13-glb-reimport.log").write_text(import_result.stdout + import_result.stderr)
assert import_result.returncode == 0, import_result.stderr[-2000:]
assert "BAS13_GLB_REIMPORT_BLEND_REOPEN_PASS" in import_result.stdout
bad = worker.submit(owner_id="studio",request_id=str(uuid4()),idempotency_key="invalid-export",
                    scene_id=scene_id,scene_revision=2,snapshot=source,
                    output=root / "must-not-publish",
                    spec={"kind":"export","formats":["glb"],"object_ids":[str(uuid4())]},
                    timeout_seconds=90)
failed = await_job(worker,bad)
assert failed["state"] == "failed", failed
assert not (root / "must-not-publish").exists(), "Failure published partial artifact"
try:
    worker.submit(**{**render_params,"output":root / "different-output"})
except ValueError as exc:
    assert "idempotency_conflict" in str(exc)
else:
    raise AssertionError("Changed payload accepted with old idempotency key")
assert all((root / x).is_dir() for x in ("render-delivery", "export-delivery"))
summary = {"source_commit":COMMIT,"blender":"4.5.15",
           "render_job_id":render["job_id"],"export_job_id":export["job_id"],
           "render_png_sha256":sha(png),"render_manifest_sha256":sha(root / "render-delivery/manifest.json"),
           "export_manifest_sha256":sha(root / "export-delivery/manifest.json"),
           "render_artifacts":len(render_manifest.artifacts),
           "export_artifacts":len(export_manifest.artifacts),
           "glb_reimport":True,"blend_reopen":True,
           "changed_payload_rejected":True,"no_partial_on_failure":True}
(out / "bas13-evidence.json").write_text(json.dumps(summary,indent=2))
print("BAS13_ARTIFACT_ACCEPTANCE_PASS",json.dumps(summary),flush=True)

"""BAS-13 isolated artifact worker: lifecycle, idempotency and no partial publish."""
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from blender_mcp.studio.artifact_worker import ArtifactWorker


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir(mode=0o700)
    blender = tmp_path / "fake-blender"
    blender.write_text(
        "#!" + sys.executable + "\n"
        "import json, struct, sys, time, pathlib, zlib\n"
        "args = sys.argv[sys.argv.index('--')+1:]\n"
        "snapshot, output, conf = map(pathlib.Path,args)\n"
        "spec = json.loads(conf.read_text())\n"
        "if snapshot.read_bytes().endswith(b'SLOW'): time.sleep(20)\n"
        "if snapshot.read_bytes().endswith(b'ERROR'): sys.exit(1)\n"
        "def chunk(name, data):\n"
        "    return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data)&0xffffffff)\n"
        "if spec['kind']=='render':\n"
        "    w,h = spec['width'],spec['height']\n"
        "    rows = b''.join(bytes([0])+bytes([x%255,50,90,255])*w for x in range(h))\n"
        "    img = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,6,0,0,0))\n"
        "    img += chunk(b'IDAT',zlib.compress(rows)) + chunk(b'IEND',b'')\n"
        "    (output/'still.png').write_bytes(img)\n"
        "    files=['still.png']\n"
        "else:\n"
        "    data=b'glTF'+struct.pack('<II',2,64)+b'\\x00'*52\n"
        "    (output/'scene.glb').write_bytes(data)\n"
        "    files=['scene.glb']\n"
        "(output/'run.json').write_text(json.dumps({'kind':spec['kind'],'files':files,\n"
        " 'blender_version':'4.5.15 LTS','camera':'TestCamera','engine':'CYCLES',\n"
        " 'samples':spec.get('samples'),'resolution':[spec.get('width'),spec.get('height')]}))\n"
    )
    blender.chmod(0o700)
    script = tmp_path / "dummy.py"
    script.write_text("# worker template placeholder\n")
    worker = ArtifactWorker(root, blender, script, "a" * 40)
    snap = root / "snap.blend"
    snap.write_bytes(b"BLENDER-SNAPSHOT")
    return worker, snap


def wait_for(worker, row, timeout=6):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        value = worker.get(row["job_id"], "owner")
        if value["state"] in ("succeeded", "failed", "cancelled", "interrupted_unknown"):
            return value
        time.sleep(.02)
    pytest.fail("Job did not terminate")


def submit(worker, snap, name="output", key="key", **changes):
    values = dict(owner_id="owner", request_id="request-1",
                  idempotency_key=key, scene_id="00000000-0000-4000-8000-000000000001",
                  scene_revision=3, snapshot=snap, output=worker.root / name,
                  spec={"kind": "render", "width": 32, "height": 32, "samples": 1},
                  timeout_seconds=6)
    values.update(changes)
    return worker.submit(**values)


def test_job_atomic_manifest_and_idempotency(runtime):
    worker, snap = runtime
    row = submit(worker, snap)
    terminal = wait_for(worker, row)
    assert terminal["state"] == "succeeded", terminal
    path = worker.root / "output"
    metadata = json.loads((path / "manifest.json").read_text())
    assert metadata["job_id"] == row["job_id"]
    data = path / "still.png"
    assert metadata["artifacts"][0]["sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
    assert metadata["artifacts"][0]["provenance"]["source_commit"] == "a" * 40
    assert submit(worker, snap, key="key")["job_id"] == row["job_id"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        submit(worker, snap, name="changed", key="key")
    with pytest.raises(ValueError, match="destination"):
        submit(worker, snap, name="output", key="new")
    with pytest.raises(ValueError):
        submit(worker, snap, name="../escape", key="escape")


def test_failed_child_does_not_publish(runtime):
    worker, snap = runtime
    snap.write_bytes(b"BLENDER-ERROR")
    row = submit(worker, snap, name="failed-output", key="broken")
    result = wait_for(worker, row)
    assert result["state"] == "failed"
    assert result["manifest"] is None
    assert not (worker.root / "failed-output").exists()
    assert (worker.work / "logs" / (row["job_id"] + ".log")).exists()


def test_cancel_real_process_and_stop_confirmation(runtime):
    worker, snap = runtime
    snap.write_bytes(b"BLENDER-SLOW")
    row = submit(worker, snap, name="cancel-output", key="cancel", timeout_seconds=6)
    start = time.monotonic()
    while time.monotonic() - start < 2:
        if worker.get(row["job_id"], "owner")["state"] == "running":
            break
        time.sleep(.01)
    worker.cancel(row["job_id"], "owner")
    result = wait_for(worker, row)
    assert result["state"] in ("cancelled", "failed")
    assert not (worker.root / "cancel-output").exists()
    if result["state"] == "cancelled":
        assert result["stop_confirmed"] == 1
    with pytest.raises(KeyError):
        worker.get(row["job_id"], "other")

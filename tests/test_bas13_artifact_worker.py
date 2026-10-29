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


def test_timeout_is_failed_and_never_published(runtime):
    worker, snap = runtime
    snap.write_bytes(b"BLENDER-SLOW")
    row = submit(worker, snap, name="timeout-output", key="timeout", timeout_seconds=1)
    result = wait_for(worker, row, timeout=5)
    assert result["state"] == "failed"
    assert "TimeoutError" in result["error"]
    assert result["manifest"] is None
    assert not (worker.root / "timeout-output").exists()


def test_staging_uses_verified_blend_and_rejects_symlink(runtime):
    worker, snap = runtime
    outside = worker.root.parent / "outside.blend"
    outside.write_bytes(b"BLENDER-TEST")
    alias = worker.root / "alias.blend"
    alias.symlink_to(outside)
    # The service only accepts an already verified copy inside the sandbox.
    with pytest.raises(ValueError, match="snapshot_symlink_denied"):
        submit(worker, alias, name="symlink", key="symlink")
    with pytest.raises(ValueError):
        submit(worker, outside, name="external-snapshot", key="outside")
    with pytest.raises(ValueError):
        submit(worker, worker.root / "missing.blend", name="missing", key="missing")


def _overwrite(worker, snap, key, expected, token, **kwargs):
    return submit(worker, snap, name="output", key=key, overwrite=True,
                  expected_sha256=expected, confirmation_token=token, **kwargs)


def test_confirmed_overwrite_atomic_backup_and_token_consumption(runtime):
    from blender_mcp.studio.artifact_publish import verified_delivery, _sha
    from blender_mcp.security_policy import (
        Policy, Profile, issue_resource_confirmation, authorize_studio_request,
    )
    from uuid import uuid4

    worker, snap = runtime
    first = submit(worker, snap)
    assert wait_for(worker, first)["state"] == "succeeded"
    destination = worker.root / "output"
    before = verified_delivery(destination)
    previous = _sha(destination / "still.png")
    scene = "00000000-0000-4000-8000-000000000001"
    secret = b"b" * 40
    op = "studio_render_still"
    token = issue_resource_confirmation(
        secret, operation=op, scene_id=scene, scene_revision=3,
        resource=destination.as_uri() + "|" + before)
    raw = {"operation": op, "effect_scope": "file", "request_id": str(uuid4()),
           "scene_id": scene, "scene_revision": 3, "idempotency_key": "renew",
           "output_uri": destination.as_uri(), "width": 32, "height": 32,
           "samples": 2, "timeout_seconds": 6, "overwrite": True,
           "expected_sha256": before, "confirmation_token": token}
    policy = Policy(Profile.STUDIO_LOCAL,write_roots=(worker.root,))
    assert authorize_studio_request(raw, policy, confirmation_secret=secret).overwrite

    replaced = _overwrite(worker, snap, "renew", before, token,
                          spec={"kind": "render", "width": 32, "height": 32,
                                "samples": 2})
    terminal = wait_for(worker, replaced)
    assert terminal["state"] == "succeeded", terminal
    assert terminal["backup"] and Path(terminal["backup"]).is_dir()
    assert _sha(Path(terminal["backup"]) / "still.png") == previous
    assert (Path(terminal["backup"]) / "manifest.json").read_bytes() != (
        destination / "manifest.json").read_bytes()
    assert verified_delivery(destination) != before
    assert _overwrite(worker, snap, "renew", before, token,
                      spec={"kind": "render", "width": 32, "height": 32,
                            "samples": 2})["job_id"] == replaced["job_id"]
    with pytest.raises(ValueError,match="consumed"):
        _overwrite(worker, snap, "other-confirmation", verified_delivery(destination),
                   token)
    with pytest.raises(ValueError,match="checksum"):
        _overwrite(worker, snap, "stale-confirmation", before, "different-token")


def test_tampered_confirmation_or_scene_denied(runtime):
    from blender_mcp.security_policy import (
        Policy, Profile, PolicyDenied,
        issue_resource_confirmation, authorize_studio_request,
    )
    from blender_mcp.studio.artifact_publish import verified_delivery
    from uuid import uuid4

    worker, snap = runtime
    first = submit(worker, snap)
    assert wait_for(worker, first)["state"] == "succeeded"
    dest = worker.root / "output"
    sha = verified_delivery(dest)
    scene = "00000000-0000-4000-8000-000000000001"
    secret = b"x" * 40
    token = issue_resource_confirmation(secret, operation="studio_export_artifacts",
        scene_id=scene, scene_revision=3,
        resource=dest.as_uri() + "|" + sha)
    raw = {"operation": "studio_export_artifacts", "effect_scope": "file",
           "request_id": str(uuid4()), "scene_id": scene, "scene_revision": 3,
           "idempotency_key": "signed", "output_uri": dest.as_uri(),
           "formats": ["glb"], "object_ids": [str(uuid4())],
           "overwrite": True, "expected_sha256": sha, "confirmation_token": token}
    policy = Policy(Profile.STUDIO_LOCAL,write_roots=(worker.root,))
    assert authorize_studio_request(raw, policy, confirmation_secret=secret).overwrite
    for altered in [
            {"scene_revision": 4}, {"expected_sha256": "0" * 64},
            {"confirmation_token": token[:-2] + "aa"},
            {"output_uri": (worker.root / "different-output").as_uri()},
            {"operation": "studio_render_still"}]:
        with pytest.raises((PolicyDenied, ValueError)):
            authorize_studio_request({**raw,**altered}, policy,
                                     confirmation_secret=secret)
    assert verified_delivery(dest) == sha


def test_overwrite_cancel_and_destination_change_fail_closed(runtime):
    from blender_mcp.studio.artifact_publish import verified_delivery
    worker, snap = runtime
    base = submit(worker, snap)
    assert wait_for(worker, base)["state"] == "succeeded"
    dest = worker.root / "output"
    before = verified_delivery(dest)
    snap.write_bytes(b"BLENDER-SLOW")
    row = _overwrite(worker, snap, "cancel-replacement", before, "authorized-test-token",
                     timeout_seconds=6)
    worker.cancel(row["job_id"], "owner")
    result = wait_for(worker,row)
    assert result["state"] == "cancelled" or result["state"] == "failed",result
    assert verified_delivery(dest) == before
    assert not result["manifest"]
    # A stale expected digest may not start another job or consume a token.
    with pytest.raises(ValueError, match="checksum"):
        _overwrite(worker,snap,"wrong-sha","f"*64,"distinct-token")


def test_worker_restart_marks_uncertain_overwrite_without_auto_replay(runtime):
    from blender_mcp.studio.artifact_publish import verified_delivery
    worker,snap = runtime
    first = submit(worker,snap)
    assert wait_for(worker,first)["state"] == "succeeded"
    before = verified_delivery(worker.root/"output")
    with worker._connect() as db:
        db.execute("""INSERT INTO jobs(job_id,owner_id,idempotency_key,payload_hash,
            request_id,scene_id,scene_revision,spec,snapshot,snapshot_sha256,
            output,timeout_seconds,state,created_at,updated_at,
            overwrite,expected_sha256,confirmation_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'running',?,?,1,?,?)""",
            ("11111111-1111-4111-8111-111111111111","owner","restart","digest",
             "request","00000000-0000-4000-8000-000000000001",3,"{}",
             str(snap),"a"*64,str(worker.root/"output"),120,"now","now",
             before,"consumed-token"))
        db.execute("""INSERT INTO confirmed_tokens(token_hash,job_id)
                      VALUES(?,?)""",("consumed-token",
                         "11111111-1111-4111-8111-111111111111"))
    resumed = ArtifactWorker(worker.root,worker.blender,worker.script,"a"*40)
    record = resumed.get("11111111-1111-4111-8111-111111111111","owner")
    assert record["state"] == "interrupted_unknown"
    assert resumed.cancel(record["job_id"],"owner")["state"] == "interrupted_unknown"
    assert verified_delivery(worker.root/"output") == before


def test_atomic_exchange_exception_rolls_back_previous_delivery(runtime, monkeypatch):
    import blender_mcp.studio.artifact_worker as worker_mod
    from blender_mcp.studio.artifact_publish import verified_delivery, atomic_exchange

    worker,snap = runtime
    initial = submit(worker,snap)
    assert wait_for(worker,initial)["state"] == "succeeded"
    destination = worker.root / "output"
    previous = verified_delivery(destination)
    calls = []

    def exchange_then_fail_once(left,right):
        atomic_exchange(left,right)
        calls.append(1)
        if len(calls) == 1:
            raise OSError("injected error after atomic swap")

    monkeypatch.setattr(worker_mod, "atomic_exchange", exchange_then_fail_once)
    second = _overwrite(worker,snap,"rollback-error",previous,"trusted-test-token")
    outcome = wait_for(worker,second)
    assert outcome["state"] == "failed",outcome
    assert len(calls) == 2, "Rollback must use another atomic exchange"
    assert verified_delivery(destination) == previous
    assert not outcome["manifest"]
    assert Path(worker.work/"backups"/second["job_id"]).exists()


def test_overwrite_destination_mutation_during_child_is_conflict(runtime):
    from blender_mcp.studio.artifact_publish import verified_delivery
    worker,snap = runtime
    # Delay only this test's fake Blender worker to allow a destination edit.
    program = worker.blender.read_text()
    program = program.replace(
        "if snapshot.read_bytes().endswith(b'SLOW'): time.sleep(20)",
        "if snapshot.read_bytes().endswith(b'SLOW'): time.sleep(20)\\n"
        "if snapshot.read_bytes().endswith(b'PAUSE'): time.sleep(0.8)")
    worker.blender.write_text(program)
    initial = submit(worker,snap)
    assert wait_for(worker,initial)["state"] == "succeeded"
    destination = worker.root/"output"
    previous = verified_delivery(destination)
    snap.write_bytes(b"BLENDER-PAUSE")
    row = _overwrite(worker,snap,"destination-race",previous,"trusted-token")
    (destination/"render-details.json").write_text('{"external":"modified"}')
    result = wait_for(worker,row)
    assert result["state"] == "failed",result
    assert result["manifest"] is None
    assert not (worker.work/"backups"/row["job_id"]).exists()
    assert (destination/"render-details.json").read_text() == '{"external":"modified"}'


def test_crash_journal_retained_for_manual_reconciliation(runtime):
    from blender_mcp.studio.artifact_publish import verified_delivery
    worker,snap = runtime
    first = submit(worker,snap)
    assert wait_for(worker,first)["state"] == "succeeded"
    delivery = worker.root/"output"
    before = verified_delivery(delivery)
    interrupted_id = "22222222-2222-4222-8222-222222222222"
    stage = worker.work/"stage"/interrupted_id
    stage.mkdir()
    (stage/"publish-journal.json").write_text(json.dumps({
        "job_id":interrupted_id,"destination":str(delivery),
        "expected_sha256":before,"state":"prepared"}))
    with worker._connect() as db:
        db.execute("""INSERT INTO jobs(job_id,owner_id,idempotency_key,payload_hash,
            request_id,scene_id,scene_revision,spec,snapshot,snapshot_sha256,
            output,timeout_seconds,state,created_at,updated_at,
            overwrite,expected_sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'running',?,?,1,?)""",
            (interrupted_id,"owner","journal","hash","r",
             "00000000-0000-4000-8000-000000000001",3,"{}",
             str(snap),"a"*64,str(delivery),120,"now","now",before))
    fresh = ArtifactWorker(worker.root,worker.blender,worker.script,"a"*40)
    state = fresh.get(interrupted_id,"owner")
    assert state["state"] == "interrupted_unknown"
    assert (stage/"publish-journal.json").is_file()
    assert verified_delivery(delivery) == before

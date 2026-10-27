"""End-to-end MCP STDIO client → gateway checkout → Blender 4.5.15 GUI (Xvfb).

Executed in the same Actions job as the checksum-pinned Blender 4.5.15 smoke.
This is a REAL process integration, not a mocked socket or headless render.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import secrets
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def unpack(result):
    data = getattr(result, "structuredContent", None)
    if data is None:
        for part in result.content:
            value = getattr(part, "text", None)
            if value:
                data = json.loads(value)
                break
    if isinstance(data, dict) and "result" in data and len(data) == 1:
        data = data["result"]
    assert isinstance(data, dict), repr(result)
    return data


async def accept(port, record):
    env = dict(os.environ)
    env.update({"BLENDER_MCP_PROFILE": "studio-local", "BLENDER_HOST": "127.0.0.1",
                "BLENDER_PORT": str(port), "DISABLE_TELEMETRY": "true"})
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "blender_mcp.server", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            toolset = {t.name for t in (await client.list_tools()).tools}
            assert {"studio_handshake", "studio_scene_inspect", "studio_create_object"} <= toolset
            assert "execute_blender_code" not in toolset
            handshake = unpack(await client.call_tool("studio_handshake", {}))
            assert handshake["addon_protocol"] >= 15, handshake
            assert handshake["blender_version"].startswith("4.5.15"), handshake
            assert handshake["profile"] == "studio-local"
            assert {"studio_create_object", "studio_scene_inspect", "studio_scene_snapshot", "studio_scene_restore", "studio_scene_save"} <= set(handshake["operations"])
            assert str(ROOT) in handshake["package_file"], handshake
            assert "uvx" not in handshake["package_file"], handshake
            assert Path(handshake["python_executable"]).resolve() == Path(sys.executable).resolve(), handshake
            inspect = {"operation": "studio_scene_inspect", "request_id": str(uuid4()),
                       "effect_scope": "read"}
            scene = unpack(await client.call_tool("studio_scene_inspect", {"request": inspect}))
            assert scene["status"] == "succeeded", scene
            sid, rev = scene["scene_id"], scene["scene_revision"]
            create = {
                "operation": "studio_create_object", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": sid, "scene_revision": rev,
                "idempotency_key": "bas05-gui-cube", "name": "BAS05_Cube",
                "spec": {"kind": "primitive", "primitive": "cube"},
            }
            made = unpack(await client.call_tool("studio_create_object", {"request": create}))
            assert made["status"] == "succeeded", made
            assert made["scene_revision"] == rev + 1, made
            oid = made["result"]["objects"][0]["id"]
            assert oid
            next_scene = unpack(await client.call_tool(
                "studio_scene_inspect", {"request": {**inspect, "request_id": str(uuid4())}}))
            assert next_scene["scene_revision"] == rev + 1, next_scene
            scene_objects = json.loads(next_scene["result"]["summary"])["objects"]
            assert any(o["name"] == "BAS05_Cube" and o["object_id"] == oid for o in scene_objects)
            stale = unpack(await client.call_tool("studio_create_object", {"request": {
                **create, "request_id": str(uuid4()), "idempotency_key": "stale-key",
            }}))
            assert stale["status"] == "failed" and stale["error"]["code"] == "stale_scene", stale
            invalid = unpack(await client.call_tool("studio_create_object", {"request": {
                **create, "request_id": str(uuid4()), "idempotency_key": "bad-payload",
                "spec": {"kind": "primitive", "primitive": "arbitrary-python"},
            }}))
            assert invalid["status"] == "failed" and invalid["error"]["code"] == "invalid_input", invalid
            # BAS-06: 50 additional scene creates and 50 scene reads through
            # the real MCP client -> Blender GUI queue (100 sequential calls).
            stress_started = time.monotonic()
            stress_count = 0
            for index in range(50):
                obj_name = f"BAS06_Stress_{index:02d}"
                request = {
                    "operation": "studio_create_object",
                    "request_id": str(uuid4()),
                    "effect_scope": "scene",
                    "scene_id": sid,
                    "scene_revision": rev + 1 + index,
                    "idempotency_key": f"bas06-stress-{index:02d}",
                    "name": obj_name,
                    "spec": {"kind": "primitive", "primitive": "cube"},
                }
                created = unpack(await client.call_tool("studio_create_object", {"request": request}))
                assert created["status"] == "succeeded", (index, created)
                assert created["scene_revision"] == rev + 2 + index, (index, created)
                stress_count += 1
                seen = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                    "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                    "effect_scope": "read", "limit": 100,
                }}))
                assert seen["status"] == "succeeded", (index, seen)
                assert seen["scene_revision"] == rev + 2 + index, (index, seen)
                objects = json.loads(seen["result"]["summary"])["objects"]
                assert any(obj["name"] == obj_name for obj in objects), (index, obj_name)
                stress_count += 1
            assert stress_count == 100
            record["bas06_stress"] = {
                "requests": stress_count, "creates": 50, "inspects": 50,
                "elapsed_seconds": round(time.monotonic() - stress_started, 3),
                "last_revision": rev + 51,
            }
            # BAS-12: immutable sandboxed .blend copy created by real GUI bpy,
            # verified and replayed without creating a second snapshot.
            sandbox_request = {
                "operation": "studio_scene_snapshot", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid,
                "scene_revision": rev + 51,
                "idempotency_key": "bas12-gui-snapshot", "label": "golden-e2e",
            }
            snap = unpack(await client.call_tool(
                "studio_scene_snapshot", {"request": sandbox_request}))
            assert snap["status"] == "succeeded", snap
            snapshot_id = snap["result"]["snapshot_id"]
            assert snap["scene_revision"] == rev + 51, snap
            reissued = unpack(await client.call_tool(
                "studio_scene_snapshot",
                {"request": {**sandbox_request, "request_id": str(uuid4())}}))
            assert reissued["status"] == "succeeded", reissued
            assert reissued["result"]["snapshot_id"] == snapshot_id, reissued
            manifest_path = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]) / ".bas12-snapshots" / snapshot_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            scene_copy = manifest_path.parent / "scene.blend"
            assert manifest["scene_id"] == sid
            assert manifest["scene_revision"] == rev + 51
            assert manifest["sha256"] == hashlib.sha256(scene_copy.read_bytes()).hexdigest()
            # Mutate then restore with a one-shot trusted acceptance token.
            mutation = {
                "operation": "studio_create_object", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": sid,
                "scene_revision": rev + 51, "idempotency_key": "bas12-restore-probe",
                "name": "BAS12_Throwaway",
                "spec": {"kind": "primitive", "primitive": "cube"},
            }
            added = unpack(await client.call_tool("studio_create_object",
                                                   {"request": mutation}))
            assert added["status"] == "succeeded", added
            from blender_mcp.security_policy import issue_confirmation
            secret = os.environ["BLENDER_MCP_CONFIRM_SECRET"].encode()
            confirm = issue_confirmation(
                secret, operation="studio_scene_restore", scene_id=sid,
                scene_revision=rev + 52, snapshot_id=snapshot_id)
            restored = unpack(await client.call_tool("studio_scene_restore", {"request": {
                "operation": "studio_scene_restore", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 52,
                "idempotency_key": "bas12-restore-case", "snapshot_id": snapshot_id,
                "confirmation_token": confirm}}))
            assert restored["status"] == "succeeded", restored
            assert restored["scene_revision"] == rev + 53, restored
            restored_inspect = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 100}}))
            assert restored_inspect["scene_revision"] == rev + 53
            restored_objects = json.loads(restored_inspect["result"]["summary"])["objects"]
            assert not any(item["name"] == "BAS12_Throwaway" for item in restored_objects)
            assert any(item["object_id"] == oid for item in restored_objects)
            assert restored["result"]["snapshot_id"] != snapshot_id  # pre-restore backup ID
            # BAS-12 non-overwrite save is an atomic copy, not a hardlink
            # to the immutable snapshot. Both file and snapshot survive.
            destination = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]) / "bas12-export.blend"
            saved_scene = unpack(await client.call_tool("studio_scene_save", {"request": {
                "operation": "studio_scene_save", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 53,
                "idempotency_key": "bas12-export", "output_uri": destination.as_uri()}}))
            assert saved_scene["status"] == "succeeded", saved_scene
            assert destination.is_file() and destination.read_bytes().startswith(b"BLENDER")
            assert saved_scene["scene_revision"] == rev + 53
            record["bas12_save"] = {
                "path": str(destination),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "snapshot_id": saved_scene["result"]["snapshot_id"],
                "overwrite_disabled": True,
            }
            # Overwrite requires the pre-existing file SHA and a separate
            # resource-bound trusted confirmation, never plain save permission.
            from blender_mcp.security_policy import issue_resource_confirmation
            old_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            overwrite_token = issue_resource_confirmation(
                secret, operation="studio_scene_save", scene_id=sid,
                scene_revision=rev + 53, resource=destination.as_uri() + "|" + old_hash)
            replaced = unpack(await client.call_tool("studio_scene_save", {"request": {
                "operation": "studio_scene_save", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 53,
                "idempotency_key": "bas12-overwrite", "output_uri": destination.as_uri(),
                "overwrite": True, "expected_sha256": old_hash,
                "confirmation_token": overwrite_token}}))
            assert replaced["status"] == "succeeded", replaced
            assert destination.is_file() and destination.read_bytes().startswith(b"BLENDER")
            replay_overwrite = unpack(await client.call_tool("studio_scene_save", {"request": {
                "operation": "studio_scene_save", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 53,
                "idempotency_key": "bas12-overwrite", "output_uri": destination.as_uri(),
                "overwrite": True, "expected_sha256": old_hash,
                "confirmation_token": overwrite_token}}))
            assert replay_overwrite["status"] == "failed", replay_overwrite
            record["bas12_overwrite"] = {
                "approved_sha256": old_hash,
                "new_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "backup_available": True, "replay_denied": True,
            }
            # Open is an in-process scene import, keeping the addon socket
            # alive and returning a NEW scene UUID to force client resync.
            original_snapshot = unpack(await client.call_tool(
                "studio_scene_snapshot", {"request": {
                    "operation": "studio_scene_snapshot", "request_id": str(uuid4()),
                    "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 53,
                    "idempotency_key": "bas12-open-prior", "label": "pre-open"}}))
            assert original_snapshot["status"] == "succeeded", original_snapshot
            prior_id = original_snapshot["result"]["snapshot_id"]
            file_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
            token_open = issue_resource_confirmation(
                secret, operation="studio_scene_open", scene_id=sid,
                scene_revision=rev + 53, snapshot_id=prior_id,
                resource=destination.as_uri() + "|" + file_sha)
            opened = unpack(await client.call_tool("studio_scene_open", {"request": {
                "operation": "studio_scene_open", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 53,
                "idempotency_key": "bas12-open-e2e", "input_uri": destination.as_uri(),
                "expected_sha256": file_sha, "snapshot_id": prior_id,
                "confirmation_token": token_open}}))
            assert opened["status"] == "succeeded", opened
            assert opened["scene_id"] != sid and opened["scene_revision"] == rev + 54
            new_context = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 200}}))
            assert new_context["status"] == "succeeded", new_context
            assert new_context["scene_id"] == opened["scene_id"]
            assert new_context["scene_revision"] == rev + 54
            assert any(item["object_id"] == oid for item in
                       json.loads(new_context["result"]["summary"])["objects"])
            record["bas12_open"] = {
                "old_scene_id": sid, "new_scene_id": opened["scene_id"],
                "old_revision": rev + 53, "new_revision": rev + 54,
                "socket_kept_alive": True, "checksum": file_sha,
            }
            # BAS-07: 10 real typed GUI creates/edits/collection actions,
            # then snapshot-preview + resource-confirmed delete.
            newid, basrev = opened["scene_id"], rev + 54
            bas_actions = []

            async def bas07(operation, key, **fields):
                nonlocal basrev
                raw = {"operation": operation, "request_id": str(uuid4()),
                       "effect_scope": "scene", "scene_id": newid,
                       "scene_revision": basrev, "idempotency_key": key, **fields}
                resp = unpack(await client.call_tool(operation, {"request": raw}))
                assert resp["status"] == "succeeded", (operation, key, resp)
                assert resp["scene_revision"] == basrev + 1, resp
                basrev += 1
                bas_actions.append({"op": operation, "key": key, "revision": basrev})
                return resp["result"]["objects"][0]["id"]

            title_id = await bas07("studio_create_object", "bas07-title",
                                   name="BAS07_Title",
                                   spec={"kind": "text", "text": "Xin chào - BAS07"},
                                   location=[1, 0, 1])
            assert await bas07("studio_set_transform", "bas07-rename",
                               object_id=title_id, action="rename",
                               name="BAS07_Renamed") == title_id
            assert await bas07("studio_set_transform", "bas07-transform",
                               object_id=title_id, action="transform",
                               location=[2, 2, 1], rotation_euler=[0, 0, 0.5],
                               scale=[1.2, 1.2, 1.2]) == title_id
            duplicate_id = await bas07("studio_set_transform", "bas07-duplicate",
                                      object_id=title_id, action="duplicate",
                                      name="BAS07_Copy")
            assert duplicate_id != title_id
            collection_id = await bas07("studio_manage_collection", "bas07-col",
                                         action="create", name="BAS07_Collection")
            assert await bas07("studio_manage_collection", "bas07-col-rename",
                               action="rename", collection_id=collection_id,
                               name="BAS07_Props") == collection_id
            assert await bas07("studio_manage_collection", "bas07-col-move",
                               action="move_object", object_id=title_id,
                               collection_id=collection_id) == title_id
            cube_id = await bas07("studio_create_object", "bas07-cube",
                                  name="BAS07_Cube",
                                  spec={"kind": "primitive", "primitive": "cube"})
            assert await bas07("studio_set_transform", "bas07-cube-scale",
                               object_id=cube_id, action="transform",
                               scale=[2, 2, 2]) == cube_id
            assert await bas07("studio_set_transform", "bas07-cube-rename",
                               object_id=cube_id, action="rename",
                               name="BAS07_FinalCube") == cube_id

            preview = unpack(await client.call_tool("studio_scene_snapshot", {"request": {
                "operation": "studio_scene_snapshot", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": newid, "scene_revision": basrev,
                "idempotency_key": "bas07-before-delete", "label": "delete-preview"}}))
            assert preview["status"] == "succeeded", preview
            preview_id = preview["result"]["snapshot_id"]
            accepted_delete = issue_resource_confirmation(
                secret, operation="studio_delete_object", scene_id=newid,
                scene_revision=basrev, snapshot_id=preview_id,
                resource=duplicate_id)
            delete_request = {
                "operation": "studio_delete_object", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": newid, "scene_revision": basrev,
                "idempotency_key": "bas07-delete", "object_id": duplicate_id,
                "snapshot_id": preview_id, "confirmation_token": accepted_delete}
            deleted = unpack(await client.call_tool(
                "studio_delete_object", {"request": delete_request}))
            assert deleted["status"] == "succeeded", deleted
            assert deleted["scene_revision"] == basrev + 1
            assert duplicate_id in deleted["result"]["scene_diff"]["deleted"]
            assert deleted["result"]["snapshot_id"] != preview_id
            again = unpack(await client.call_tool("studio_delete_object", {"request": {
                **delete_request, "request_id": str(uuid4())}}))
            assert again["status"] == "failed"
            basrev += 1
            final = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 200}}))
            assert final["status"] == "succeeded"
            assert final["scene_revision"] == basrev
            items = json.loads(final["result"]["summary"])["objects"]
            assert not any(x["object_id"] == duplicate_id for x in items)
            assert any(x["object_id"] == title_id and x["name"] == "BAS07_Renamed"
                       and x["type"] == "FONT" for x in items)
            record["bas07"] = {"operations": bas_actions, "delete_passed": True,
                               "delete_replay_denied": True, "preview_id": preview_id,
                               "pre_delete_backup": deleted["result"]["snapshot_id"],
                               "deleted_object_id": duplicate_id,
                               "final_revision": basrev}
            record["bas12_restore"] = {"passed": True, "before_revision": rev + 52,
                                       "after_revision": rev + 53,
                                       "backup_id": restored["result"]["snapshot_id"]}
            record["bas12_snapshot"] = {
                "snapshot_id": snapshot_id, "sha256": manifest["sha256"],
                "bytes": manifest["bytes"], "replay": True,
                "path": str(scene_copy), "manifest": str(manifest_path),
                "scene_revision": manifest["scene_revision"],
            }
            record.update({"handshake": handshake, "scene_id": sid,
                           "initial_revision": rev, "new_revision": made["scene_revision"],
                           "object_id": oid, "object_name": "BAS05_Cube",
                           "invalid_error": invalid["error"]["code"],
                           "stale_error": stale["error"]["code"],
                           "toolset": sorted(toolset)})


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    studio_root = output / "studio-sandbox"
    studio_root.mkdir(mode=0o700, exist_ok=True)
    studio_root.chmod(0o700)
    os.environ["BLENDER_MCP_STUDIO_ROOT"] = str(studio_root)
    os.environ["BLENDER_MCP_CONFIRM_SECRET"] = secrets.token_urlsafe(32)
    spec = json.loads((ROOT / "blender-runtime.json").read_text())
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    blender = ROOT / ".blender-runtime" / "runtime" / spec["artifacts"]["Linux-x86_64"]["executable"]
    assert blender.is_file(), "Run pinned BAS-02 golden job before BAS-05 E2E"
    port = free_port()
    # Both GUI Blender and the MCP subprocess receive the same random secret;
    # it never appears in command-line arguments or log artifacts.
    os.environ["BLENDER_MCP_SOCKET_TOKEN"] = secrets.token_urlsafe(32)
    os.environ["BLENDER_MCP_PROFILE"] = "studio-local"
    done = output / "done"
    log_path = output / "blender-gui.log"
    log = log_path.open("w")
    process = subprocess.Popen(
        ["xvfb-run", "-a", str(blender), "--factory-startup", "--disable-autoexec",
         "--python-exit-code", "1", "--python", str(ROOT / "scripts" / "bas05_blender_gui.py"),
         "--", str(port), str(done), str(output)],
        cwd=ROOT, env={**os.environ, "DISABLE_TELEMETRY": "true"},
        stdout=log, stderr=subprocess.STDOUT,
    )
    record = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "blender_version": spec["blender_version"],
              "gateway_python": platform.python_version(),
              "client": "mcp Python ClientSession over stdio",
              "display": "Xvfb"}
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError("Blender exited before GUI socket ready; see blender-gui.log")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Blender GUI socket did not become ready")
        asyncio.run(asyncio.wait_for(accept(port, record), timeout=100))
        done.write_text("done\n")
        process.wait(timeout=45)
        assert process.returncode == 0, "Blender GUI exit failure"
        blend = output / "bas05-scene.blend"
        assert blend.is_file() and blend.stat().st_size > 1000
        record["scene_blend_sha256"] = hashlib.sha256(blend.read_bytes()).hexdigest()
        heartbeat = json.loads((output / "bas06-heartbeat.json").read_text())
        assert heartbeat["count"] >= 10, heartbeat
        assert heartbeat["max_gap_seconds"] < 2.0, heartbeat
        # Reopen the saved snapshot in a separate headless Blender process so
        # the interactive user scene cannot be destroyed by acceptance tests.
        snapshot = Path(record["bas12_snapshot"]["path"])
        reopen_script = ROOT / "scripts" / "bas12_snapshot_reopen.py"
        reopened = subprocess.run([
            str(blender), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(reopen_script),
            "--", str(snapshot), record["scene_id"], str(record["bas12_snapshot"]["scene_revision"])
        ], cwd=ROOT, capture_output=True, text=True, timeout=90)
        (output / "bas12-reopen.log").write_text(reopened.stdout + reopened.stderr)
        assert reopened.returncode == 0, reopened.stderr[-1000:]
        assert "BAS12_REOPEN_PASS" in reopened.stdout, reopened.stdout[-1000:]
        # Also open the separately published output copy; this is the public
        # studio_scene_save artifact, not an immutable internal snapshot path.
        exported_scene = Path(record["bas12_save"]["path"])
        export_checked = subprocess.run([
            str(blender), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(reopen_script),
            "--", str(exported_scene), record["scene_id"],
            str(record["bas12_restore"]["after_revision"]),
        ], cwd=ROOT, capture_output=True, text=True, timeout=90)
        (output / "bas12-saved-export-reopen.log").write_text(
            export_checked.stdout + export_checked.stderr)
        assert export_checked.returncode == 0, export_checked.stderr[-1000:]
        assert "BAS12_REOPEN_PASS" in export_checked.stdout
        record["bas12_save"]["reopen"] = "passed"
        record["bas12_snapshot"]["reopen"] = "passed"
        record["bas06_heartbeat"] = heartbeat
        record["passed"] = True
        (output / "bas05-e2e.json").write_text(json.dumps(record, indent=2) + "\n")
        print("BAS05_MCP_GUI_E2E_OK", record["source_commit"], flush=True)
    finally:
        done.write_text("done\n")
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()


if __name__ == "__main__":
    main()

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
            # Automated Blender GUI main-thread mutation/inspection, outside MCP.
            async def bas12_golden_gui(action):
                folder = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent
                req = folder / "bas12-golden-edit.request.json"
                ack = folder / "bas12-golden-edit.result.json"
                ack.unlink(missing_ok=True)
                req.write_text(json.dumps({"action": action}))
                deadline = time.monotonic() + 15
                while not ack.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                assert ack.exists(), "Blender golden GUI probe timed out"
                result = json.loads(ack.read_text())
                assert result.get("ok") is True, result
                return result["state"]

            def assert_golden(state):
                assert abs(state["lens"] - 41.25) < 1e-4, state
                for field, expected in (
                    ("diffuse", (0.125, 0.25, 0.5, 1.0)),
                    ("base_color", (0.125, 0.25, 0.5, 1.0)),
                ):
                    assert len(state[field]) == len(expected), state
                    assert all(abs(x - y) < 1e-4 for x, y in zip(state[field], expected)), state
                assert abs(state["metallic"] - 0.22) < 1e-4, state
                assert abs(state["roughness"] - 0.41) < 1e-4, state
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
            # BAS-08: real MCP → GUI GPU capture; no UI or scene mutations.
            assert "studio_capture_viewport" in toolset
            assert "studio_capture_viewport" in handshake["operations"]
            before_capture = json.loads(scene["result"]["summary"])
            captured = unpack(await client.call_tool("studio_capture_viewport", {"request": {
                "operation": "studio_capture_viewport", "request_id": str(uuid4()),
                "effect_scope": "read", "scene_id": sid, "scene_revision": rev,
                "max_size": 320, "mode": "viewport"}}))
            assert captured["status"] == "succeeded", captured.get("error")
            result_capture = captured["result"]["capture"]
            import base64
            png = base64.b64decode(result_capture["image_base64"], validate=True)
            assert png[:8] == bytes.fromhex("89504e470d0a1a0a")
            assert hashlib.sha256(png).hexdigest() == result_capture["sha256"]
            assert result_capture["width"] <= 320 and result_capture["height"] <= 320
            (Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent / "bas08-viewport.png").write_bytes(png)
            capture_observation = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                **inspect, "request_id": str(uuid4())}}))
            after_capture = json.loads(capture_observation["result"]["summary"])
            assert capture_observation["scene_revision"] == rev
            assert before_capture["scene_fingerprint"] == after_capture["scene_fingerprint"]
            assert before_capture["selected"] == after_capture["selected"]
            assert before_capture["frame"] == after_capture["frame"]
            record["bas08_capture"] = {
                "sha256": result_capture["sha256"], "width": result_capture["width"],
                "height": result_capture["height"], "revision_preserved": True,
                "selection_preserved": True, "frame_preserved": True}
            # Camera mode must also use offscreen projection without switching
            # scene.camera, user selection or the viewport shading.
            assert before_capture["camera"], "BAS08 test scene needs an active camera"
            camera_capture = unpack(await client.call_tool("studio_capture_viewport", {"request": {
                "operation": "studio_capture_viewport", "request_id": str(uuid4()),
                "effect_scope": "read", "scene_id": sid, "scene_revision": rev,
                "max_size": 320, "mode": "camera"}}))
            assert camera_capture["status"] == "succeeded", camera_capture.get("error")
            camera_image = camera_capture["result"]["capture"]
            camera_png = base64.b64decode(camera_image["image_base64"], validate=True)
            assert camera_png[:8] == bytes.fromhex("89504e470d0a1a0a")
            assert hashlib.sha256(camera_png).hexdigest() == camera_image["sha256"]
            (Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent / "bas08-camera.png").write_bytes(camera_png)
            post_camera = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                **inspect, "request_id": str(uuid4())}}))
            post_camera_summary = json.loads(post_camera["result"]["summary"])
            for key in ("scene_fingerprint", "selected", "frame", "camera"):
                assert post_camera_summary[key] == before_capture[key], key
            assert post_camera["scene_revision"] == rev
            record["bas08_capture"]["camera_sha256"] = camera_image["sha256"]
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
            initial_golden = await bas12_golden_gui("inspect")
            assert_golden(initial_golden)
            record["bas12_golden_expected"] = initial_golden
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
            # Mutate material nodes and camera lens in Blender GUI outside MCP.
            # A successful restore must revert ALL these exact values.
            mutated = await bas12_golden_gui("mutate")
            assert abs(mutated["lens"] - 92.75) < 1e-4, mutated
            assert abs(mutated["roughness"] - 0.95) < 1e-4, mutated
            assert mutated["revision"] == rev + 51
            record["bas12_golden_mutated"] = mutated
            # Direct Blender GUI mutation must now advance the observed revision.
            observed_mutation = unpack(await client.call_tool(
                "studio_scene_inspect", {"request": {
                    "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                    "effect_scope": "read", "limit": 100}}))
            assert observed_mutation["status"] == "succeeded", observed_mutation
            assert observed_mutation["scene_revision"] == rev + 52
            record["bas08_gui_revision_bump"] = True
            # Mutate then restore with a one-shot trusted acceptance token.
            mutation = {
                "operation": "studio_create_object", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": sid,
                "scene_revision": rev + 52, "idempotency_key": "bas12-restore-probe",
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
                scene_revision=rev + 53, snapshot_id=snapshot_id)
            restored = unpack(await client.call_tool("studio_scene_restore", {"request": {
                "operation": "studio_scene_restore", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 53,
                "idempotency_key": "bas12-restore-case", "snapshot_id": snapshot_id,
                "confirmation_token": confirm}}))
            assert restored["status"] == "succeeded", restored
            assert restored["scene_revision"] == rev + 54, restored
            restored_inspect = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 100}}))
            assert restored_inspect["scene_revision"] == rev + 54
            restored_objects = json.loads(restored_inspect["result"]["summary"])["objects"]
            assert not any(item["name"] == "BAS12_Throwaway" for item in restored_objects)
            assert any(item["object_id"] == oid for item in restored_objects)
            assert restored["result"]["snapshot_id"] != snapshot_id  # pre-restore backup ID
            recovered_golden = await bas12_golden_gui("inspect")
            assert_golden(recovered_golden)
            assert recovered_golden["revision"] == rev + 54
            assert recovered_golden["scene_id"] == sid
            record["bas12_golden_restored"] = recovered_golden
            record["bas12_golden_comparison"] = "exact fields (float tolerance 1e-4) PASS"
            # BAS-12 non-overwrite save is an atomic copy, not a hardlink
            # to the immutable snapshot. Both file and snapshot survive.
            destination = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]) / "bas12-export.blend"
            saved_scene = unpack(await client.call_tool("studio_scene_save", {"request": {
                "operation": "studio_scene_save", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 54,
                "idempotency_key": "bas12-export", "output_uri": destination.as_uri()}}))
            assert saved_scene["status"] == "succeeded", saved_scene
            assert destination.is_file() and destination.read_bytes().startswith(b"BLENDER")
            assert saved_scene["scene_revision"] == rev + 54
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
                scene_revision=rev + 54, resource=destination.as_uri() + "|" + old_hash)
            replaced = unpack(await client.call_tool("studio_scene_save", {"request": {
                "operation": "studio_scene_save", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 54,
                "idempotency_key": "bas12-overwrite", "output_uri": destination.as_uri(),
                "overwrite": True, "expected_sha256": old_hash,
                "confirmation_token": overwrite_token}}))
            assert replaced["status"] == "succeeded", replaced
            assert destination.is_file() and destination.read_bytes().startswith(b"BLENDER")
            replay_overwrite = unpack(await client.call_tool("studio_scene_save", {"request": {
                "operation": "studio_scene_save", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 54,
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
                    "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 54,
                    "idempotency_key": "bas12-open-prior", "label": "pre-open"}}))
            assert original_snapshot["status"] == "succeeded", original_snapshot
            prior_id = original_snapshot["result"]["snapshot_id"]
            file_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
            token_open = issue_resource_confirmation(
                secret, operation="studio_scene_open", scene_id=sid,
                scene_revision=rev + 54, snapshot_id=prior_id,
                resource=destination.as_uri() + "|" + file_sha)
            opened = unpack(await client.call_tool("studio_scene_open", {"request": {
                "operation": "studio_scene_open", "request_id": str(uuid4()),
                "effect_scope": "file", "scene_id": sid, "scene_revision": rev + 54,
                "idempotency_key": "bas12-open-e2e", "input_uri": destination.as_uri(),
                "expected_sha256": file_sha, "snapshot_id": prior_id,
                "confirmation_token": token_open}}))
            assert opened["status"] == "succeeded", opened
            assert opened["scene_id"] != sid and opened["scene_revision"] == rev + 55
            new_context = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 200}}))
            assert new_context["status"] == "succeeded", new_context
            assert new_context["scene_id"] == opened["scene_id"]
            assert new_context["scene_revision"] == rev + 55
            assert any(item["object_id"] == oid for item in
                       json.loads(new_context["result"]["summary"])["objects"])
            opened_golden = await bas12_golden_gui("inspect")
            assert_golden(opened_golden)
            assert opened_golden["scene_id"] == opened["scene_id"]
            # Old scene commands must be rejected after import even if their
            # revision happened to be numerically valid before the switch.
            old_scene_request = {
                "operation": "studio_create_object", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": sid,
                "scene_revision": rev + 54, "idempotency_key": "bas12-stale-after-open",
                "name": "MUST_NOT_CREATE", "spec": {"kind": "primitive", "primitive": "cube"},
            }
            stale_after_open = unpack(await client.call_tool(
                "studio_create_object", {"request": old_scene_request}))
            assert stale_after_open["status"] == "failed", stale_after_open
            assert stale_after_open["error"]["code"] == "stale_scene", stale_after_open
            live_after_open = unpack(await client.call_tool(
                "studio_scene_inspect", {"request": {
                    "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                    "effect_scope": "read", "limit": 200}}))
            assert live_after_open["scene_id"] == opened["scene_id"]
            assert not any(x["name"] == "MUST_NOT_CREATE" for x in
                           json.loads(live_after_open["result"]["summary"])["objects"])
            record["bas12_reconnect"] = {
                "stdio_reused": True, "new_scene_id": opened["scene_id"],
                "old_scene_rejected": True, "post_open_inspect_passed": True,
                "load_handler_semantics": "in-session import only; not open_mainfile"}
            record["bas12_open"] = {
                "old_scene_id": sid, "new_scene_id": opened["scene_id"],
                "old_revision": rev + 54, "new_revision": rev + 55,
                "socket_kept_alive": True, "checksum": file_sha,
            }
            # BAS-07: 10 real typed GUI creates/edits/collection actions,
            # then snapshot-preview + resource-confirmed delete.
            newid, basrev = opened["scene_id"], rev + 55
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
            renamed_view = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "object_id": title_id, "limit": 1}}))
            assert renamed_view["status"] == "succeeded", renamed_view
            renamed_objects = json.loads(renamed_view["result"]["summary"])["objects"]
            assert len(renamed_objects) == 1 and renamed_objects[0]["name"] == "BAS07_Renamed"
            assert renamed_objects[0]["object_id"] == title_id
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

            # Simulate user edits in Blender's GUI main-thread timer. These
            # bypass the dispatcher; BAS-08 must advance the scene revision.
            # Each action must invalidate an otherwise valid delete preview.
            gui_actions = []
            for action in ("translate", "mesh_vertex", "material", "camera", "rename"):
                stale_preview = unpack(await client.call_tool(
                    "studio_scene_snapshot", {"request": {
                        "operation": "studio_scene_snapshot", "request_id": str(uuid4()),
                        "effect_scope": "file", "scene_id": newid,
                        "scene_revision": basrev,
                        "idempotency_key": "bas07-gui-preview-" + action,
                        "label": "gui-edit-preview-" + action}}))
                assert stale_preview["status"] == "succeeded", stale_preview
                stale_id = stale_preview["result"]["snapshot_id"]
                change_request = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent / "bas07-direct-edit.request.json"
                change_result = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent / "bas07-direct-edit.result.json"
                if change_result.exists():
                    change_result.unlink()
                change_request.write_text(json.dumps({
                    "action": action, "object_id": cube_id}))
                deadline = time.monotonic() + 15
                while not change_result.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                assert change_result.exists(), "GUI timer did not acknowledge edit"
                ack = json.loads(change_result.read_text())
                assert ack["success"], ack
                assert ack["before_revision"] == ack["after_revision"] == basrev, ack
                blocked_token = issue_resource_confirmation(
                    secret, operation="studio_delete_object", scene_id=newid,
                    scene_revision=basrev, snapshot_id=stale_id, resource=cube_id)
                rejected = unpack(await client.call_tool("studio_delete_object", {"request": {
                    "operation": "studio_delete_object", "request_id": str(uuid4()),
                    "effect_scope": "scene", "scene_id": newid, "scene_revision": basrev,
                    "idempotency_key": "bas07-gui-delete-" + action,
                    "object_id": cube_id, "snapshot_id": stale_id,
                    "confirmation_token": blocked_token}}))
                assert rejected["status"] == "failed", (action, rejected)
                assert rejected["error"]["code"] == "conflict", (action, rejected)
                live = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                    "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                    "effect_scope": "read", "limit": 200}}))
                assert live["scene_revision"] == basrev + 1, (action, live)
                assert any(x["object_id"] == cube_id for x in
                           json.loads(live["result"]["summary"])["objects"]), action
                basrev = live["scene_revision"]
                gui_actions.append({"action": action, "rejected": True,
                                    "revision_bumped": True})
            record["bas07_out_of_band_gui"] = gui_actions

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
            async def bas08_probe(action):
                folder = Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent
                req_path = folder / "bas08-negative.request.json"
                ack_path = folder / "bas08-negative.result.json"
                ack_path.unlink(missing_ok=True)
                req_path.write_text(json.dumps({"action": action}))
                deadline = time.monotonic() + 15
                while not ack_path.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                assert ack_path.exists(), "BAS-08 GUI negative probe timed out: " + action
                ack = json.loads(ack_path.read_text())
                assert ack.get("ok") is True, ack
                return ack


            # BAS-08 controlled metadata upgrade for objects/collections/materials
            # created before the MCP project assigned stable UUIDs.
            await bas08_probe("create_untracked")
            untracked = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 200}}))
            assert untracked["status"] == "succeeded", untracked
            assert untracked["scene_revision"] == basrev + 1
            basrev = untracked["scene_revision"]
            prebind = json.loads(untracked["result"]["summary"])
            assert any(row["name"] == "BAS08_ManualObject" and row["object_id"] is None
                       for row in prebind["objects"]), "GUI-created object should have no UUID"
            bind_req = {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": newid,
                "scene_revision": basrev, "idempotency_key": "bas08-id-bootstrap",
                "bind_missing_ids": True, "limit": 200}
            owner_bound = unpack(await client.call_tool(
                "studio_scene_inspect", {"request": bind_req}))
            assert owner_bound["status"] == "succeeded", owner_bound
            assert owner_bound["scene_revision"] == basrev + 1, owner_bound
            basrev += 1
            bound_state = json.loads(owner_bound["result"]["summary"])
            assert bound_state["ids_bound"] >= 3
            cube = next(row for row in bound_state["objects"] if row["name"] == "BAS08_ManualObject")
            assert cube["object_id"]
            assert all(row["object_id"] for row in bound_state["objects"])
            assert all(x["collection_id"] for x in bound_state["collections"])
            golden_row = next(row for row in bound_state["objects"] if row["name"] == "BAS08_ManualObject")
            assert golden_row["materials"] and golden_row["materials"][0]["material_id"]
            cube_uuid = cube["object_id"]
            # No-op rerun cannot silently bump revision or change any UUID.
            second = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                **bind_req, "request_id": str(uuid4()), "scene_revision": basrev,
                "idempotency_key": "bas08-id-bootstrap-noop"}}))
            assert second["status"] == "succeeded", second
            assert second["scene_revision"] == basrev
            assert json.loads(second["result"]["summary"])["ids_bound"] == 0
            looked_up = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "object_id": cube_uuid, "limit": 1}}))
            assert looked_up["status"] == "succeeded"
            assert json.loads(looked_up["result"]["summary"])["objects"][0]["name"] == "BAS08_ManualObject"
            observer_forge = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                **bind_req, "request_id": str(uuid4()), "effect_scope": "read"}}))
            assert observer_forge["error"]["code"] == "invalid_input", observer_forge
            record["bas08_binding"] = {
                "ids_bound": bound_state["ids_bound"], "cube_id": cube_uuid,
                "no_op_revision_preserved": True, "material_bound": True,
                "collections_bound": True}

            # Real Blender GUI negative cases, not mocks. The GUI timer changes
            # the window's scene/editor outside MCP and acknowledges each step.
            await bas08_probe("empty")
            empty = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 20}}))
            assert empty["status"] == "succeeded", empty
            blank = json.loads(empty["result"]["summary"])
            assert blank["object_count"] == 0 and blank["camera"] is None
            empty_framing = unpack(await client.call_tool("studio_set_camera", {
                "request": {"operation": "studio_set_camera",
                            "request_id": str(uuid4()), "effect_scope": "scene",
                            "scene_id": empty["scene_id"],
                            "scene_revision": empty["scene_revision"],
                            "idempotency_key": "bas11-empty-scene",
                            "frame_scene": True}}))
            assert empty_framing["error"]["code"] == "invalid_input", empty_framing
            no_camera = unpack(await client.call_tool("studio_capture_viewport", {"request": {
                "operation": "studio_capture_viewport", "request_id": str(uuid4()),
                "effect_scope": "read", "scene_id": empty["scene_id"],
                "scene_revision": empty["scene_revision"],
                "mode": "camera", "max_size": 320}}))
            assert no_camera["error"]["code"] == "capability_unavailable", no_camera
            await bas08_probe("hide_viewport")
            no_view = unpack(await client.call_tool("studio_capture_viewport", {"request": {
                "operation": "studio_capture_viewport", "request_id": str(uuid4()),
                "effect_scope": "read", "scene_id": empty["scene_id"],
                "scene_revision": empty["scene_revision"],
                "mode": "viewport", "max_size": 320}}))
            assert no_view["error"]["code"] == "capability_unavailable", no_view
            await bas08_probe("restore")
            recovered = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "object_id": cube_uuid, "limit": 1}}))
            assert recovered["status"] == "succeeded", recovered
            assert recovered["scene_id"] == newid
            assert recovered["scene_revision"] == basrev
            assert json.loads(recovered["result"]["summary"])["objects"][0]["object_id"] == cube_uuid
            record["bas08_negative_gui"] = {
                "empty_scene": True, "no_camera": no_camera["error"]["code"],
                "no_viewport": no_view["error"]["code"],
                "restored_original_scene": True}

            # BAS-10 through the actual MCP stdio -> authenticated socket
            # -> Blender GUI main-thread dispatcher, not a direct Python call.
            bas10_operations = []
            async def bas10_send(spec, key, target=cube_uuid):
                nonlocal basrev
                req = {"operation": "studio_assign_material",
                       "request_id": str(uuid4()), "effect_scope": "scene",
                       "scene_id": newid, "scene_revision": basrev,
                       "idempotency_key": key, "object_id": target, "spec": spec}
                reply = unpack(await client.call_tool("studio_assign_material", {"request": req}))
                return req, reply

            assert "studio_assign_material" in toolset
            assert "studio_assign_material" in handshake["operations"]
            for material_spec, key in (
                    ({"kind": "pbr", "base_color": [0.12, 0.58, 0.9, 1],
                      "metallic": 0.3, "roughness": 0.4}, "bas10-gui-pbr"),
                    ({"kind": "bevel", "width": 0.15, "segments": 3}, "bas10-gui-bevel"),
                    ({"kind": "geometry_nodes", "preset": "linear_array", "count": 3,
                      "offset": [1.5, 0, 0]}, "bas10-gui-array")):
                raw, result10 = await bas10_send(material_spec, key)
                assert result10["status"] == "succeeded", (key, result10)
                assert result10["scene_revision"] == basrev + 1
                assert result10["result"]["scene_diff"]["updated"] == [cube_uuid]
                basrev += 1
                again = unpack(await client.call_tool("studio_assign_material", {"request": {
                    **raw, "request_id": str(uuid4())}}))
                assert again["status"] == "succeeded" and again["scene_revision"] == basrev
                assert again["result"]["summary"] == "Idempotent replay", again
                bas10_operations.append({"kind": material_spec["kind"], "revision": basrev,
                                         "replay_safe": True})
            _, missing10 = await bas10_send(
                {"kind": "subdivision", "levels": 2}, "bas10-missing", str(uuid4()))
            assert missing10["error"]["code"] == "object_not_found", missing10
            _, invalid10 = await bas10_send(
                {"kind": "geometry_nodes", "preset": "arbitrary-code", "count": 3,
                 "offset": [1, 0, 0]}, "bas10-invalid-graph")
            assert invalid10["error"]["code"] == "invalid_input", invalid10
            gui10 = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "scene_id": newid,
                "scene_revision": basrev, "object_id": cube_uuid, "limit": 1}}))
            assert gui10["status"] == "succeeded", gui10
            rows10 = json.loads(gui10["result"]["summary"])["objects"]
            assert rows10[0]["materials"][-1]["material_id"], rows10
            record["bas10_gui"] = {"operations": bas10_operations, "uuid": cube_uuid,
                                   "unknown_id_denied": True, "arbitrary_graph_denied": True,
                                   "material_id_exposed": True, "last_revision": basrev}

            # BAS-11 real MCP -> authenticated socket -> Blender GUI main thread.
            # Headless CPU render is independent; this tests live GUI camera mode.
            assert {"studio_set_camera", "studio_set_light"} <= toolset
            assert {"studio_set_camera", "studio_set_light"} <= set(handshake["operations"])
            bas11_ids = {"cameras": [], "lights": []}
            bas11_start = basrev

            async def bas11_call(operation, key, **fields):
                nonlocal basrev
                raw = {"operation": operation, "request_id": str(uuid4()),
                       "effect_scope": "scene", "scene_id": newid,
                       "scene_revision": basrev, "idempotency_key": key, **fields}
                result11 = unpack(await client.call_tool(operation, {"request": raw}))
                assert result11["status"] == "succeeded", (key, result11)
                assert result11["scene_revision"] == basrev + 1, (key, result11)
                basrev += 1
                return raw, result11

            original_scene11 = unpack(await client.call_tool("studio_scene_inspect", {
                "request": {"operation": "studio_scene_inspect",
                            "request_id": str(uuid4()), "effect_scope": "read",
                            "scene_id": newid, "scene_revision": basrev, "limit": 200}}))
            original11 = json.loads(original_scene11["result"]["summary"])
            camera_before11 = original11["camera"]
            for ix in range(2):
                args11 = ({"target_id": cube_id, "lens_mm": 45,
                           "resolution_x": 960, "resolution_y": 540}
                          if ix == 0 else {"location": [8, -12, 7],
                                           "rotation_euler": [1.1, 0, 0.5],
                                           "lens_mm": 35, "active": False})
                raw11, created11 = await bas11_call(
                    "studio_set_camera", "bas11-gui-camera-" + str(ix),
                    name="BAS11_GUI_CAMERA_" + str(ix), **args11)
                ident11 = created11["result"]["objects"][0]["id"]
                assert created11["result"]["objects"][0]["kind"] == "camera"
                assert created11["result"]["scene_diff"]["created"] == [ident11]
                bas11_ids["cameras"].append(ident11)
                replay11 = unpack(await client.call_tool("studio_set_camera", {
                    "request": {**raw11, "request_id": str(uuid4())}}))
                assert replay11["status"] == "succeeded" and replay11["scene_revision"] == basrev
                assert replay11["result"]["objects"][0]["id"] == ident11
                assert replay11["result"]["summary"] == "Idempotent replay"
            for typ, energy in (("AREA", 350), ("POINT", 90), ("SUN", 2)):
                light_spec = {"kind": "light", "light_type": typ, "energy": energy,
                              "name": "BAS11_GUI_" + typ, "color": [1, .85, .7],
                              "location": [1, -4, 7]}
                _, light11 = await bas11_call(
                    "studio_set_light", "bas11-gui-light-" + typ, spec=light_spec)
                lamp_id = light11["result"]["objects"][0]["id"]
                assert light11["result"]["objects"][0]["kind"] == "light"
                assert light11["result"]["scene_diff"]["created"] == [lamp_id]
                bas11_ids["lights"].append(lamp_id)
            _, world11 = await bas11_call("studio_set_light", "bas11-gui-world",
                                        spec={"kind": "world", "color": [.1, .2, .35],
                                              "strength": .8})
            assert world11["result"]["scene_diff"]["created"] == []
            # Request data validation must fail without touching Blender scene.
            bad11 = unpack(await client.call_tool("studio_set_light", {"request": {
                "operation": "studio_set_light", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": newid,
                "scene_revision": basrev, "idempotency_key": "bas11-invalid-energy",
                "spec": {"kind": "light", "light_type": "SUN", "energy": 100001,
                         "color": [1, 1, 1], "location": [0, 0, 3]}}}))
            assert bad11["error"]["code"] == "invalid_input", bad11
            missing11 = unpack(await client.call_tool("studio_set_camera", {"request": {
                "operation": "studio_set_camera", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": newid,
                "scene_revision": basrev, "idempotency_key": "bas11-missing-target",
                "target_id": str(uuid4())}}))
            assert missing11["error"]["code"] == "object_not_found", missing11
            before11 = unpack(await client.call_tool("studio_scene_inspect", {
                "request": {"operation": "studio_scene_inspect",
                            "request_id": str(uuid4()), "effect_scope": "read",
                            "scene_id": newid, "scene_revision": basrev, "limit": 200}}))
            before11_data = json.loads(before11["result"]["summary"])
            assert before11_data["camera"] == "BAS11_GUI_CAMERA_0", before11_data["camera"]
            assert before11_data["frame"] == original11["frame"]
            assert before11_data["selected"] == original11["selected"]
            assert all(any(x["object_id"] == oid for x in before11_data["objects"])
                       for oid in bas11_ids["cameras"] + bas11_ids["lights"])
            capture11 = unpack(await client.call_tool("studio_capture_viewport", {
                "request": {"operation": "studio_capture_viewport",
                            "request_id": str(uuid4()), "effect_scope": "read",
                            "scene_id": newid, "scene_revision": basrev,
                            "mode": "camera", "max_size": 320}}))
            assert capture11["status"] == "succeeded", capture11
            image11 = capture11["result"]["capture"]
            png11 = base64.b64decode(image11["image_base64"], validate=True)
            assert png11.startswith(bytes.fromhex("89504e470d0a1a0a"))
            assert hashlib.sha256(png11).hexdigest() == image11["sha256"]
            (Path(os.environ["BLENDER_MCP_STUDIO_ROOT"]).parent /
             "bas11-gui-camera.png").write_bytes(png11)
            after11 = unpack(await client.call_tool("studio_scene_inspect", {
                "request": {"operation": "studio_scene_inspect",
                            "request_id": str(uuid4()), "effect_scope": "read",
                            "scene_id": newid, "scene_revision": basrev, "limit": 200}}))
            after11_data = json.loads(after11["result"]["summary"])
            assert after11_data["scene_fingerprint"] == before11_data["scene_fingerprint"]
            assert after11_data["selected"] == before11_data["selected"]
            assert after11_data["frame"] == before11_data["frame"]
            assert after11_data["camera"] == before11_data["camera"]
            assert after11["scene_revision"] == basrev
            # Switching between the two BAS-11 cameras must be reversible.
            _, switched11 = await bas11_call(
                "studio_set_camera", "bas11-gui-switch",
                camera_id=bas11_ids["cameras"][1], active=True)
            assert switched11["result"]["scene_diff"]["updated"] == [
                bas11_ids["cameras"][1]]
            switched_view11 = unpack(await client.call_tool("studio_scene_inspect", {
                "request": {"operation": "studio_scene_inspect",
                            "request_id": str(uuid4()), "effect_scope": "read"}}))
            assert json.loads(switched_view11["result"]["summary"])["camera"] == "BAS11_GUI_CAMERA_1"
            _, switched_back11 = await bas11_call(
                "studio_set_camera", "bas11-gui-switch-back",
                camera_id=bas11_ids["cameras"][0], active=True)
            assert switched_back11["result"]["scene_diff"]["updated"] == [
                bas11_ids["cameras"][0]]
            record["bas11_gui"] = {
                "camera_ids": bas11_ids["cameras"], "light_ids": bas11_ids["lights"],
                "world_configured": True, "active_camera": "BAS11_GUI_CAMERA_0",
                "camera_before": camera_before11,
                "revision_before": bas11_start, "revision_after": basrev,
                "camera_png_sha256": image11["sha256"],
                "camera_png_bytes": len(png11),
                "idempotent_replay": True, "negative_input_and_missing_target": True,
                "selection_frame_preserved": True, "offscreen_capture_scene_stable": True,
                "interactive_gui": "Xvfb real Blender UI (automated, not human mouse)"}

            record["bas12_restore"] = {"passed": True, "before_revision": rev + 53,
                                       "after_revision": rev + 54,
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



def bas09_socket_cases(port):
    """Real add-on TCP ingress: auth/permission/read-timeout, no side effects."""
    def send(payload):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as peer:
            peer.settimeout(3)
            peer.sendall((json.dumps(payload) + "\n").encode())
            return json.loads(peer.recv(8192))

    unauthorized = send({"type": "ping", "auth": "invalid",
                         "request_id": str(uuid4())})
    assert unauthorized["code"] == "unauthorized", unauthorized
    forbidden = send({"type": "execute_code",
                      "auth": os.environ["BLENDER_MCP_SOCKET_TOKEN"],
                      "params": {"code": "raise RuntimeError('MUST_NOT_RUN')"},
                      "request_id": str(uuid4())})
    assert forbidden["code"] == "permission_denied", forbidden
    # An incomplete frame is never submitted to the main-thread queue.
    # This tests a real TCP read deadline, NOT cancellation of a mutation.
    with socket.create_connection(("127.0.0.1", port), timeout=3) as peer:
        peer.settimeout(0.1)
        peer.sendall(b'{"type":"ping"')
        try:
            unexpected = peer.recv(8192)
        except socket.timeout:
            timed_out = True
        else:
            raise AssertionError("Unexpected response to partial frame: " + repr(unexpected))
    return {"unauthorized": True, "permission_denied": True,
            "partial_frame_timeout": timed_out,
            "scope": "incomplete read-only frame; no mutation dispatched"}


async def bas09_reconnect_case(port, record):
    """Two fresh MCP clients against the same live Blender GUI; no duplicate."""
    env = {**os.environ, "BLENDER_MCP_PROFILE": "studio-local",
           "BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": str(port),
           "DISABLE_TELEMETRY": "true"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "blender_mcp.server", "--host", "127.0.0.1", "--port", str(port)],
        env=env, cwd=str(ROOT))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            handshake = unpack(await client.call_tool("studio_handshake", {}))
            assert "studio_create_object" in handshake["operations"], handshake
            before = unpack(await client.call_tool("studio_scene_inspect", {
                "request": {"operation": "studio_scene_inspect",
                            "request_id": str(uuid4()), "effect_scope": "read",
                            "limit": 200}}))
            assert before["status"] == "succeeded", before
            before_count = json.loads(before["result"]["summary"])["object_count"]
            sid, rev = before["scene_id"], before["scene_revision"]
            original = {"operation": "studio_create_object", "request_id": str(uuid4()),
                        "effect_scope": "scene", "scene_id": sid,
                        "scene_revision": rev, "idempotency_key": "bas09-reconnect-once",
                        "name": "BAS09_Once", "spec": {"kind": "primitive", "primitive": "cube"}}
            created = unpack(await client.call_tool("studio_create_object",
                                                    {"request": original}))
            assert created["status"] == "succeeded", created
            assert created["scene_revision"] == rev + 1, created
            oid = created["result"]["objects"][0]["id"]
    # The first gateway/STDIO process is disconnected at this point.
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            replay = unpack(await client.call_tool("studio_create_object",
                                                   {"request": original}))
            assert replay["status"] == "succeeded", replay
            assert replay["result"]["objects"][0]["id"] == oid, replay
            assert replay["scene_revision"] == rev + 1, replay
            assert replay["result"]["summary"] == "Idempotent replay", replay
            state = unpack(await client.call_tool("studio_scene_inspect", {
                "request": {"operation": "studio_scene_inspect",
                            "request_id": str(uuid4()), "effect_scope": "read",
                            "limit": 200}}))
            assert state["status"] == "succeeded"
            assert state["scene_id"] == sid and state["scene_revision"] == rev + 1
            objects = json.loads(state["result"]["summary"])
            assert objects["object_count"] == before_count + 1
            assert len([row for row in objects["objects"] if row["object_id"] == oid]) == 1
            record["bas09_reconnect"] = {
                "fresh_stdio_processes": 2, "scene_id": sid,
                "revision_before": rev, "revision_after": rev + 1,
                "object_id": oid, "idempotent_replay": True,
                "no_duplicate_side_effect": True, "gui_responsive": True}


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
        record["bas09_socket_cases"] = bas09_socket_cases(port)
        asyncio.run(asyncio.wait_for(bas09_reconnect_case(port, record), timeout=40))
        done.write_text("done\n")
        process.wait(timeout=45)
        assert process.returncode == 0, "Blender GUI exit failure"
        blend = output / "bas05-scene.blend"
        assert blend.is_file() and blend.stat().st_size > 1000
        record["scene_blend_sha256"] = hashlib.sha256(blend.read_bytes()).hexdigest()
        # Independent headless reopen proves GUI-bound UUIDs persisted to BLEND.
        verify_ids = subprocess.run([
            str(blender), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python",
            str(ROOT / "scripts" / "bas08_ids_reopen.py"), "--",
            str(blend), str(record["bas08_binding"]["cube_id"])
        ], cwd=ROOT, capture_output=True, text=True, timeout=90)
        (output / "bas08-reopen.log").write_text(verify_ids.stdout + verify_ids.stderr)
        assert verify_ids.returncode == 0, verify_ids.stderr[-1000:]
        assert "BAS08_IDS_REOPEN_PASS" in verify_ids.stdout
        record["bas08_binding"]["headless_reopen_passed"] = True
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

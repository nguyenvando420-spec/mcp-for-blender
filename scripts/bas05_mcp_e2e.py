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
            assert {"studio_create_object", "studio_scene_inspect", "studio_set_transform", "studio_manage_collection"} <= set(handshake["operations"])
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
            # BAS-07: ten real typed operations through MCP → Blender GUI.
            # Never delete project data: BAS-12 snapshot gate is not yet accepted.
            rev07 = rev + 51
            bas07_evidence = []
            async def bas07(operation, key, **fields):
                nonlocal rev07
                req = {"operation": operation, "request_id": str(uuid4()),
                       "effect_scope": "scene", "scene_id": sid,
                       "scene_revision": rev07, "idempotency_key": key, **fields}
                response = unpack(await client.call_tool(operation, {"request": req}))
                assert response["status"] == "succeeded", (key, response)
                assert response["scene_revision"] == rev07 + 1, (key, response)
                rev07 += 1
                bas07_evidence.append({"key": key, "operation": operation,
                                       "revision": rev07, "result": response["result"]})
                return response["result"]["objects"][0]["id"], req

            title_id, _ = await bas07(
                "studio_create_object", "bas07-text", name="BAS07_Title",
                spec={"kind": "text", "text": "BAS-07 Unicode: Xin chào"},
                location=[1.0, 0.0, 1.0])
            renamed_id, _ = await bas07(
                "studio_set_transform", "bas07-rename", action="rename",
                object_id=title_id, name="BAS07_Renamed")
            assert renamed_id == title_id
            transformed_id, _ = await bas07(
                "studio_set_transform", "bas07-transform", action="transform",
                object_id=title_id, location=[2.0, 3.0, 1.0],
                rotation_euler=[0.0, 0.0, 0.5], scale=[1.2, 1.2, 1.2])
            assert transformed_id == title_id
            copy_id, _ = await bas07(
                "studio_set_transform", "bas07-copy", action="duplicate",
                object_id=title_id, name="BAS07_Copy")
            assert copy_id != title_id
            col_id, _ = await bas07(
                "studio_manage_collection", "bas07-col", action="create",
                name="BAS07_Collection")
            renamed_col_id, _ = await bas07(
                "studio_manage_collection", "bas07-col-rename", action="rename",
                collection_id=col_id, name="BAS07_Props")
            assert renamed_col_id == col_id
            moved_id, _ = await bas07(
                "studio_manage_collection", "bas07-move", action="move_object",
                collection_id=col_id, object_id=title_id)
            assert moved_id == title_id
            cube_id, cube_request = await bas07(
                "studio_create_object", "bas07-cube", name="BAS07_Cube",
                spec={"kind": "primitive", "primitive": "cube"},
                location=[0.0, 2.0, 0.0])
            await bas07(
                "studio_set_transform", "bas07-scale", action="transform",
                object_id=cube_id, scale=[2.0, 2.0, 2.0])
            final_id, _ = await bas07(
                "studio_set_transform", "bas07-cube-rename", action="rename",
                object_id=cube_id, name="BAS07_Cube_Final")
            assert final_id == cube_id
            # Replaying the original request must not advance the revision.
            replay = unpack(await client.call_tool("studio_create_object",
                                                    {"request": cube_request}))
            assert replay["status"] == "succeeded", replay
            assert replay["scene_revision"] == cube_request["scene_revision"] + 1, replay
            assert replay["result"]["objects"][0]["id"] == cube_id
            missing = unpack(await client.call_tool("studio_set_transform", {"request": {
                "operation": "studio_set_transform", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": sid, "scene_revision": rev07,
                "idempotency_key": "bas07-missing", "object_id": str(uuid4()),
                "action": "rename", "name": "ShouldNotExist"}}))
            assert missing["error"]["code"] == "object_not_found", missing
            nan_input = unpack(await client.call_tool("studio_set_transform", {"request": {
                "operation": "studio_set_transform", "request_id": str(uuid4()),
                "effect_scope": "scene", "scene_id": sid, "scene_revision": rev07,
                "idempotency_key": "bas07-bounds", "object_id": cube_id,
                "action": "transform", "location": [100001.0, 0.0, 0.0]}}))
            assert nan_input["error"]["code"] == "invalid_input", nan_input
            final_scene = unpack(await client.call_tool("studio_scene_inspect", {"request": {
                "operation": "studio_scene_inspect", "request_id": str(uuid4()),
                "effect_scope": "read", "limit": 200}}))
            assert final_scene["scene_revision"] == rev07, final_scene
            objects07 = json.loads(final_scene["result"]["summary"])["objects"]
            assert any(o["object_id"] == title_id and o["name"] == "BAS07_Renamed"
                       and o["type"] == "FONT" for o in objects07)
            assert any(o["object_id"] == copy_id for o in objects07)
            assert any(o["object_id"] == cube_id and o["name"] == "BAS07_Cube_Final"
                       for o in objects07)
            record["bas07"] = {"operations": bas07_evidence, "passed": True,
                               "delete_enabled": False, "last_revision": rev07}

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

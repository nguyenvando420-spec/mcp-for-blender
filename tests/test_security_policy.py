"""BAS-04 denial tests. This policy is a preflight, not a Blender sandbox."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from blender_mcp.security_policy import (
    DEV_ACK_ENV, DEV_ACK_VALUE, PROFILE_ENV, Policy, PolicyDenied, Profile,
    authorize_studio_request, canonical_allowed_file, check_egress_url,
    exposed_legacy_tools, issue_confirmation, profile_from_env, require_loopback,
    verify_confirmation,
)

SCENE = str(uuid4())
OBJECT = str(uuid4())
SNAPSHOT = str(uuid4())
SECRET = b"s" * 32


def request(operation="studio_scene_inspect", effect_scope="read", **extra):
    return {
        "operation": operation, "effect_scope": effect_scope,
        "request_id": str(uuid4()), "scene_id": SCENE,
        "scene_revision": 7, "idempotency_key": "op-key", **extra,
    }


def denied(fn, *args, **kwargs):
    with pytest.raises(PolicyDenied):
        fn(*args, **kwargs)


def test_profiles_fail_closed():
    assert profile_from_env({}) is Profile.OBSERVER
    denied(profile_from_env, {PROFILE_ENV: "surprise"})
    denied(profile_from_env, {PROFILE_ENV: "developer-local"})
    assert profile_from_env({
        PROFILE_ENV: "developer-local", DEV_ACK_ENV: DEV_ACK_VALUE
    }) is Profile.DEVELOPER_LOCAL
    registered = {"execute_blender_code", "get_scene_info", "generate_3d"}
    assert exposed_legacy_tools(Profile.STUDIO_LOCAL, registered) == {"get_scene_info"}
    assert exposed_legacy_tools(Profile.REMOTE, registered) == set()
    assert exposed_legacy_tools(Profile.DEVELOPER_LOCAL, registered) == registered


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.1", "::", "example.org", "127.0.0.2.evil.test"])
def test_no_public_bridge(host):
    denied(require_loopback, host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_local_bridge(host):
    require_loopback(host)


def test_file_traversal_symlink_overwrite_and_scheme(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    other = tmp_path / "private.blend"
    other.write_text("secret")
    source = root / "ok.blend"
    source.write_text("blend")
    assert canonical_allowed_file(source.as_uri(), (root,), writing=False) == source
    denied(canonical_allowed_file, other.as_uri(), (root,), writing=False)
    denied(canonical_allowed_file, f"file://{root}/../private.blend", (root,), writing=False)
    denied(canonical_allowed_file, "https://example.org/scene.blend", (root,), writing=False)
    denied(canonical_allowed_file, f"file://evil-host{source}", (root,), writing=False)
    denied(canonical_allowed_file, source.as_uri(), (root,), writing=True)
    assert canonical_allowed_file(source.as_uri(), (root,), writing=True, overwrite=True) == source
    symlink = root / "shortcut.blend"
    symlink.symlink_to(source)
    denied(canonical_allowed_file, symlink.as_uri(), (root,), writing=False)
    escaped = root / "escaped"
    escaped.symlink_to(tmp_path, target_is_directory=True)
    denied(canonical_allowed_file, (escaped / "private.blend").as_uri(), (root,), writing=False)


@pytest.mark.parametrize("url", [
    "http://cdn.example.org/a", "https://127.0.0.1/a",
    "https://cdn.example.org.evil.org/a", "https://user:pass@cdn.example.org/a",
    "https://cdn.example.org:8443/a", "file:///etc/passwd",
])
def test_egress_denials(url):
    denied(check_egress_url, url, frozenset({"cdn.example.org"}))


def test_egress_allowed_exact_host():
    assert check_egress_url("https://cdn.example.org/x", frozenset({"cdn.example.org"}))


def test_token_signature_expiry_binding_and_tamper():
    token = issue_confirmation(
        SECRET, operation="studio_scene_restore", scene_id=SCENE,
        scene_revision=7, snapshot_id=SNAPSHOT, now=1000,
    )
    params = dict(operation="studio_scene_restore", scene_id=SCENE,
                  scene_revision=7, snapshot_id=SNAPSHOT)
    verify_confirmation(SECRET, token, now=1000, **params)
    denied(verify_confirmation, SECRET, token, now=1300, **params)
    denied(verify_confirmation, SECRET, token, now=999, **params)
    denied(verify_confirmation, SECRET, token + "a", now=1000, **params)
    denied(verify_confirmation, SECRET, token, now=1000, **{**params, "scene_revision": 8})
    denied(verify_confirmation, SECRET, token, now=1000, **{**params, "snapshot_id": str(uuid4())})
    denied(verify_confirmation, SECRET, token, now=1000, **{**params, "operation": "studio_scene_open"})


def test_allowlists_and_confirmation(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    source = root / "in.blend"
    source.write_text("blend")
    observer = Policy(Profile.OBSERVER)
    authorize_studio_request(request(), observer)
    denied(authorize_studio_request, request("studio_create_object", "scene",
        name="Cube", spec={"kind": "primitive", "primitive": "cube"}), observer)
    studio = Policy(Profile.STUDIO_LOCAL, read_roots=(root,), write_roots=(root,))
    allowed = request("studio_scene_save", "file", output_uri=(root/"new.blend").as_uri())
    assert authorize_studio_request(allowed, studio).operation == "studio_scene_save"
    denied(authorize_studio_request, request("studio_scene_save", "file",
        output_uri=(tmp_path/"outside.blend").as_uri()), studio)
    restore = request("studio_scene_restore", "file", snapshot_id=SNAPSHOT, confirmation_token="fake")
    denied(authorize_studio_request, restore, studio, confirmation_secret=SECRET)
    token = issue_confirmation(SECRET, operation="studio_scene_restore",
        scene_id=SCENE, scene_revision=7, snapshot_id=SNAPSHOT, now=1000)
    restore["confirmation_token"] = token
    assert authorize_studio_request(restore, studio, confirmation_secret=SECRET, now=1000)
    remote = Policy(Profile.REMOTE, read_roots=(root,), write_roots=(root,))
    denied(authorize_studio_request, request(), remote)
    denied(authorize_studio_request, request("studio_render_still", "file",
        width=320, height=180, samples=8, output_uri=(root/"x.png").as_uri()), Policy(Profile.WORKER, write_roots=(root,)))


def test_raw_code_guard_never_connects(monkeypatch):
    from blender_mcp import server
    monkeypatch.setenv(PROFILE_ENV, "studio-local")
    monkeypatch.delenv(DEV_ACK_ENV, raising=False)
    def fail():
        raise AssertionError("Raw Python must not touch Blender socket")
    monkeypatch.setattr(server, "get_blender_connection", fail)
    result = asyncio.run(server.execute_blender_code(ctx=None, code="print('hello')"))
    assert "permission_denied" in result


def test_connection_rejects_public_interface(monkeypatch):
    from blender_mcp.server import resolve_connection
    monkeypatch.setenv("BLENDER_HOST", "0.0.0.0")
    denied(resolve_connection)

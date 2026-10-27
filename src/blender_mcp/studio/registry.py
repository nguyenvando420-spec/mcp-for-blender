"""BAS-05: bounded, fail-closed typed Studio operation registry.

An explicit allowlist, not a Python execution tunnel. Only v1 inspect and
primitive-create have Blender handlers here; jobs, persistence and remote
authentication are intentionally NOT advertised as executable capabilities.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import UUID
from pydantic import ValidationError

from .contracts import (
    Capabilities, ContractVersion, ErrorCode, ObjectRef, PrimitiveSpec,
    ResultPayload, SceneDiff, StudioError, StudioResponse, ensure_supported,
)
from ..security_policy import Policy, PolicyDenied, authorize_studio_request

ADDON_PROTOCOL_MIN = 15
# Operation -> command implemented in the paired BAS-05 Blender addon.
HANDLERS = {
    "studio_scene_inspect": "studio_scene_inspect_v1",
    "studio_create_object": "studio_create_object_v1",
    "studio_scene_snapshot": "studio_scene_snapshot_v1",
}
READ_TOOLS = frozenset({"studio_handshake", "studio_scene_inspect"})
MUTATION_TOOLS = frozenset({"studio_create_object"})


def visible_studio_tools(profile):
    """No remote/worker tools without a trusted authenticated entrypoint."""
    from ..security_policy import Profile
    if profile in (Profile.OBSERVER, Profile.STUDIO_LOCAL, Profile.DEVELOPER_LOCAL):
        return (READ_TOOLS | (MUTATION_TOOLS if profile != Profile.OBSERVER else frozenset())
                | ({"studio_scene_snapshot"} if profile != Profile.OBSERVER and os.getenv("BLENDER_MCP_STUDIO_ROOT") else set()))
    return frozenset()


class StudioGateway:
    def __init__(self, bridge: Callable[[str, dict, bool], dict], policy: Policy):
        self.bridge = bridge
        self.policy = policy

    def _info(self):
        data = self.bridge("get_addon_info", {}, True)
        if not isinstance(data, dict):
            raise RuntimeError("Invalid Blender add-on capability reply")
        return data

    def capabilities(self, info=None):
        info = self._info() if info is None else info
        protocol = info.get("protocol_version")
        if type(protocol) is not int or protocol < ADDON_PROTOCOL_MIN:
            return Capabilities(addon_protocol=0, operations=set())
        advertised = set(info.get("capabilities") or [])
        available = {name for name, command in HANDLERS.items() if command in advertised}
        return Capabilities(addon_protocol=protocol, operations=available,
                            features=({"primitive-create-v1"} if "studio_create_object" in available else set())
                            | ({"scene-inspect-v1"} if "studio_scene_inspect" in available else set())
                            | ({"scene-snapshot-v1"} if "studio_scene_snapshot" in available else set()))

    def handshake(self):
        """No implicit capabilities: absent/outdated add-on advertises none."""
        try:
            info = self._info()
            caps = self.capabilities(info)
            return {
                "contract": caps.contract.model_dump(),
                "addon_protocol": caps.addon_protocol,
                "addon_version": info.get("addon_version"),
                "blender_version": info.get("blender_version"),
                "operations": sorted(caps.operations & (
                    {"studio_scene_inspect"} if self.policy.profile.value == "observer" else
                    set(caps.operations))),
                "features": sorted(caps.features),
                "profile": self.policy.profile.value,
                "python_executable": sys.executable,
                "package_file": str(Path(__file__).resolve()),
                "remote_authenticated": False,
                "job_execution": "not_advertised",
            }
        except Exception as exc:
            # A disconnected local socket can raise a plain Exception from the
            # legacy bridge. The handshake must fail closed, not crash the MCP client.
            return {
                "contract": ContractVersion().model_dump(),
                "operations": [], "profile": self.policy.profile.value,
                "error": {"code": ErrorCode.WORKER_UNAVAILABLE.value,
                          "message": "Blender add-on unavailable (" + type(exc).__name__ + ")"},
                "python_executable": sys.executable,
                "package_file": str(Path(__file__).resolve()),
            }

    @staticmethod
    def _id(raw):
        try:
            return UUID(str(raw.get("request_id")))
        except (ValueError, TypeError, AttributeError):
            return UUID(int=0)

    @staticmethod
    def _context(raw):
        try:
            return UUID(str(raw.get("scene_id"))), int(raw.get("scene_revision") or 0)
        except (ValueError, TypeError, AttributeError):
            return UUID(int=0), 0

    def _failed(self, raw, code, message):
        sid, revision = self._context(raw)
        response = StudioResponse(
            request_id=self._id(raw), scene_id=sid, scene_revision=max(revision, 0),
            status="failed", error=StudioError(code=code, message=str(message)[:256] or code.value),
        )
        return response.model_dump(mode="json")

    def invoke(self, raw: dict[str, Any], expected_operation: str):
        if not isinstance(raw, dict):
            raw = {}
        if raw.get("operation") != expected_operation:
            return self._failed(raw, ErrorCode.INVALID_INPUT, "Tool and operation mismatch")
        major = (raw.get("contract") or {}).get("major", 1) if isinstance(raw.get("contract", {}), dict) else None
        if major != 1:
            return self._failed(raw, ErrorCode.UNSUPPORTED_VERSION, "Studio contract major 1 required")
        try:
            # Validate & authorize before any Blender operation. Even hidden tools
            # are protected by server.py's tools/call dispatcher.
            req = authorize_studio_request(raw, self.policy)
            caps = self.capabilities()
            ensure_supported(req, caps)
            if req.operation == "studio_scene_inspect":
                reply = self.bridge(HANDLERS[req.operation],
                                    {"query": req.query or "", "limit": req.limit}, True)
                result = ResultPayload(summary=json.dumps({
                    "name": reply.get("name"), "object_count": reply.get("object_count"),
                    "objects": reply.get("objects", []),
                }, ensure_ascii=False))
            elif req.operation == "studio_scene_snapshot":
                from .snapshot_store import SnapshotStore, SnapshotError
                root = os.getenv("BLENDER_MCP_STUDIO_ROOT", "")
                try:
                    store = SnapshotStore(root)
                    snapshot_id, path, replay = store.begin_keyed(
                        scene_id=str(req.scene_id), scene_revision=req.scene_revision,
                        idempotency_key=req.idempotency_key, label=req.label or "")
                    if replay:
                        artifact = store.verify(snapshot_id)
                    else:
                        saved = self.bridge(HANDLERS[req.operation], {
                            "scene_id": str(req.scene_id),
                            "scene_revision": req.scene_revision,
                            "snapshot_id": snapshot_id,
                            "path": str(path),
                        }, False)
                        if saved.get("scene_id") != str(req.scene_id) or saved.get("scene_revision") != req.scene_revision:
                            raise SnapshotError("Blender snapshot reply has mismatched scene context")
                        artifact = store.finalize(
                            snapshot_id, scene_id=str(req.scene_id),
                            scene_revision=req.scene_revision,
                            idempotency_key=req.idempotency_key)
                        if saved.get("sha256") != artifact.sha256:
                            raise SnapshotError("Blender snapshot checksum mismatch")
                    reply = {"scene_id": artifact.scene_id,
                             "scene_revision": artifact.scene_revision}
                    result = ResultPayload(
                        snapshot_id=UUID(artifact.snapshot_id),
                        summary=("Snapshot replay: " if replay else "Snapshot written: ")
                                + artifact.sha256)
                except (SnapshotError, OSError) as exc:
                    return self._failed(raw, ErrorCode.CONFLICT,
                                        "Snapshot failed or execution state unknown: " + type(exc).__name__)
            elif req.operation == "studio_create_object":
                if not isinstance(req.spec, PrimitiveSpec):
                    return self._failed(raw, ErrorCode.CAPABILITY_UNAVAILABLE,
                                        "Text creation belongs to BAS-07")
                reply = self.bridge(HANDLERS[req.operation], {
                    "scene_id": str(req.scene_id),
                    "scene_revision": req.scene_revision,
                    "idempotency_key": req.idempotency_key,
                    "name": req.name,
                    "primitive": req.spec.primitive,
                    "location": list(req.location),
                }, False)
                obj = ObjectRef(id=UUID(reply["object_id"]), name=reply["name"], kind="object")
                result = ResultPayload(objects=[obj],
                                       scene_diff=SceneDiff(created=[obj.id]),
                                       summary="Primitive created" if not reply.get("replayed") else "Idempotent replay")
            else:
                return self._failed(raw, ErrorCode.CAPABILITY_UNAVAILABLE, "No BAS-05 handler")
            response = StudioResponse(
                request_id=req.request_id, scene_id=UUID(reply["scene_id"]),
                scene_revision=reply["scene_revision"], status="succeeded", result=result,
            )
            return response.model_dump(mode="json")
        except ValidationError as exc:
            return self._failed(raw, ErrorCode.INVALID_INPUT, exc.errors()[0].get("msg", "Invalid Studio request"))
        except PolicyDenied as exc:
            return self._failed(raw, ErrorCode.PERMISSION_DENIED, str(exc))
        except ValueError as exc:
            if str(exc) == ErrorCode.CAPABILITY_UNAVAILABLE.value:
                code = ErrorCode.CAPABILITY_UNAVAILABLE
            elif "stale_scene" in str(exc):
                code = ErrorCode.STALE_SCENE
            elif "conflict" in str(exc):
                code = ErrorCode.CONFLICT
            else:
                code = ErrorCode.INTERNAL
            return self._failed(raw, code, str(exc))
        except Exception as exc:
            # Existing BlenderConnection wraps addon error messages in generic
            # exceptions. Recover ONLY explicit fixed error markers, never
            # reflect arbitrary details or tracebacks to a model.
            message = str(exc)
            if "stale_scene:" in message:
                return self._failed(raw, ErrorCode.STALE_SCENE, "Scene revision mismatch")
            if "conflict:" in message:
                return self._failed(raw, ErrorCode.CONFLICT, "Idempotency key conflict")
            return self._failed(raw, ErrorCode.INTERNAL, type(exc).__name__)

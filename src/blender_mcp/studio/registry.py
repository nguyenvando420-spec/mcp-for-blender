"""BAS-05: bounded, fail-closed typed Studio operation registry.

An explicit allowlist, not a Python execution tunnel. Only v1 inspect and
primitive-create have Blender handlers here; jobs, persistence and remote
authentication are intentionally NOT advertised as executable capabilities.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import UUID
from pydantic import ValidationError

from .contracts import (
    Capabilities, ContractVersion, ErrorCode, ObjectRef, PrimitiveSpec, TextSpec,
    ResultPayload, SceneDiff, StudioError, StudioResponse, ensure_supported,
)
from ..security_policy import Policy, PolicyDenied, authorize_studio_request

ADDON_PROTOCOL_MIN = 15
# Operation -> command implemented in the paired BAS-05 Blender addon.
HANDLERS = {
    "studio_scene_inspect": "studio_scene_inspect_v1",
    "studio_create_object": "studio_create_object_v1",
    "studio_set_transform": "studio_set_transform_v1",
    "studio_manage_collection": "studio_manage_collection_v1",
}
READ_TOOLS = frozenset({"studio_handshake", "studio_scene_inspect"})
MUTATION_TOOLS = frozenset({"studio_create_object", "studio_set_transform", "studio_manage_collection"})


def visible_studio_tools(profile):
    """No remote/worker tools without a trusted authenticated entrypoint."""
    from ..security_policy import Profile
    if profile in (Profile.OBSERVER, Profile.STUDIO_LOCAL, Profile.DEVELOPER_LOCAL):
        return READ_TOOLS | (MUTATION_TOOLS if profile != Profile.OBSERVER else frozenset())
    return frozenset()


def _validate_numeric_bounds(req):
    """Second-line limits on top of BAS-03's strict wire types.

    These limits are additive runtime policy; canonical v1 JSON Schema remains
    unchanged until an explicit contract-version/schema regeneration.
    """
    if req.operation not in ("studio_create_object", "studio_set_transform"):
        return
    for field, limit in (("location", 100000.0), ("rotation_euler", 1000.0),
                         ("scale", 1000.0)):
        values = getattr(req, field, None)
        if values is None:
            continue
        if any(not math.isfinite(v) or abs(v) > limit for v in values):
            raise ValueError("invalid_input: out-of-range " + field)
        if field == "scale" and any(v <= 0 for v in values):
            raise ValueError("invalid_input: scale must be positive")
    if req.operation == "studio_set_transform" and req.action == "duplicate":
        if req.name is None or any(getattr(req, f) is not None for f in
                                    ("location", "rotation_euler", "scale")):
            raise ValueError("invalid_input: duplicate requires name only")


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
                            features=({"primitive-create-v1", "text-create-v1"} if "studio_create_object" in available and "studio_create_text_v1" in advertised else ({"primitive-create-v1"} if "studio_create_object" in available else set()))
                            | ({"scene-inspect-v1"} if "studio_scene_inspect" in available else set())
                            | ({"object-transforms-v1"} if "studio_set_transform" in available else set())
                            | ({"collections-v1"} if "studio_manage_collection" in available else set()))

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
            _validate_numeric_bounds(req)
            if req.operation == "studio_scene_inspect":
                reply = self.bridge(HANDLERS[req.operation],
                                    {"query": req.query or "", "limit": req.limit}, True)
                result = ResultPayload(summary=json.dumps({
                    "name": reply.get("name"), "object_count": reply.get("object_count"),
                    "objects": reply.get("objects", []),
                }, ensure_ascii=False))
            elif req.operation == "studio_create_object":
                args = {
                    "scene_id": str(req.scene_id),
                    "scene_revision": req.scene_revision,
                    "idempotency_key": req.idempotency_key,
                    "name": req.name,
                    "location": list(req.location),
                }
                if isinstance(req.spec, PrimitiveSpec):
                    args["primitive"] = req.spec.primitive
                    command = HANDLERS[req.operation]
                elif isinstance(req.spec, TextSpec):
                    args["text"] = req.spec.text
                    command = "studio_create_text_v1"
                    if command not in set(self._info().get("capabilities") or []):
                        return self._failed(raw, ErrorCode.CAPABILITY_UNAVAILABLE,
                                            "Blender add-on lacks text creation")
                else:
                    return self._failed(raw, ErrorCode.CAPABILITY_UNAVAILABLE,
                                        "Unsupported creation variant")
                reply = self.bridge(command, args, False)
                obj = ObjectRef(id=UUID(reply["object_id"]), name=reply["name"], kind="object")
                result = ResultPayload(objects=[obj],
                                       scene_diff=SceneDiff(created=[obj.id]),
                                       summary="Object created" if not reply.get("replayed") else "Idempotent replay")
            elif req.operation == "studio_set_transform":
                args = {
                    "scene_id": str(req.scene_id), "scene_revision": req.scene_revision,
                    "idempotency_key": req.idempotency_key, "object_id": str(req.object_id),
                    "action": req.action, "name": req.name,
                    "location": list(req.location) if req.location is not None else None,
                    "rotation_euler": list(req.rotation_euler) if req.rotation_euler is not None else None,
                    "scale": list(req.scale) if req.scale is not None else None,
                }
                reply = self.bridge(HANDLERS[req.operation], args, False)
                oid = UUID(reply["object_id"])
                obj = ObjectRef(id=oid, name=reply["name"], kind="object")
                result = ResultPayload(
                    objects=[obj],
                    scene_diff=SceneDiff(created=[oid] if req.action == "duplicate" else [],
                                         updated=[] if req.action == "duplicate" else [oid]),
                    summary="Idempotent replay" if reply.get("replayed") else req.action,
                )
            elif req.operation == "studio_manage_collection":
                reply = self.bridge(HANDLERS[req.operation], {
                    "scene_id": str(req.scene_id), "scene_revision": req.scene_revision,
                    "idempotency_key": req.idempotency_key, "action": req.action,
                    "name": req.name,
                    "collection_id": str(req.collection_id) if req.collection_id else None,
                    "object_id": str(req.object_id) if req.object_id else None,
                }, False)
                ident = UUID(reply["object_id"])
                kind = "object" if req.action == "move_object" else "collection"
                obj = ObjectRef(id=ident, name=reply["name"], kind=kind)
                result = ResultPayload(
                    objects=[obj],
                    scene_diff=SceneDiff(created=[ident] if req.action == "create" else [],
                                         updated=[] if req.action == "create" else [ident]),
                    summary="Idempotent replay" if reply.get("replayed") else req.action,
                )
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
            if str(exc).startswith("invalid_input:"):
                code = ErrorCode.INVALID_INPUT
            elif str(exc) == ErrorCode.CAPABILITY_UNAVAILABLE.value:
                code = ErrorCode.CAPABILITY_UNAVAILABLE
            elif "stale_scene" in str(exc):
                code = ErrorCode.STALE_SCENE
            elif "object_not_found:" in str(exc):
                code = ErrorCode.OBJECT_NOT_FOUND
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
            if "invalid_input:" in message:
                return self._failed(raw, ErrorCode.INVALID_INPUT, "Invalid Blender object arguments")
            if "object_not_found:" in message:
                return self._failed(raw, ErrorCode.OBJECT_NOT_FOUND, "Object or collection not found")
            if "conflict:" in message:
                return self._failed(raw, ErrorCode.CONFLICT, "Idempotency key conflict")
            return self._failed(raw, ErrorCode.INTERNAL, type(exc).__name__)

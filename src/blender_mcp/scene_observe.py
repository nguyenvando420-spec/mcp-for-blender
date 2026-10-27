"""BAS-08: bounded, read-only Blender scene observation primitives.

This module is intentionally independent of MCP transport. All calls must be
made from Blender's main thread; gateway wiring and capture are separate gates.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid

ID_KEY = "studio_object_id"
MAX_PAGE_SIZE = 200


def _number(value):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError("Non-finite scene coordinate")
    return round(n, 7)


def _vector(items):
    return [_number(x) for x in items]


def _object_id(obj, *, assign=False):
    object_id = obj.get(ID_KEY)
    if isinstance(object_id, str):
        try:
            return str(uuid.UUID(object_id))
        except ValueError:
            pass
    if not assign:
        return None
    new_id = str(uuid.uuid4())
    obj[ID_KEY] = new_id
    return new_id


def observe_scene(scene, *, offset=0, limit=100, object_id=None, assign_ids=False):
    """Return deterministic paged state. assign_ids=True is an explicit mutation.

    The caller must enforce permissions for assign_ids. The no-write default
    allows observer profiles to inspect an arbitrary Blender scene safely.
    Object lookup is performed BEFORE paging, so it cannot disappear due to
    client-side page limits.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError("limit must be between 1 and 200")
    if object_id is not None:
        object_id = str(uuid.UUID(object_id))

    objects = sorted(scene.objects, key=lambda x: (x.name, x.type))
    ids = {}
    seen = set()
    for obj in objects:
        stable_id = _object_id(obj, assign=assign_ids)
        if stable_id is not None and stable_id in seen:
            # Duplicating Blender objects copies custom properties. Never
            # silently reassign IDs in read-only observation.
            if assign_ids:
                stable_id = str(uuid.uuid4())
                obj[ID_KEY] = stable_id
            else:
                stable_id = None
        if stable_id is not None:
            seen.add(stable_id)
        ids[id(obj)] = stable_id

    if object_id is not None:
        matches = [obj for obj in objects if ids[id(obj)] == object_id]
        if not matches:
            raise LookupError("object_id not found; refresh scene summary")
        objects = matches

    records = []
    for obj in objects[offset:offset + limit]:
        box = []
        if getattr(obj, "type", None) in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            box = [_vector(obj.matrix_world @ corner) for corner in obj.bound_box]
        records.append({
            "id": ids[id(obj)],
            "name": obj.name,
            "type": obj.type,
            "selected": bool(obj.select_get()),
            "location": _vector(obj.location),
            "rotation_euler": _vector(obj.rotation_euler),
            "scale": _vector(obj.scale),
            "world_bounds": {
                "min": [min(v[i] for v in box) for i in range(3)],
                "max": [max(v[i] for v in box) for i in range(3)],
            } if box else None,
            "collections": sorted(c.name for c in obj.users_collection),
            "materials": [
                {"name": slot.material.name if slot.material else None}
                for slot in obj.material_slots
            ],
        })

    payload = {
        "scene": scene.name,
        "total": len(objects),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(objects),
        "objects": records,
    }
    return payload


def state_digest(summary):
    """Stable digest of the *complete* read-only summary, not a revision counter.

    Callers must fetch all pages and include every object before interpreting
    this as an out-of-band edit detector. It is not a security confirmation.
    """
    canonical = json.dumps(summary, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

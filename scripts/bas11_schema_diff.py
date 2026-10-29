"""Report runtime versus committed Studio request-schema drift in CI."""
import json
from pathlib import Path

from blender_mcp.studio.contracts import json_schemas

root = Path(__file__).resolve().parents[1]
saved = json.loads((root / "docs/schemas/studio-request-v1.schema.json").read_text())
runtime = json_schemas()["request"]


def compare(a, b, path="$"):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(a.keys() | b.keys()):
            if key not in a:
                print("MISSING_SAVED", path + "." + key, json.dumps(b[key])[:1200])
            elif key not in b:
                print("EXTRA_SAVED", path + "." + key, json.dumps(a[key])[:1200])
            else:
                compare(a[key], b[key], path + "." + key)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            print("LIST_LENGTH", path, len(a), len(b))
        for index, (left, right) in enumerate(zip(a, b)):
            compare(left, right, path + "[" + str(index) + "]")
    elif a != b:
        print("DIFF", path, "saved=", repr(a)[:200], "runtime=", repr(b)[:200])


compare(saved, runtime)
print("BAS11_SCHEMA_DRIFT_CHECK_COMPLETE")

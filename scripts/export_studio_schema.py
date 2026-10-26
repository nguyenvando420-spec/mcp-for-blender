"""Write BAS-03 JSON Schemas from the runtime Pydantic models."""

import json
from pathlib import Path

from blender_mcp.studio.contracts import json_schemas


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "docs" / "schemas"
    target.mkdir(parents=True, exist_ok=True)
    for name, schema in json_schemas().items():
        (target / f"studio-{name}-v1.schema.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()

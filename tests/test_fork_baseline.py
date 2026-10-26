"""BAS-01 fork-invariants: catch packaging drift without importing bpy."""

from __future__ import annotations

import ast
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
ROOT_ADDON = ROOT / "addon.py"
BUNDLED_ADDON = ROOT / "src" / "blender_mcp" / "bundled" / "addon.py"
MANAGER = ROOT / "src" / "blender_mcp" / "addon_manager.py"


def _integer_constant(path: Path, name: str) -> int:
    """Read a top-level literal assignment without importing bpy."""
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for stmt in module.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in stmt.targets):
            value = ast.literal_eval(stmt.value)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            break
    raise AssertionError(f"{name} must be a literal integer in {path}")


def test_root_and_bundled_addons_are_identical() -> None:
    # install-addon deploys the bundled copy, not the root source file.
    assert ROOT_ADDON.read_bytes() == BUNDLED_ADDON.read_bytes(), (
        "addon.py and src/blender_mcp/bundled/addon.py diverged; "
        "copy the updated addon to both locations before release"
    )


def test_addon_protocol_is_consistent() -> None:
    expected = _integer_constant(MANAGER, "EXPECTED_ADDON_PROTOCOL_VERSION")
    assert expected > 0
    assert _integer_constant(ROOT_ADDON, "ADDON_PROTOCOL_VERSION") == expected
    assert _integer_constant(BUNDLED_ADDON, "ADDON_PROTOCOL_VERSION") == expected


def test_local_package_entrypoint() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["name"] == "mcp-for-blender"
    assert config["project"]["scripts"]["mcp-for-blender"] == "blender_mcp.server:main"
    assert config["tool"]["setuptools"]["package-data"]["blender_mcp"]
    assert (ROOT / "uv.lock").is_file()
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12.14"

"""Install checksum-pinned Blender and collect golden-scene evidence (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(config: dict, cache: Path) -> tuple[Path, dict]:
    key = f"{platform.system()}-{platform.machine()}"
    if key not in config["artifacts"]:
        raise RuntimeError(f"Unqualified platform: {key}; see docs/BAS-02-runtime.md")
    spec = config["artifacts"][key]
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec["filename"]
    if not archive.is_file() or digest(archive) != spec["sha256"]:
        partial = archive.with_suffix(archive.suffix + ".part")
        for url in spec["urls"]:
            try:
                print(f"Downloading {url}", flush=True)
                with urlopen(url, timeout=60) as source, partial.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                if digest(partial) != spec["sha256"]:
                    raise RuntimeError("Archive checksum mismatch")
                partial.replace(archive)
                break
            except Exception as exc:
                partial.unlink(missing_ok=True)
                print(f"Download failed: {exc}", flush=True)
        else:
            raise RuntimeError("All pinned download sources failed")
    # Re-extract each time: an executable left in the cache is not proof of provenance.
    destination = cache / "runtime"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    if archive.name.endswith(".tar.xz"):
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
    else:
        with tempfile.TemporaryDirectory(prefix="bas02-mount-") as mount:
            subprocess.run(["hdiutil", "attach", str(archive), "-readonly", "-nobrowse",
                            "-mountpoint", mount], check=True)
            try:
                subprocess.run(["ditto", str(Path(mount) / "Blender.app"),
                                str(destination / "Blender.app")], check=True)
            finally:
                subprocess.run(["hdiutil", "detach", mount], check=True)
    return destination / spec["executable"], {"platform": key, **spec}


def run_blender(executable: Path, script: str, output: Path, log: str, version: str) -> None:
    command = [str(executable), "--background", "--factory-startup", "--disable-autoexec",
               "--python-exit-code", "1", "--python", str(ROOT / "scripts" / script),
               "--", "--output-dir", str(output), "--expected-version", version]
    with (output / log).open("w") as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True,
                       timeout=300, env={**os.environ, "DISABLE_TELEMETRY": "true"})


def verify_bundle(output: Path) -> dict:
    manifest = json.loads((output / "manifest.json").read_text())
    required = {"baseline.blend", "preview.png", "render.log", "reopen.log",
                "blender-environment.json", "validation.json"}
    if manifest.get("schema_version") != 1 or not required.issubset(manifest["files"]):
        raise ValueError("Incomplete evidence manifest")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid evidence path: {name}")
        path = output / relative
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Missing or corrupted evidence: {name}")
    validation = json.loads((output / "validation.json").read_text())
    if validation != manifest.get("validation") or not validation.get("passed"):
        raise ValueError("Bundle did not pass Blender reopen validation")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".blender-runtime")
    parser.add_argument("--verify-only", action="store_true",
                        help="Check stored hashes without downloading Blender")
    parser.add_argument("--reopen", action="store_true",
                        help="Verify an existing bundle then reopen its BLEND with pinned Blender")
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    config = json.loads((ROOT / "blender-runtime.json").read_text())
    if args.verify_only or args.reopen:
        verify_bundle(output)
        if args.verify_only:
            print("BAS02_BUNDLE_OK")
            return
    if not args.reopen and output.exists() and any(output.iterdir()):
        raise ValueError("Use a new output directory; evidence is never mixed across runs")
    if platform.python_version() != config["gateway_python"]:
        raise RuntimeError(f"Gateway requires Python {config['gateway_python']}; got {platform.python_version()}")
    output.mkdir(parents=True, exist_ok=True)
    executable, archive = install(config, args.cache_dir.expanduser().resolve())
    if args.reopen:
        # Recovery logs must not mutate the original evidence bundle.
        with tempfile.TemporaryDirectory(prefix="bas02-recovery-") as temp:
            recovery = Path(temp)
            for name in ("baseline.blend", "preview.png"):
                shutil.copy2(output / name, recovery / name)
            run_blender(executable, "verify_golden.py", recovery, "recovery.log", config["blender_version"])
            print((recovery / "recovery.log").read_text())
        print("BAS02_RECOVERY_OK")
        return
    run_blender(executable, "blender_headless_smoke.py", output, "render.log", config["blender_version"])
    run_blender(executable, "verify_golden.py", output, "reopen.log", config["blender_version"])
    source = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True, check=True).stdout.strip()
    manifest = {
        "schema_version": 1,
        "source_commit": source,
        "source_dirty": bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                             capture_output=True, text=True, check=True).stdout),
        "gateway": {"python": platform.python_version(), "os": platform.system(),
                    "os_release": platform.release(), "architecture": platform.machine()},
        "runtime_archive": archive,
        "blender": json.loads((output / "blender-environment.json").read_text()),
        "validation": json.loads((output / "validation.json").read_text()),
        "files": {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify_bundle(output)
    print(f"BAS02_GOLDEN_OK {output}")


if __name__ == "__main__":
    main()

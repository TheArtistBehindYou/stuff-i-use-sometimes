"""Build distribution artifacts for tsproj2fla.

Produces, under ``dist/``:

* ``tsproj2fla.py``  - a single-file, dependency-light script (needs only lz4+Pillow)
* ``tsproj2fla.zip`` - the full source tree (package + tests + docs + samples)
* ``tsproj2fla-<version>-py3-none-any.whl`` and ``.tar.gz`` - installable packages

Run with ``python tools/build_dist.py``.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PACKAGE = ROOT / "tsproj2fla"

HEADER = '''#!/usr/bin/env python3
"""tsproj2fla - convert ToonSquid (.tsproj) projects to Adobe Animate (.fla).

Single-file build. Requires only: lz4, Pillow.

Usage:
    python tsproj2fla.py INPUT.tsproj [-o OUTPUT.fla] [--list]
"""
'''

MODULE_ORDER = ["dvtx", "tsproj", "converter", "__main__"]


def _version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'version = "([^"]+)"', text).group(1)


def _strip_relative_imports(source: str) -> str:
    source = re.sub(r"^from \.\w+ import .*$", "", source, flags=re.M)
    source = re.sub(r"^from \.__main__ import .*$", "", source, flags=re.M)
    source = re.sub(r"^from \. import .*$", "", source, flags=re.M)
    source = re.sub(r"^from __future__ import .*$", "", source, flags=re.M)
    return source


def build_single_file() -> Path:
    parts = [HEADER, "from __future__ import annotations\n"]
    for name in MODULE_ORDER:
        src = (PACKAGE / f"{name}.py").read_text()
        parts.append(f"\n# ===== tsproj2fla.{name} =====\n")
        parts.append(_strip_relative_imports(src))
    combined = "\n".join(parts)
    # drop the __main__ guard so the file can simply be executed
    out = DIST / "tsproj2fla.py"
    out.write_text(combined)
    return out


def build_source_zip() -> Path:
    out = DIST / "tsproj2fla.zip"
    include = [
        "tsproj2fla/__init__.py",
        "tsproj2fla/__main__.py",
        "tsproj2fla/converter.py",
        "tsproj2fla/dvtx.py",
        "tsproj2fla/tsproj.py",
        "tests/test_tsproj2fla.py",
        "tools/build_dist.py",
        "pyproject.toml",
        "requirements.txt",
        "README.md",
        "AGENTS.md",
        ".gitignore",
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in include:
            path = ROOT / rel
            if path.exists():
                zf.write(path, f"tsproj2fla/{rel}")
    return out


def build_wheel() -> list[Path]:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "build"],
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(DIST), str(ROOT)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
    return sorted(DIST.glob(f"tsproj2fla-{_version()}*"))


def main() -> int:
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("*"):
        if old.is_file():
            old.unlink()

    single = build_single_file()
    archive = build_source_zip()
    print(f"single-file : {single} ({single.stat().st_size/1024:.1f} KiB)")
    print(f"source zip  : {archive} ({archive.stat().st_size/1024:.1f} KiB)")

    wheels = build_wheel()
    for wheel in wheels:
        print(f"package     : {wheel} ({wheel.stat().st_size/1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build a standalone tsproj2fla executable with PyInstaller.

The result bundles Python plus the Pillow and lz4 libraries, so end users can
run it with nothing installed. It must be run on the target operating system:
PyInstaller cannot cross-compile (build the Windows .exe on Windows).

Usage (Windows):
    pip install pyinstaller
    python tools/build_exe.py

The executable is written to dist/ as ``tsproj2fla.exe`` (Windows) or
``tsproj2fla`` (macOS/Linux).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def _entry_script() -> Path:
    """Use the self-contained single-file build as the PyInstaller entry point.

    The package's ``__main__.py`` uses relative imports, which PyInstaller
    cannot resolve in a frozen one-file executable; the bundled single-file
    script has no such imports.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import build_dist  # noqa: PLC0415

    DIST.mkdir(exist_ok=True)
    return build_dist.build_single_file()


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run:  pip install pyinstaller")
        return 1

    entry = _entry_script()
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "tsproj2fla",
        "--distpath",
        str(DIST),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--collect-all",
        "PIL",
        "--collect-all",
        "lz4",
        str(entry),
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        name = "tsproj2fla.exe" if sys.platform == "win32" else "tsproj2fla"
        print(f"\nBuilt: {DIST / name}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

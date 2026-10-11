"""Command line entry point: ``python -m tsproj2fla input.tsproj [-o out.fla]``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .converter import convert
from .tsproj import load_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tsproj2fla",
        description="Convert a ToonSquid .tsproj project into an Adobe Animate .fla file.",
    )
    parser.add_argument("input", type=Path, help="path to the .tsproj file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="output .fla path (default: alongside the input, same stem)",
    )
    parser.add_argument(
        "--no-reverse-layers", action="store_true",
        help="keep ToonSquid's top-to-bottom layer order instead of reversing it",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="only print a summary of the project and exit",
    )
    args = parser.parse_args(argv)

    if not args.input.exists():
        parser.error(f"input file not found: {args.input}")

    project = load_project(args.input)
    print(f"Project : {project.name} ({project.width}x{project.height} @ {project.fps}fps)")
    print(f"Scenes  : {len(project.scene_ids)}")
    print(f"Clips   : {len(project.clips)}")
    print(f"Textures: {len(project.textures)}")
    print(f"Audio   : {len(project.audio)}")

    if args.list:
        for scene in project.scenes:
            print(f"  scene {scene.name!r}: {len(scene.layer_order)} layers, {scene.total_frames} frames")
        return 0

    output = args.output or args.input.with_suffix(".fla")
    data, warnings = convert(project, reverse_layers=not args.no_reverse_layers)
    output.write_bytes(data)

    print(f"Wrote   : {output} ({len(data) / 1024:.1f} KiB)")
    for warning in warnings:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

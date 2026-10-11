# tsproj2fla

Convert a [ToonSquid](https://toonsquid.com) project (`.tsproj`) into an Adobe
Animate document (`.fla`).

The tool reverse-engineers the ToonSquid container (a ZIP of SQLite databases
plus a custom `DVTX` texture codec) and emits an **uncompressed XFL** archive,
which is exactly what a post-CS5 `.fla` file is. Adobe Animate opens XFL files
the same way it opens `.fla` files, so the result can be opened directly, or
re-saved as a compressed `.fla`.

## Install

```bash
pip install -r requirements.txt      # lz4 + pillow
pip install -e .                     # optional: installs the `tsproj2fla` command
```

Requires Python 3.10+.

### Just want the tool? Grab a prebuilt file

Everything in [`dist/`](dist/) is ready to use:

| File | What it is | How to run |
| --- | --- | --- |
| `dist/tsproj2fla.py` | Single self-contained script (no install) | `python tsproj2fla.py INPUT.tsproj` |
| `dist/tsproj2fla.zip` | Full source tree (package, tests, docs) | unzip, then `python -m tsproj2fla ...` |
| `dist/tsproj2fla-0.1.0-py3-none-any.whl` | Installable package | `pip install dist/*.whl` |
| `dist/tsproj2fla-0.1.0.tar.gz` | Source distribution | `pip install dist/*.tar.gz` |

The single-file script and the wheel both need only `lz4` and `Pillow`:

```bash
pip install lz4 pillow
python dist/tsproj2fla.py MyProject.tsproj -o Out.fla
```

Rebuild these artifacts at any time with `python tools/build_dist.py`.

### Drag and drop (no command line)

If you'd rather not use a terminal, use a launcher from [`launchers/`](launchers/):

| Your OS | File | How |
| --- | --- | --- |
| Windows | `launchers/Convert-to-FLA.bat` | Drag your `.tsproj` onto it |
| macOS | `launchers/Convert-to-FLA.command` | Drag your `.tsproj` onto it |
| Linux | `launchers/convert-to-fla.sh` | Drag your `.tsproj` onto it (file-manager action) |

Each launcher converts every dropped file and keeps the window open so you can
read the result. They find `tsproj2fla.py` next to themselves, then in `dist/`,
then fall back to an installed `tsproj2fla` command — so either download the
single-file script next to the launcher, or install the wheel. You can also drop
several `.tsproj` files at once.

> macOS may require `chmod +x Convert-to-FLA.command` the first time, and
> Gatekeeper may need a right-click → Open to allow it. Linux file managers
> vary; the `.sh` also works fine from a terminal.

On **Windows**, the launcher installs the two required libraries (Pillow and
lz4) automatically on first run, so you never have to run `pip` yourself.

### Standalone executable (nothing to install)

To run with no Python and no dependencies at all, build a single-file
executable that bundles everything:

```bash
pip install pyinstaller pillow lz4
python tools/build_exe.py          # -> dist/tsproj2fla.exe  (Windows)
```

This must be run **on the target OS** — PyInstaller cannot build a Windows
`.exe` from macOS or Linux. A GitHub Actions workflow
([`.github/workflows/build-exe.yml`](.github/workflows/build-exe.yml)) does it
for you: go to the repo's **Actions → build-exe → Run workflow**, or push a
`v*` tag, and download the Windows/macOS/Linux executables from the run's
artifacts.


## Usage

```bash
# convert a project next to the source file
python -m tsproj2fla MyProject.tsproj

# choose the output path
python -m tsproj2fla MyProject.tsproj -o Out.fla

# just inspect the project
python -m tsproj2fla MyProject.tsproj --list
```

Or from Python:

```python
from tsproj2fla import load_project, convert

project = load_project("MyProject.tsproj")
fla_bytes, warnings = convert(project)
open("Out.fla", "wb").write(fla_bytes)
```

## What gets converted

| ToonSquid | Adobe Animate |
| --- | --- |
| Scene / animation clip | `DOMTimeline` (scene) and library symbols (movie clips) |
| Vector layer (fills + strokes) | `DOMShape` with an XFL `edges` string |
| Pixel / painted layer | `DOMBitmapInstance` + `DOMBitmapItem` (`LIBRARY/*.png`, `bin/*.dat`) |
| Text layer | `DOMStaticText` |
| Symbol / nested clip layer | `DOMSymbolInstance` referencing a library symbol |
| Group layer | flattened into its child layers |
| Frame-by-frame drawings | one keyframe per drawing span |
| Layer transform (position, scale, rotation, pivot) | `DOMShape`/instance `<Matrix>` |
| Layer order | reversed to match Animate's bottom-first timeline |

Run `tsproj2fla --list` to see the scenes, clips, textures and audio found in a
project before converting.

## How it works

`tsproj2fla` reads the container directly; nothing is re-encoded lossily.

1. **`tsproj.py`** unzips the `.tsproj`, opens the embedded SQLite databases
   (`data` and every `Animations/<uuid>.tsanim`) and decodes the JSON blobs they
   store for scenes, layers, drawings, paths and transforms.
2. **`dvtx.py`** decodes ToonSquid's `DVTX` texture format: a small header
   followed by `bv41` chunks, each an independent LZ4 block that shares the
   previous block as a dictionary. The concatenated result is a flat RGBA
   buffer, which becomes the exported PNG.
3. **`converter.py`** maps the ToonSquid layer graph onto XFL elements and
   writes the archive (`DOMDocument.xml`, `PublishSettings.xml`, `LIBRARY/*`,
   `bin/*`, `META-INF/metadata.xml`).

Both ToonSquid schemas are handled:

* **1.x** — `tf` keyframe tracks (`pX`/`pY`/`sX`/`sY`/`r`) with pivot `pi`, and
  paths as quadratic `cp` control points.
* **2.x** — static `tf` (`p`/`pv`/`s`), constant colours, and cubic Bézier
  subpaths (`p` with `i`/`o` handles).

## Known limitations

* **Animation is baked per drawing span.** ToonSquid interpolates transform
  keyframes continuously; the converter emits one keyframe per drawing (with the
  transform sampled at the drawing's first frame) and does not reproduce tweened
  motion, easing, or per-frame colour changes as Animate motion tweens.
* **Audio and video are not embedded.** ToonSquid audio metadata is parsed, but
  clips are not yet attached to XFL sound layers.
* **Some layer types are skipped:** camera, scroll/parallax, adjustment/effect
  and background layers are dropped (they have no direct XFL equivalent).
* **Strokes are solid only.** ToonSquid's textured/variable-width brushes become
  a solid stroke of the average width; SVG-like stroke alignment is approximated.
* **Text** is emitted as static text using the layer's font name as-is; the font
  itself is not embedded, so Animate substitutes a system font.
* **No automatic validation inside Adobe Animate** — the output is structurally
  valid XFL, but always spot-check the result.

## Tests

```bash
python -m pytest tests/ -q
```

The suite builds a small synthetic `.tsproj` in memory (including a hand-encoded
`DVTX` texture) and asserts the converter produces well-formed XFL with the
expected shape, bitmap and symbol elements. No demo projects or network access
are required.

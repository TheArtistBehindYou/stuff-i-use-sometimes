# AGENTS.md

## Project

`tsproj2fla` converts ToonSquid `.tsproj` projects into Adobe Animate `.fla`
(uncompressed XFL) files. Pure Python; no Adobe tooling required to run.

## Layout

- `tsproj2fla/dvtx.py` — `DVTX` texture codec (header + `bv41` LZ4 block chunks).
- `tsproj2fla/tsproj.py` — ZIP/SQLite container reader → `Project`/`Clip`.
- `tsproj2fla/converter.py` — layer graph → XFL elements + `.fla` zip writer.
- `tsproj2fla/__main__.py` — CLI (`python -m tsproj2fla`).
- `launchers/` — drag-and-drop launchers (`.bat`, `.command`, `.sh`).
- `tests/test_tsproj2fla.py` — builds a synthetic `.tsproj` in memory.

## Commands

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                 # tests (no network / demo files needed)
python -m tsproj2fla FILE.tsproj -o OUT.fla
python -m tsproj2fla FILE.tsproj --list    # inspect a project
```

## Format notes (reverse-engineered)

**`.tsproj`** is a ZIP: `<uuid>/data` (SQLite), `<uuid>/Animations/<id>.tsanim`
(SQLite per timeline), `<uuid>/Textures/<id>` (DVTX), `<uuid>/Audio/<id>` +
`.wav`, `<uuid>/Thumbnails/*`. All structured values are JSON blobs in SQLite.

- `data` tables: `meta_d` (JSON under key `me`: `na` name, `re` {w,h}, `fr` fps),
  `scenes` (id, `"index"` — quoted, it is a reserved word), `res_anim_clips`,
  `res_audio_clips`, `res_shared_textures` (ownerId→textureId + dest rect).
- `.tsanim` tables: `animationClip` (JSON: `na`, `fr`, `tF` total frames, `lO`
  layer order), `layers` (id, integer `type`, JSON), `drawings` (JSON: `s`/`e`
  frame span, `ly` child layer ids).
- Layer types: 0 pixel, 2 text, 3 vector, 4 symbol, 5 group, 6 camera, 7 shape,
  8 scroll, 9 audio, 10 animation, 11 background, 122 effect.

**Two ToonSquid schemas exist and both are handled:**

- 1.x: `tf` has keyframe tracks `pX`/`pY`/`sX`/`sY`/`r`, pivot in `pi`; paths are
  quadratic `cp` control points (`po` abs pos, `ou`/`in` offsets, `pT` type,
  `cl` closed).
- 2.x: `tf` is static (`p` position, `pv` pivot, `s` scale); colours are constant
  `{"c": {...}}`; paths are cubic subpaths `{"s": [{"p": [...], "c": bool}]}`
  where each point has `p` (abs) and `i`/`o` (relative handles).

**DVTX**: `DVTX` + u16 version + u16 w + u16 h, body at **offset 10**. Then
chunks `bv41` + u32 uncompressed + u32 compressed + LZ4 payload. Each block is
LZ4-compressed against the previous block as a dictionary. Concatenated output
is a flat RGBA buffer (`w*h*4`).

**XFL / `.fla`**: ZIP with `mimetype` = `application/vnd.adobe.fla`, `DOMDocument.xml`
(ns `http://ns.adobe.com/xfl/2008/`, `width`/`height`/`frameRate`), `LIBRARY/*.xml`
symbols + `LIBRARY/*.png` bitmaps, `bin/M <name> 0.dat` bitmap data.

- Edge string grammar: `!x y` moveTo, `|x y` / `/x y` lineTo, `[cx cy x y` / `]cx cy x y`
  quadratic. **Coordinates are twips** (×20). `Edge` carries
  `fillStyle0`/`fillStyle1`/`strokeStyle` indices.
  - There is **no cubic command** in the `edges` attribute — cubics live in a
    separate `cubics` attribute that only Animate uses. ToonSquid 2.x/3.x paths
    are cubic, so they must be **flattened to line-tos** (see `_flatten_cubic`).
    Emitting `(;...;);` produces a file Animate opens with an empty stage.
- Matrix is `[a b c d tx ty]`, `x' = a·x + c·y + tx`, `y' = b·x + d·y + ty`;
  rotation is clockwise in y-down space. Layers are listed bottom-first, so
  ToonSquid's top-first `lO` is reversed.

## Gotchas

- Always quote `"index"` in SQL (`ORDER BY "index"`).
- `MobileSettings.xml` is intentionally empty — exclude it from XML
  well-formedness checks.
- Transforms fold the pivot in: `T(pos)·R·S·T(-pivot)`; paths are absolute
  canvas coordinates, so do **not** subtract the pivot from path points.
- Animate silently opens an invalid `.fla` with an **empty stage** (assets still
  show in the library) rather than reporting an error. Validate output with an
  independent reader (e.g. `xfl2svg`) instead of trusting that it "opens".
- A filled shape's outline must be **explicitly closed** in `edges` (the closing
  segment back to the first point). ToonSquid stores fills as open contours.
- Every `DOMFrame` needs a `duration`; without it the layer is 1 frame long.
- A layer's first `DOMFrame` must be at `index="0"`; layers are not allowed to
  start partway down the timeline.
- Omit `fillStyle0`/`fillStyle1`/`strokeStyle` attributes when there is no such
  style. Writing `fillStyle0="0"` makes readers look up a fill style `0` that
  does not exist.
- A library item's `itemID` must be **identical** in the `<Include>` (and
  `<DOMBitmapItem>`) and in the `DOMSymbolItem` file it points at. Animate
  resolves items by `itemID`; if they differ it loads nothing and shows an empty
  stage. Never mint a fresh ID per call — cache it per item (`item_id()`).
- `mimetype` must be the **first** zip entry and stored **uncompressed**
  (`ZIP_STORED`); Animate validates this like EPUB/ODF.
- `bin/M <name> 0.dat` is **not** a PNG. It is a raw bitmap: magic `0x03 0x05`
  (32-bit) / `0x03 0x03` (8-bit), then rowSize/width/height (u16 LE), 16 reserved
  bytes, `hasAlpha` (byte 24), `variant` (byte 25), then pixels as A,R,G,B with
  colour premultiplied by alpha (stored alpha is `a+1` for partial alpha; see
  JPEXS `LosslessImageBinDataReader`). `variant=0` with exactly `w*h*4` payload
  bytes is read as a verbatim pixel plane. Store it uncompressed.

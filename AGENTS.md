# AGENTS.md

## Project

`tsproj2fla` converts ToonSquid `.tsproj` projects into Adobe Animate `.fla`
(uncompressed XFL) files. Pure Python; no Adobe tooling required to run.

## Layout

- `tsproj2fla/dvtx.py` — `DVTX` texture codec (header + `bv41` LZ4 block chunks).
- `tsproj2fla/tsproj.py` — ZIP/SQLite container reader → `Project`/`Clip`.
- `tsproj2fla/converter.py` — layer graph → XFL elements + `.fla` zip writer.
- `tsproj2fla/__main__.py` — CLI (`python -m tsproj2fla`).
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

- Edge string grammar: `!x y` moveTo, `|x y` lineTo, `[cx cy x y` quadratic,
  `(;c1x,c1y c2x,c2y ex,ey);` cubic, `/` close. **Coordinates are twips**
  (×20). `Edge` carries `fillStyle0`/`fillStyle1`/`strokeStyle` indices.
- Matrix is `[a b c d tx ty]`, `x' = a·x + c·y + tx`, `y' = b·x + d·y + ty`;
  rotation is clockwise in y-down space. Layers are listed bottom-first, so
  ToonSquid's top-first `lO` is reversed.

## Gotchas

- Always quote `"index"` in SQL (`ORDER BY "index"`).
- `MobileSettings.xml` is intentionally empty — exclude it from XML
  well-formedness checks.
- Transforms fold the pivot in: `T(pos)·R·S·T(-pivot)`; paths are absolute
  canvas coordinates, so do **not** subtract the pivot from path points.

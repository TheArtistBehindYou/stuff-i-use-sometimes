#!/usr/bin/env python3
"""tsproj2fla - convert ToonSquid (.tsproj) projects to Adobe Animate (.fla).

Single-file build, version 0.3.0. Requires only: lz4, Pillow.

Usage:
    python tsproj2fla.py INPUT.tsproj [-o OUTPUT.fla] [--list]
    python tsproj2fla.py --version
"""

from __future__ import annotations

__version__ = "0.3.0"


# ===== tsproj2fla.dvtx =====

"""Decoder for ToonSquid's ``DVTX`` pixel/texture container.

Layout (little-endian)::

    "DVTX" | u16 version | u16 width | u16 height | 4 reserved bytes
    then N blocks of: "bv41" | u32 uncompressed_len | u32 compressed_len | payload

Each block is an independent LZ4 block.  The uncompressed stream is a flat
RGBA8 buffer (``width * height * 4`` bytes, top-down).  Blocks that are not the
first are compressed against the previous block as an LZ4 dictionary, which is
how the encoder gets long-distance matches across block boundaries.
"""


import struct

import lz4.block

MAGIC = b"DVTX"
BLOCK_MAGIC = b"bv41"
HEADER_SIZE = 10  # magic(4) + version(2) + width(2) + height(2)


class DvtxError(ValueError):
    pass


def _decompress_block(payload: bytes, out_len: int, dictionary: bytes | None) -> bytes:
    kwargs = {"uncompressed_size": out_len}
    if dictionary:
        kwargs["dict"] = dictionary
    return lz4.block.decompress(payload, **kwargs)


def decode_dvtx(data: bytes) -> tuple[int, int, bytes]:
    """Return ``(width, height, rgba_bytes)`` for a ``DVTX`` texture."""
    if data[:4] != MAGIC:
        raise DvtxError(f"not a DVTX texture (magic={data[:4]!r})")
    width, height = struct.unpack("<HH", data[6:10])

    out = bytearray()
    prev: bytes | None = None
    off = HEADER_SIZE
    n = len(data)
    while off + 12 <= n and data[off:off + 4] == BLOCK_MAGIC:
        unc, comp = struct.unpack("<II", data[off + 4:off + 12])
        payload = data[off + 12:off + 12 + comp]
        if len(payload) != comp:
            raise DvtxError("truncated DVTX block")
        try:
            block = _decompress_block(payload, unc, prev)
        except Exception as exc:  # pragma: no cover - defensive
            raise DvtxError(f"failed to decode DVTX block at {off}: {exc}") from exc
        out += block
        prev = block
        off += 12 + comp

    expected = width * height * 4
    raw = bytes(out[:expected])
    if len(raw) < expected:
        raw = raw + b"\x00" * (expected - len(raw))
    return width, height, raw


# ===== tsproj2fla.tsproj =====

"""Parser for ToonSquid ``.tsproj`` project files.

A ``.tsproj`` is a ZIP archive shaped like::

    <project-uuid>/data                       # SQLite: project metadata + resource index
    <project-uuid>/Animations/<clip-uuid>.tsanim   # SQLite: one timeline/clip
    <project-uuid>/Textures/<uuid>            # DVTX pixel layers
    <project-uuid>/Audio/<uuid>               # JSON metadata
    <project-uuid>/Audio/<uuid>.wav           # audio payload
    <project-uuid>/Thumbnails/<uuid>.png

All structured data inside the SQLite databases is stored as UTF-8 JSON blobs.
This module only *reads* the container and exposes it as plain Python objects;
the translation to XFL lives in :mod:`tsproj2fla.converter`.
"""


import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any



# sqlite3.Connection.deserialize landed in Python 3.11; older runtimes fall back
# to writing the database to a temporary file.
_HAS_DESERIALIZE = hasattr(sqlite3.Connection, "deserialize")


@dataclass
class Clip:
    """A single timeline: either a project *scene* or a reusable *animation clip*."""

    id: str
    name: str
    fps: int
    width: int
    height: int
    total_frames: int
    layer_order: list[str]
    layers: dict[str, dict[str, Any]]  # id -> layer json
    layer_types: dict[str, int]        # id -> integer layer type
    drawings: dict[str, dict[str, Any]]
    is_scene: bool = False

    def layer_type(self, layer_id: str) -> int:
        return self.layer_types.get(layer_id, -1)


@dataclass
class Project:
    path: Path
    id: str
    name: str
    width: int
    height: int
    fps: int
    scene_ids: list[str]
    clips: dict[str, Clip] = field(default_factory=dict)
    textures: dict[str, tuple[int, int, bytes]] = field(default_factory=dict)
    audio: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def scenes(self) -> list[Clip]:
        return [self.clips[s] for s in self.scene_ids if s in self.clips]


@dataclass
class _SqliteHandle:
    """A SQLite connection plus the temp file backing it (if any).

    ``sqlite3.Connection`` allows neither attribute assignment nor weak
    references, so the temp path is carried alongside the connection instead of
    on it.
    """

    conn: sqlite3.Connection
    tmp_path: str | None = None

    def close(self) -> None:
        self.conn.close()
        if self.tmp_path:
            try:
                Path(self.tmp_path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - let the OS reap it
                pass


def _load_sqlite(data: bytes) -> _SqliteHandle:
    """Open an in-memory SQLite database from raw file bytes.

    Uses :meth:`sqlite3.Connection.deserialize` when available (Python 3.11+),
    which avoids touching the filesystem entirely. Older Pythons fall back to a
    temporary file.

    On Windows a temp file stays locked until its connection is closed, so the
    old code's "delete before close" raised ``PermissionError``; the caller must
    now release the handle via :meth:`_SqliteHandle.close`.
    """
    if _HAS_DESERIALIZE:
        conn = sqlite3.connect(":memory:")
        conn.deserialize(data)
        return _SqliteHandle(conn)

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        return _SqliteHandle(sqlite3.connect(tmp.name), tmp_path=tmp.name)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _json_blob(value: bytes | str) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def _read_clip(conn: sqlite3.Connection, clip_id: str, is_scene: bool) -> Clip:
    row = conn.execute("SELECT data FROM animationClip WHERE id=?", (clip_id,)).fetchone()
    meta = _json_blob(row[0]) if row else {}

    layers: dict[str, dict[str, Any]] = {}
    layer_types: dict[str, int] = {}
    for lid, ltype, data in conn.execute("SELECT id, type, data FROM layers"):
        try:
            layers[lid] = _json_blob(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            layers[lid] = {}
        layer_types[lid] = ltype

    drawings: dict[str, dict[str, Any]] = {}
    for did, data in conn.execute("SELECT id, data FROM drawings"):
        try:
            drawings[did] = _json_blob(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            drawings[did] = {}

    resolution = meta.get("re") or {}
    return Clip(
        id=clip_id,
        name=meta.get("na") or clip_id[:8],
        fps=int(meta.get("fr") or 24),
        width=int(resolution.get("w") or 0),
        height=int(resolution.get("h") or 0),
        total_frames=int(meta.get("tF") or 1),
        layer_order=list(meta.get("lO") or []),
        layers=layers,
        layer_types=layer_types,
        drawings=drawings,
        is_scene=is_scene,
    )


def load_project(path: str | Path) -> Project:
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

        root = next((n.split("/", 1)[0] for n in names if n.endswith("/data")), None)
        if root is None:
            raise ValueError("not a ToonSquid project: missing '<uuid>/data'")

        data_conn = _load_sqlite(zf.read(f"{root}/data"))

        meta = {}
        for key, value in data_conn.conn.execute("SELECT key, value FROM meta_d"):
            if key == "me":
                meta = _json_blob(value)

        scene_ids = [sid for sid, _ in data_conn.conn.execute('SELECT id, "index" FROM scenes ORDER BY "index"')]
        all_clip_ids = [cid for (cid,) in data_conn.conn.execute("SELECT id FROM res_anim_clips")]
        audio_ids = [aid for (aid,) in data_conn.conn.execute("SELECT id FROM res_audio_clips")]
        data_conn.close()

        resolution = meta.get("re") or {}
        project = Project(
            path=path,
            id=root,
            name=meta.get("na") or path.stem,
            width=int(resolution.get("w") or 0),
            height=int(resolution.get("h") or 0),
            fps=int(meta.get("fr") or 24),
            scene_ids=scene_ids,
        )

        scene_set = set(scene_ids)
        for cid in all_clip_ids:
            member = f"{root}/Animations/{cid}.tsanim"
            if member not in names:
                continue
            conn = _load_sqlite(zf.read(member))
            project.clips[cid] = _read_clip(conn.conn, cid, is_scene=cid in scene_set)
            conn.close()

        for member in names:
            if member.startswith(f"{root}/Textures/") and not member.endswith("/"):
                tid = member.rsplit("/", 1)[-1]
                try:
                    project.textures[tid] = decode_dvtx(zf.read(member))
                except Exception as exc:  # pragma: no cover - defensive
                    project.warnings.append(f"texture {tid}: {exc}")

        for aid in audio_ids:
            member = f"{root}/Audio/{aid}"
            if member not in names:
                continue
            info = _json_blob(zf.read(member))
            wav = None
            if f"{member}.wav" in names:
                wav = zf.read(f"{member}.wav")
            project.audio[aid] = {"info": info, "wav": wav}

    return project


# ===== tsproj2fla.converter =====

"""Translate a parsed ToonSquid project into Adobe Animate XFL, then zip it as .fla.

The output uses the XFL 2.2 (uncompressed FLA) layout::

    mimetype
    <name>.xfl
    DOMDocument.xml
    PublishSettings.xml
    MobileSettings.xml
    META-INF/metadata.xml
    LIBRARY/*.xml            # symbol timelines
    LIBRARY/*.png            # bitmap sources
    bin/M <name> 0.dat       # bitmap data referenced by DOMBitmapItem

Only a subset of ToonSquid's feature set maps cleanly onto XFL.  Frame-by-frame
drawings, vector paths (with fills/strokes), pixel/bitmap layers, transforms,
layer grouping and symbol clips are converted.  See the README for the
documented approximations.
"""


import io
import math
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from PIL import Image



XFL_NS = "http://ns.adobe.com/xfl/2008/"
TWIPS = 20  # XFL edge coordinates are in twips (1/20 px)

# ToonSquid layer type ids
LT_PIXEL = 0
LT_TEXT = 2
LT_VECTOR = 3
LT_SYMBOL = 4
LT_GROUP = 5
LT_CAMERA = 6
LT_SHAPE = 7
LT_SCROLL = 8
LT_AUDIO = 9
LT_ANIMATION = 10
LT_BACKGROUND = 11
LT_EFFECT = 122

LAYER_COLORS = [
    "#4FFF4F", "#4F80FF", "#FF4F4F", "#FFD24F", "#C14FFF",
    "#4FFFD2", "#FF8C4F", "#8C4FFF", "#4FFFA1", "#FF4FD2",
]


# --------------------------------------------------------------------------- #
# Track / transform helpers
# --------------------------------------------------------------------------- #
def _unwrap(value: Any) -> float | None:
    """Keyframe values are sometimes a bare number and sometimes ``{"vl": n}``."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict) and "vl" in value:
        return _unwrap(value["vl"])
    return None


def _active_key(keys: list[dict[str, Any]], frame: int) -> dict[str, Any] | None:
    chosen = None
    for key in keys:
        if key.get("f", 0) <= frame:
            chosen = key
        else:
            break
    return chosen or (keys[0] if keys else None)


def eval_track(track: Any, frame: int, default: float = 0.0) -> float:
    """Evaluate a ToonSquid keyframe track (or a constant) at ``frame``."""
    if track is None:
        return default
    if isinstance(track, (int, float)):
        return float(track)
    if not isinstance(track, dict):
        return default
    key = _active_key(track.get("k") or [], frame)
    if key is None:
        return default
    value = _unwrap(key.get("vl"))
    return default if value is None else value


@dataclass
class Matrix:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def to_xml(self) -> str:
        parts = []
        if self.a != 1.0:
            parts.append(f'a="{self.a:.6g}"')
        if self.b != 0.0:
            parts.append(f'b="{self.b:.6g}"')
        if self.c != 0.0:
            parts.append(f'c="{self.c:.6g}"')
        if self.d != 1.0:
            parts.append(f'd="{self.d:.6g}"')
        if self.tx != 0.0:
            parts.append(f'tx="{self.tx:.6g}"')
        if self.ty != 0.0:
            parts.append(f'ty="{self.ty:.6g}"')
        return "<Matrix " + " ".join(parts) + "/>" if parts else "<Matrix/>"

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.c * y + self.tx, self.b * x + self.d * y + self.ty)

    def multiply(self, other: "Matrix") -> "Matrix":
        """Return ``self * other`` (apply ``other`` first, then ``self``)."""
        return Matrix(
            a=self.a * other.a + self.c * other.b,
            b=self.b * other.a + self.d * other.b,
            c=self.a * other.c + self.c * other.d,
            d=self.b * other.c + self.d * other.d,
            tx=self.a * other.tx + self.c * other.ty + self.tx,
            ty=self.b * other.tx + self.d * other.ty + self.ty,
        )


def transform_matrix(layer_json: dict[str, Any], frame: int) -> tuple[Matrix, tuple[float, float]]:
    """Return the layer's affine transform and pivot for ``frame``.

    ToonSquid stores the transform as ``T(position) * R * S * T(-pivot)`` where
    ``pivot`` is a point in the layer's own coordinate space.  Two schemas exist:

    * 1.x (``pX``/``pY``/``sX``/``sY``/``r`` keyframe tracks, pivot in ``pi``)
    * 2.x (static ``p``/``pv``/``s``, pivot in ``pv``)
    """
    tf = layer_json.get("tf") or {}
    if not tf:
        return Matrix(), (0.0, 0.0)

    if "pX" in tf or "pY" in tf:
        pivot = tf.get("pi") or [0.0, 0.0]
        px = eval_track(tf.get("pX"), frame, 0.0)
        py = eval_track(tf.get("pY"), frame, 0.0)
        sx = eval_track(tf.get("sX"), frame, 1.0)
        sy = eval_track(tf.get("sY"), frame, 1.0)
        rot = math.radians(eval_track(tf.get("r"), frame, 0.0))
    else:
        pivot = tf.get("pv") or tf.get("p") or [0.0, 0.0]
        pos = tf.get("p") or [0.0, 0.0]
        px, py = float(pos[0]), float(pos[1])
        scale = tf.get("s") or [1.0, 1.0]
        sx = float(scale[0])
        sy = float(scale[1]) if len(scale) > 1 else sx
        rot = math.radians(eval_track(tf.get("r"), frame, 0.0))

    pix, piy = float(pivot[0]), float(pivot[1])
    cos, sin = math.cos(rot), math.sin(rot)
    a = sx * cos
    b = sx * sin
    c = -sy * sin
    d = sy * cos
    matrix = Matrix(
        a=a, b=b, c=c, d=d,
        tx=px - (a * pix + c * piy),
        ty=py - (b * pix + d * piy),
    )
    return matrix, (pix, piy)


# --------------------------------------------------------------------------- #
# Vector path -> XFL edge string
# --------------------------------------------------------------------------- #
def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-3:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _path_subpaths(key_value: Any) -> list[dict[str, Any]]:
    """Normalise a path keyframe value into a list of subpaths.

    Two schemas exist: 1.x stores a single path dict (``{"cp": [...], "cl": ..}``)
    and 2.x stores ``{"s": [ {"p": [...], "c": bool}, ... ]}``.
    """
    if not isinstance(key_value, dict):
        return []
    if "s" in key_value and isinstance(key_value["s"], list):
        return key_value["s"]
    return [key_value]


def _edges_for_subpath_format_a(subpath: dict[str, Any], force_close: bool = False) -> str:
    points = subpath.get("cp") or []
    if not points:
        return ""
    closed = bool(subpath.get("cl")) or force_close
    out: list[str] = []

    def pos(point: dict[str, Any]) -> tuple[float, float]:
        po = point.get("po") or [0.0, 0.0]
        return po[0] * TWIPS, po[1] * TWIPS

    start = pos(points[0])
    out.append(f"!{_fmt(start[0])} {_fmt(start[1])}")
    prev = points[0]
    for cur in points[1:]:
        if prev.get("pT") in (1, 2) and prev.get("ou") is not None:
            ox, oy = prev["ou"]
            qx = (prev["po"][0] + ox) * TWIPS
            qy = (prev["po"][1] + oy) * TWIPS
            ex, ey = pos(cur)
            out.append(f"[{_fmt(qx)} {_fmt(qy)} {_fmt(ex)} {_fmt(ey)}")
        else:
            ex, ey = pos(cur)
            out.append(f"|{_fmt(ex)} {_fmt(ey)}")
        prev = cur
    if closed:
        ex, ey = pos(points[0])
        out.append(f"|{_fmt(ex)} {_fmt(ey)}")
    return "".join(out)


def _flatten_cubic(
    p0: tuple[float, float],
    c1: tuple[float, float],
    c2: tuple[float, float],
    p3: tuple[float, float],
    tolerance: float = 0.15,
    max_segments: int = 32,
) -> list[tuple[float, float]]:
    """Approximate a cubic Bezier as line-to points (excluding ``p0``).

    XFL's ``edges`` attribute has no cubic command, so cubics are flattened into
    a polyline. The segment count follows the control-polygon length, which
    keeps gentle curves cheap and tight curves smooth.
    """
    def dist(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])

    control_len = dist(p0, c1) + dist(c1, c2) + dist(c2, p3)
    count = max(1, min(max_segments, int(control_len / max(tolerance, 1e-6)) + 1))

    points = []
    for step in range(1, count + 1):
        t = step / count
        u = 1 - t
        x = u * u * u * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * p3[0]
        y = u * u * u * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * p3[1]
        points.append((x, y))
    return points


def _edges_for_subpath_format_b(subpath: dict[str, Any], force_close: bool = False) -> str:
    points = subpath.get("p") or []
    if not points:
        return ""
    closed = bool(subpath.get("c")) or force_close
    n = len(points)
    first = points[0]["p"]
    out = [f"!{_fmt(first[0] * TWIPS)} {_fmt(first[1] * TWIPS)}"]
    segments = n if closed else n - 1
    for k in range(segments):
        a = points[k]
        b = points[(k + 1) % n]
        p0 = (a["p"][0], a["p"][1])
        p3 = (b["p"][0], b["p"][1])
        o = a.get("o") or [0, 0]
        i = b.get("i") or [0, 0]
        c1 = (p0[0] + o[0], p0[1] + o[1])
        c2 = (p3[0] + i[0], p3[1] + i[1])
        for x, y in _flatten_cubic(p0, c1, c2, p3):
            out.append(f"|{_fmt(x * TWIPS)} {_fmt(y * TWIPS)}")
    return "".join(out)


def path_to_edges(key_value: Any, force_close: bool = False) -> str:
    """Convert a ToonSquid path keyframe value into an XFL edges string.

    ``force_close`` closes every contour; XFL fills an open contour as if it were
    closed anyway, but its own shape builder expects the closing segment to be
    present, so filled paths must be explicitly closed.
    """
    subpaths = _path_subpaths(key_value)
    parts = []
    for sub in subpaths:
        if "cp" in sub:
            parts.append(_edges_for_subpath_format_a(sub, force_close))
        elif "p" in sub:
            parts.append(_edges_for_subpath_format_b(sub, force_close))
    return "".join(p for p in parts if p)


def _color_hex(color: dict[str, Any]) -> tuple[str, float]:
    r = int(round(float(color.get("r", 0)) * 255))
    g = int(round(float(color.get("g", 0)) * 255))
    b = int(round(float(color.get("b", 0)) * 255))
    a = float(color.get("a", 1.0))
    return f"#{r:02X}{g:02X}{b:02X}", a


def _solid_color_value(track: Any, frame: int) -> tuple[str, float] | None:
    """Return ``(hex, alpha)`` for a solid-colour track or constant colour dict."""
    if not track:
        return None
    if isinstance(track, dict) and "c" in track and "k" not in track:
        # constant colour (2.x stores {"c": {...}} directly on the layer)
        return _color_hex(track["c"])
    if not isinstance(track, dict):
        return None
    key = _active_key(track.get("k") or [], frame)
    if key is None:
        return None
    value = key.get("vl")
    if isinstance(value, dict) and "c" in value and value["c"]:
        return _color_hex(value["c"])
    if isinstance(value, dict) and "c" in track and track.get("c"):
        return _color_hex(track["c"])
    return None


# --------------------------------------------------------------------------- #
# XFL element builders
# --------------------------------------------------------------------------- #
def build_vector_element(
    layer_json: dict[str, Any], frame: int, parent_matrix: Matrix | None = None
) -> str:
    keys = (layer_json.get("p") or {}).get("k")
    if keys is None:
        # 2.x stores the path (subpaths) directly on the layer
        key_value = layer_json.get("p")
    else:
        key = _active_key(keys or [], frame)
        key_value = key.get("vl") if key else None
    if not isinstance(key_value, dict):
        return ""
    matrix, _pivot = transform_matrix(layer_json, frame)
    if parent_matrix is not None:
        matrix = parent_matrix.multiply(matrix)
    # ToonSquid paths are in absolute canvas coordinates; the layer transform
    # (which already folds in the pivot) places them, so no pivot subtraction.
    fill = _solid_color_value(layer_json.get("fC"), frame)
    stroke_color = _solid_color_value(layer_json.get("sC"), frame)
    width = eval_track(layer_json.get("sW"), frame, 0.0)
    if fill and fill[1] <= 0:
        fill = None
    if stroke_color and (stroke_color[1] <= 0 or width <= 0):
        stroke_color = None

    edges = path_to_edges(key_value, force_close=fill is not None)
    if not edges:
        return ""

    fills_xml = ""
    if fill:
        color, alpha = fill
        alpha_attr = f' alpha="{alpha:.4g}"' if alpha < 1 else ""
        fills_xml = (
            f'<fills><FillStyle index="1"><SolidColor color="{color}"{alpha_attr}/>'
            f"</FillStyle></fills>"
        )
    strokes_xml = ""
    if stroke_color and width > 0:
        color, alpha = stroke_color
        alpha_attr = f' alpha="{alpha:.4g}"' if alpha < 1 else ""
        strokes_xml = (
            f'<strokes><StrokeStyle index="1">'
            f'<SolidStroke scaleMode="normal" weight="{_fmt(width)}">'
            f'<fill><SolidColor color="{color}"{alpha_attr}/></fill>'
            f"</SolidStroke></StrokeStyle></strokes>"
        )

    fill_attr = ' fillStyle1="1"' if fill else ""
    stroke_attr = ' strokeStyle="1"' if (stroke_color and width > 0) else ""
    return (
        "<DOMShape>"
        f"{fills_xml}{strokes_xml}"
        f'<edges><Edge{fill_attr}{stroke_attr} edges={quoteattr(edges)}/></edges>'
        f"<matrix>{matrix.to_xml()}</matrix>"
        "</DOMShape>"
    )


def build_bitmap_element(
    layer_json: dict[str, Any],
    frame: int,
    bitmap_name: str,
    rect: tuple[int, int, int, int],
    parent_matrix: Matrix | None = None,
) -> str:
    x, y, _w, _h = rect
    matrix, _ = transform_matrix(layer_json, frame)
    if parent_matrix is not None:
        matrix = parent_matrix.multiply(matrix)
    # The bitmap covers the absolute canvas rect (x, y, w, h); the layer transform
    # is applied around the rect's centre, so push the rect's top-left through it.
    tx, ty = matrix.apply(x, y)
    placed = Matrix(a=matrix.a, b=matrix.b, c=matrix.c, d=matrix.d, tx=tx, ty=ty)
    return (
        f'<DOMBitmapInstance libraryItemName={quoteattr(bitmap_name)}>'
        f"<matrix>{placed.to_xml()}</matrix>"
        f"<transformationPoint><Point/></transformationPoint>"
        "</DOMBitmapInstance>"
    )


def build_text_element(
    layer_json: dict[str, Any], frame: int, parent_matrix: Matrix | None = None
) -> str:
    text = layer_json.get("t") or ""
    if not text:
        return ""
    matrix, _ = transform_matrix(layer_json, frame)
    if parent_matrix is not None:
        matrix = parent_matrix.multiply(matrix)
    color, _ = _color_hex(layer_json.get("co") or {"r": 0, "g": 0, "b": 0, "a": 1})
    # fS is a font size in points (not twips); lBW is the line-box width in px.
    size = float(layer_json.get("fS") or 12)
    face, attrs = _font_face(str(layer_json.get("fN") or ""))
    width = float(layer_json.get("lBW") or 0.0)
    if width <= 0:
        width = max(len(text) * size * 0.6, size)
    height = size * 1.4
    align = {0: "left", 1: "center", 2: "right"}.get(int(layer_json.get("al") or 0), "left")
    alignment = f' alignment="{align}"' if align != "left" else ""
    # Real XFL stores the string in a <characters> child element (not an
    # attribute) and identifies the font by `face`.
    return (
        f'<DOMStaticText width="{_fmt(width)}" height="{_fmt(height)}">'
        f"<matrix>{matrix.to_xml()}</matrix>"
        "<textRuns><DOMTextRun>"
        f"<characters>{escape(text)}</characters>"
        f'<textAttrs><DOMTextAttrs{alignment} aliasText="false" autoKern="false" '
        f'size="{_fmt(size)}" bitmapSize="{_fmt(size * TWIPS)}" face={quoteattr(face)} '
        f'fillColor="{color}"{attrs}/></textAttrs>'
        "</DOMTextRun></textRuns>"
        f'<bounds>{_rect_xml(width, height)}</bounds>'
        "</DOMStaticText>"
    )


def _font_face(font: str) -> tuple[str, str]:
    """Split a ToonSquid ``fN`` name into an XFL ``face`` and extra attrs.

    ToonSquid often stores generic names such as ``System Font SemiBold``; those
    map to the ``_sans`` device font, with the weight carried in ``bold``.
    """
    if not font:
        return "_sans", ""
    low = font.lower()
    bold = any(w in low for w in ("bold", "black", "heavy", "semibold", "medium"))
    italic = "italic" in low or "oblique" in low
    if "system font" in low or low in {"system", "system font"}:
        face = "_sans"
    else:
        face = font
    extra = ' bold="true"' if bold else ""
    if italic:
        extra += ' italic="true"'
    return face, extra


def _rect_xml(width: float, height: float) -> str:
    right = _fmt(width * TWIPS)
    bottom = _fmt(height * TWIPS)
    return (
        f'<Rectangle left="0" top="0" right="{right}" bottom="{bottom}"/>'
    )


# --------------------------------------------------------------------------- #
# Converter
# --------------------------------------------------------------------------- #
@dataclass
class Converter:
    project: Project
    reverse_layers: bool = True
    texture_map: dict[str, tuple[str, tuple[int, int, int, int]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    current_clip: Clip | None = None
    _symbol_stack: list[str] = field(default_factory=list)
    _item_counter: int = 0
    _bitmap_names: dict[str, str] = field(default_factory=dict)
    _item_ids: dict[str, str] = field(default_factory=dict)

    # -- helpers ---------------------------------------------------------- #
    def item_id(self, key: str) -> str:
        """Stable library itemID for a given key.

        The DOMBitmapItem in DOMDocument.xml and the media it points at must
        carry a consistent itemID or Animate cannot resolve the library item.
        """
        if key not in self._item_ids:
            self._item_counter += 1
            self._item_ids[key] = f"{uuid.uuid4().hex[:8]}-{self._item_counter:08x}"
        return self._item_ids[key]

    def bitmap_name(self, texture_id: str) -> str:
        if texture_id not in self._bitmap_names:
            self._bitmap_names[texture_id] = f"ts_{texture_id[:8]}"
        return self._bitmap_names[texture_id]

    def layer_color(self, index: int) -> str:
        return LAYER_COLORS[index % len(LAYER_COLORS)]

    # -- drawing layer -> element ---------------------------------------- #
    def drawing_layer_element(
        self, layer_id: str, frame: int, parent_matrix: Matrix | None = None
    ) -> str:
        assert self.current_clip is not None
        ltype = self.current_clip.layer_type(layer_id)
        layer_json = self.current_clip.layers.get(layer_id, {})
        if ltype in (LT_VECTOR, LT_SHAPE):
            return build_vector_element(layer_json, frame, parent_matrix)
        if ltype == LT_PIXEL:
            entry = self.texture_map.get(layer_id)
            if entry is None or entry[0] not in self.project.textures:
                return ""
            texture_id, rect = entry
            return build_bitmap_element(
                layer_json, frame, self.bitmap_name(texture_id), rect, parent_matrix
            )
        if ltype == LT_TEXT:
            return build_text_element(layer_json, frame, parent_matrix)
        if ltype == LT_SYMBOL:
            # The user wants flat layers, not movie clips: inline the referenced
            # clip's content directly instead of emitting a DOMSymbolInstance.
            return self.flatten_symbol_layer(layer_id, frame, parent_matrix)
        if ltype == LT_GROUP:
            return "".join(
                p
                for p in (
                    self.drawing_layer_element(c, frame, parent_matrix)
                    for c in layer_json.get("ch", [])
                )
                if p
            )
        return ""

    def symbol_layer_matrix(self, layer_id: str, frame: int) -> Matrix:
        """The placement transform of a symbol layer at ``frame``."""
        assert self.current_clip is not None
        layer_json = self.current_clip.layers.get(layer_id, {})
        matrix, _ = transform_matrix(layer_json, frame)
        return matrix

    def symbol_target(self, layer_id: str) -> str | None:
        assert self.current_clip is not None
        ref = self.current_clip.layers.get(layer_id, {}).get("cl") or {}
        clip_id = ref.get("id") if isinstance(ref, dict) else None
        if not clip_id or clip_id not in self.project.clips:
            return None
        return clip_id

    def flatten_symbol_layer(
        self, layer_id: str, frame: int, parent_matrix: Matrix | None = None
    ) -> str:
        clip_id = self.symbol_target(layer_id)
        if clip_id is None:
            return ""
        if clip_id in self._symbol_stack:
            self.warnings.append(f"recursive symbol {clip_id} skipped")
            return ""
        placement = self.symbol_layer_matrix(layer_id, frame)
        if parent_matrix is not None:
            placement = parent_matrix.multiply(placement)
        child = self.project.clips[clip_id]
        self._symbol_stack.append(clip_id)
        try:
            return "".join(self.clip_elements(child, frame, placement))
        finally:
            self._symbol_stack.pop()

    # -- animation layer -> DOMLayer ------------------------------------- #
    def _layer_spans(
        self, clip: Clip, layer_id: str, parent_matrix: Matrix | None = None
    ) -> list[tuple[int, int, str]]:
        """Return ``(start_index, duration, elements)`` spans for a layer."""
        prev = self.current_clip
        self.current_clip = clip
        try:
            layer_json = clip.layers.get(layer_id, {})
            ltype = clip.layer_type(layer_id)
            total = max(clip.total_frames, 1)

            if ltype in (LT_SYMBOL, LT_VECTOR, LT_PIXEL, LT_TEXT, LT_SHAPE):
                return [(0, total, self.drawing_layer_element(layer_id, 0, parent_matrix))]

            spans: list[tuple[int, int, str]] = []
            for drawing_id in layer_json.get("dr", []):
                drawing = clip.drawings.get(drawing_id)
                if not drawing:
                    continue
                start = max(int(drawing.get("s", 1)) - 1, 0)
                end = max(int(drawing.get("e", start + 1)), start + 1)
                parts = [
                    self.drawing_layer_element(child, start, parent_matrix)
                    for child in drawing.get("ly", [])
                ]
                spans.append((start, end - start, "".join(p for p in parts if p)))
            if not spans:
                spans = [(0, total, "")]
            spans.sort(key=lambda s: s[0])
            return spans
        finally:
            self.current_clip = prev

    def _dom_layer(
        self,
        clip: Clip,
        layer_id: str,
        color_index: int,
        name: str | None = None,
        parent_matrix: Matrix | None = None,
    ) -> str:
        ltype = clip.layer_type(layer_id)
        if ltype in (LT_CAMERA, LT_AUDIO, LT_EFFECT, LT_SCROLL, LT_BACKGROUND):
            return ""
        layer_json = clip.layers.get(layer_id, {})
        name = name or layer_json.get("Na") or "Layer"
        spans = self._layer_spans(clip, layer_id, parent_matrix)
        if not spans or not any(elements for _start, _duration, elements in spans):
            # Nothing drawable on this layer; drop it so the timeline is not
            # littered with the empty helper layers ToonSquid leaves behind.
            return ""
        current = ' current="true" isSelected="true"' if color_index == 0 else ""
        frames = []
        cursor = 0
        for start, duration, elements in spans:
            start = max(start, cursor)
            if start > cursor:
                # XFL layers must be contiguous from frame 0; hold the previous
                # drawing across the gap.
                frames.append(f'<DOMFrame index="{cursor}" duration="{start - cursor}" keyMode="9728"><elements/></DOMFrame>')
            body = f"<elements>{elements}</elements>" if elements else "<elements/>"
            frames.append(
                f'<DOMFrame index="{start}" duration="{max(duration, 1)}" keyMode="9728">{body}</DOMFrame>'
            )
            cursor = start + max(duration, 1)
        return (
            f'<DOMLayer name={quoteattr(name)} color="{self.layer_color(color_index)}"{current}>'
            f"<frames>{''.join(frames)}</frames>"
            "</DOMLayer>"
        )

    def _background_layer(self, name: str, frames: int) -> str:
        """A plain background layer spanning the timeline."""
        return (
            f'<DOMLayer name={quoteattr(name)} color="#C0C0C0">'
            f'<frames><DOMFrame index="0" duration="{max(frames, 1)}" keyMode="9728">'
            "<elements/></DOMFrame></frames></DOMLayer>"
        )

    def clip_elements(
        self, clip: Clip, frame: int, parent_matrix: Matrix | None = None
    ) -> list[str]:
        """Every drawable element of ``clip`` (used when inlining a symbol).

        Animation layers are expanded through ``_layer_spans`` so their drawings
        (which may hold pixel/vector/symbol children) survive the flattening.
        """
        prev = self.current_clip
        self.current_clip = clip
        try:
            out: list[str] = []
            for lid in reversed(clip.layer_order) if self.reverse_layers else clip.layer_order:
                ltype = clip.layer_type(lid)
                if ltype in (LT_CAMERA, LT_AUDIO, LT_EFFECT, LT_SCROLL, LT_BACKGROUND):
                    continue
                if ltype == LT_GROUP:
                    for child in clip.layers.get(lid, {}).get("ch", []):
                        out.extend(self._elements_for_layer(clip, child, parent_matrix))
                    continue
                out.extend(self._elements_for_layer(clip, lid, parent_matrix))
            return out
        finally:
            self.current_clip = prev

    def _elements_for_layer(
        self, clip: Clip, layer_id: str, parent_matrix: Matrix | None = None
    ) -> list[str]:
        spans = self._layer_spans(clip, layer_id, parent_matrix)
        return [el for _start, _duration, el in spans if el]

    def clip_layer_specs(
        self, clip: Clip, prefix: str = ""
    ) -> list[tuple[str, str]]:
        """``(name, layer_id)`` pairs for the clip's drawable layers."""
        return [(name, lid) for name, _owner, lid, _ in self._layer_specs(clip, prefix, None)]

    # -- clip -> DOMTimeline --------------------------------------------- #
    def clip_timeline(self, clip: Clip, name: str, scene: bool = False) -> str:
        prev = self.current_clip
        self.current_clip = clip
        try:
            layers_xml = self._build_layers(clip, scene=scene)
            if not layers_xml:
                layers_xml.append(
                    '<DOMLayer name="Layer 1" color="#4FFF4F" current="true" isSelected="true">'
                    '<frames><DOMFrame index="0" duration="1" keyMode="9728"><elements/></DOMFrame></frames>'
                    "</DOMLayer>"
                )
            return (
                f"<DOMTimeline name={quoteattr(name)}>"
                f"<layers>{''.join(layers_xml)}</layers>"
                "</DOMTimeline>"
            )
        finally:
            self.current_clip = prev

    def _build_layers(self, clip: Clip, scene: bool = False) -> list[str]:
        """Flat ``DOMLayer`` list for ``clip``.

        Nested symbol/movieclip layers are expanded into their own layers so the
        result is a plain layer stack rather than a tree of symbols.
        """
        layers_xml: list[str] = []
        color_index = 0
        for name, owner, lid, parent in self._layer_specs(clip, "", None):
            xml = self._dom_layer(owner, lid, color_index, name, parent)
            if xml:
                layers_xml.append(xml)
                color_index += 1
        if scene:
            layers_xml.append(self._background_layer("Background", max(clip.total_frames, 1)))
        return layers_xml

    def _layer_specs(
        self,
        clip: Clip,
        prefix: str,
        parent: Matrix | None,
    ) -> list[tuple[str, Clip, str, Matrix | None]]:
        """``(name, owner_clip, layer_id, parent_matrix)`` for drawable layers."""
        specs: list[tuple[str, Clip, str, Matrix | None]] = []
        prev = self.current_clip
        self.current_clip = clip
        try:
            order = list(reversed(clip.layer_order)) if self.reverse_layers else list(clip.layer_order)
            for lid in order:
                ltype = clip.layer_type(lid)
                if ltype in (LT_CAMERA, LT_AUDIO, LT_EFFECT, LT_SCROLL, LT_BACKGROUND):
                    continue
                name = clip.layers.get(lid, {}).get("Na") or "Layer"
                full = f"{prefix}{name}"
                if ltype == LT_GROUP:
                    for child in clip.layers.get(lid, {}).get("ch", []):
                        specs.append((full, clip, child, parent))
                elif ltype == LT_SYMBOL:
                    clip_id = self.symbol_target(lid)
                    if clip_id is None or clip_id in self._symbol_stack:
                        if clip_id in self._symbol_stack:
                            self.warnings.append(f"recursive symbol {clip_id} skipped")
                        continue
                    placement = self.symbol_layer_matrix(lid, 0)
                    if parent is not None:
                        placement = parent.multiply(placement)
                    child_clip = self.project.clips[clip_id]
                    self._symbol_stack.append(clip_id)
                    try:
                        specs.extend(
                            self._layer_specs(child_clip, prefix=f"{full} / ", parent=placement)
                        )
                    finally:
                        self._symbol_stack.pop()
                else:
                    specs.append((full, clip, lid, parent))
            return specs
        finally:
            self.current_clip = prev


def build_texture_map(project: Project) -> dict[str, tuple[str, tuple[int, int, int, int]]]:
    """Map drawing-layer id -> (texture id, destination rect (x, y, w, h))."""
    with zipfile.ZipFile(project.path) as zf:
        handle = _load_sqlite(zf.read(f"{project.id}/data"))
    try:
        mapping = {}
        for owner, _otype, texture_id, _w, _h, x, y, w, h in handle.conn.execute(
            "SELECT ownerId, ownerType, textureId, width, height, x, y, w, h "
            "FROM res_shared_textures"
        ):
            mapping[owner] = (texture_id, (int(x), int(y), int(w), int(h)))
        return mapping
    finally:
        handle.close()


def convert(project: Project, reverse_layers: bool = True) -> tuple[bytes, list[str]]:
    """Convert a parsed project into ``.fla`` bytes.  Returns ``(bytes, warnings)``."""
    converter = Converter(project=project, reverse_layers=reverse_layers)
    converter.texture_map = build_texture_map(project)

    scenes = project.scenes or list(project.clips.values())
    timelines = [converter.clip_timeline(clip, clip.name, scene=True) for clip in scenes]

    width = project.width or (scenes[0].width if scenes else 550)
    height = project.height or (scenes[0].height if scenes else 400)
    fps = project.fps or (scenes[0].fps if scenes else 24)

    used_textures = sorted({entry[0] for entry in converter.texture_map.values()})
    media_xml = []
    for texture_id in used_textures:
        if texture_id not in project.textures:
            continue
        name = converter.bitmap_name(texture_id)
        w, h, _ = project.textures[texture_id]
        media_xml.append(
            f'<DOMBitmapItem name={quoteattr(name)} itemID="{converter.item_id("bmp:" + texture_id)}" '
            'allowSmoothing="true" useImportedJPEGData="false" compressionType="lossless" '
            'originalCompressionType="lossless" quality="50" '
            f'href={quoteattr(name + ".png")} '
            f'sourceExternalFilepath={quoteattr(".\\LIBRARY\\" + name + ".png")} '
            f'bitmapDataHRef={quoteattr("M " + name + " 0.dat")} '
            f'frameRight="{w * TWIPS}" frameBottom="{h * TWIPS}"/>'
        )

    symbols_xml = []  # symbols are flattened into layers, not emitted as clips

    dom = (
        '<DOMDocument xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xmlns="{XFL_NS}" width="{width}" height="{height}" frameRate="{fps}" '
        'currentTimeline="1" xflVersion="2.2" creatorInfo="tsproj2fla" platform="Windows" '
        'versionInfo="Saved by Adobe Flash Windows 12.0 build 537" majorVersion="12" '
        'buildNumber="537" playOptionsPlayLoop="false" playOptionsPlayPages="false" '
        'playOptionsPlayFrameActions="false">'
        + (f"<media>{''.join(media_xml)}</media>" if media_xml else "")
        + (f"<symbols>{''.join(symbols_xml)}</symbols>" if symbols_xml else "")
        + f"<timelines>{''.join(timelines)}</timelines>"
        + "<PrinterSettings/><publishHistory/>"
        + "</DOMDocument>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # The FLA/XFL container, like EPUB/ODF, requires "mimetype" to be the
        # first entry and stored uncompressed so the format is detectable from
        # the raw bytes. Animate validates this and opens a blank document if
        # the entry is deflated.
        zf.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/vnd.adobe.fla",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr(f"{project.name}.xfl", "PROXY-CS5")
        zf.writestr("DOMDocument.xml", dom)
        zf.writestr("PublishSettings.xml", _publish_settings(width, height, fps))
        zf.writestr("MobileSettings.xml", "")
        zf.writestr("META-INF/metadata.xml", _metadata_xml())

        for texture_id in used_textures:
            if texture_id not in project.textures:
                continue
            w, h, raw = project.textures[texture_id]
            name = converter.bitmap_name(texture_id)
            # LIBRARY/*.png is the preview bitmap Animate shows; bin/M *.dat is
            # the actual bitmap data, stored as an uncompressed pixel plane.
            img = Image.frombytes("RGBA", (w, h), raw)
            png = io.BytesIO()
            img.save(png, format="PNG")
            zf.writestr(f"LIBRARY/{name}.png", png.getvalue())
            zf.writestr(
                zipfile.ZipInfo(f"bin/M {name} 0.dat"),
                _encode_dat_bitmap(w, h, raw),
                compress_type=zipfile.ZIP_STORED,
            )

    return buf.getvalue(), converter.warnings


def _encode_dat_bitmap(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA pixels as an Animate ``.dat`` lossless bitmap.

    Layout (little-endian), matching JPEXS' LosslessImageBinDataReader:
      magic 0x03 0x05 (32-bit), rowSize = width*4, width, height, 16 reserved
      bytes, hasAlpha, variant (0 = verbatim pixel plane), then pixels.
    Pixels are stored as A,R,G,B with colour premultiplied by alpha, using the
    reader's ``a-1`` pairing (so stored alpha is ``a+1`` for partial alpha).
    """
    header = bytearray(26)
    header[0] = 0x03
    header[1] = 0x05
    header[2:4] = (width * 4).to_bytes(2, "little")
    header[4:6] = width.to_bytes(2, "little")
    header[6:8] = height.to_bytes(2, "little")
    header[12:16] = (width * 20).to_bytes(4, "little")  # frameRight, twips
    header[20:24] = (height * 20).to_bytes(4, "little")  # frameBottom, twips
    header[24] = 1  # hasAlpha
    header[25] = 0  # variant 0: verbatim A,R,G,B pixel plane

    out = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        if a == 0:
            sr = sg = sb = sa = 0
        elif a == 255:
            sr, sg, sb, sa = r, g, b, 255
        else:
            sa = a + 1
            sr = (r * a) // 256
            sg = (g * a) // 256
            sb = (b * a) // 256
        out[i], out[i + 1], out[i + 2], out[i + 3] = sa, sr, sg, sb
    return bytes(header) + bytes(out)


def _decode_dat_bitmap(data: bytes) -> tuple[int, int, bytes]:
    """Decode a variant-0 ``.dat`` bitmap back to RGBA (for tests)."""
    assert data[0] == 0x03 and data[1] == 0x05
    width = int.from_bytes(data[4:6], "little")
    height = int.from_bytes(data[6:8], "little")
    pixels = data[26:26 + width * height * 4]
    out = bytearray(len(pixels))
    for i in range(0, len(pixels), 4):
        sa, sr, sg, sb = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
        if sa == 0:
            out[i + 3] = 0
        elif sa == 255:
            out[i], out[i + 1], out[i + 2], out[i + 3] = sr, sg, sb, 255
        else:
            a = sa - 1
            out[i] = min(255, (sr * 256) // a) if a else 0
            out[i + 1] = min(255, (sg * 256) // a) if a else 0
            out[i + 2] = min(255, (sb * 256) // a) if a else 0
            out[i + 3] = a
    return width, height, bytes(out)


def _publish_settings(width: int, height: int, fps: int) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<flash_profiles><flash_profile version="1.0" name="Default" current="true">'
        '<PublishFormatProperties enabled="true"><defaultNames>1</defaultNames>'
        "<flash>1</flash><html>1</html></PublishFormatProperties>"
        '<PublishFlashProperties enabled="true"><Version>17</Version>'
        "<ActionScriptVersion>3</ActionScriptVersion><CompressMovie>1</CompressMovie>"
        "</PublishFlashProperties>"
        f'<PublishHtmlProperties enabled="true"><Width>{width}</Width>'
        f"<Height>{height}</Height><Loop>1</Loop></PublishHtmlProperties>"
        "</flash_profile></flash_profiles>"
    )


def _metadata_xml() -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:format>application/vnd.adobe.fla</dc:format></rdf:Description>"
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f"<xmp:MetadataDate>{stamp}</xmp:MetadataDate>"
        f"<xmp:ModifyDate>{stamp}</xmp:ModifyDate></rdf:Description>"
        '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )


# ===== tsproj2fla.__main__ =====

"""Command line entry point: ``python -m tsproj2fla input.tsproj [-o out.fla]``."""


import argparse
import sys
from pathlib import Path






def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tsproj2fla",
        description="Convert a ToonSquid .tsproj project into an Adobe Animate .fla file.",
    )
    parser.add_argument("input", type=Path, nargs="?", help="path to the .tsproj file")
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
    parser.add_argument(
        "--version", action="version", version=f"tsproj2fla {__version__}",
    )
    args = parser.parse_args(argv)

    if args.input is None:
        parser.error("the following arguments are required: input")

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

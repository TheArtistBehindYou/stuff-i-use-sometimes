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
from __future__ import annotations

import io
import math
import struct
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .tsproj import Clip, Project, _load_sqlite

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
    _audio_names: dict[str, str] = field(default_factory=dict)
    _audio_formats: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    _audio_dats: dict[str, bytes] = field(default_factory=dict)

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

    def audio_name(self, audio_id: str) -> str:
        """Library name (with extension) for ``audio_id``, e.g. ``audio_ab12cd34.wav``.

        Important: Animate's sound lookup does not resolve names with a leading
        directory the way images do, so the name must be *identical* to the
        ``soundName`` used on the frame.
        """
        if audio_id not in self._audio_names:
            entry = self.project.audio.get(audio_id) or {}
            ext = entry.get("ext") or "wav"
            self._audio_names[audio_id] = f"audio_{audio_id[:8]}.{ext}"
            data = entry.get("data")
            if data:
                try:
                    dat, label, export_format, samples = _encode_audio_dat(data)
                except Exception as exc:  # pragma: no cover - defensive
                    self.warnings.append(f"audio {audio_id}: {exc}")
                    dat, label, export_format, samples = data, "44kHz 16bit Stereo", 1, 0
                self._audio_formats[audio_id] = (label, export_format, samples)
                self._audio_dats[audio_id] = dat
        return self._audio_names[audio_id]

    def layer_color(self, index: int) -> str:
        return LAYER_COLORS[index % len(LAYER_COLORS)]

    def _audio_layer_xml(
        self, clip: Clip, layer_id: str, color_index: int, name: str
    ) -> str:
        """A ``DOMLayer`` whose frame fires a library sound via ``soundName``.

        XFL has no sound *element*: a sound is played by naming the library item
        on the keyframe (``soundName``).  ToonSquid stores the audio on a
        type-9 layer that points at a drawing whose ``s``/``e`` give the frame
        span, and whose ``aC`` carries the audio-clip id.
        """
        layer_json = clip.layers.get(layer_id, {})
        audio_id = (layer_json.get("aC") or {}).get("id")
        if not audio_id or audio_id not in self.project.audio:
            return ""
        drawing = clip.drawings.get(layer_json.get("DI")) or {}
        start = max(int(drawing.get("s", 1)) - 1, 0)
        end = max(int(drawing.get("e", start + 1)), start + 1)
        duration = max(end - start, 1)
        sound = self.audio_name(audio_id)
        frames = []
        if start > 0:
            frames.append(
                f'<DOMFrame index="0" duration="{start}" keyMode="9728"><elements/></DOMFrame>'
            )
        frames.append(
            f'<DOMFrame index="{start}" duration="{duration}" keyMode="9728" '
            f'soundName={quoteattr(sound)} soundSync="start"><elements/></DOMFrame>'
        )
        return (
            f'<DOMLayer name={quoteattr(name)} color="{self.layer_color(color_index)}">'
            f"<frames>{''.join(frames)}</frames></DOMLayer>"
        )

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
        audio_xml = self._build_audio_layers(clip, color_index)
        layers_xml.extend(audio_xml)
        if scene:
            layers_xml.append(self._background_layer("Background", max(clip.total_frames, 1)))
        return layers_xml

    def _build_audio_layers(self, clip: Clip, color_index: int = 0) -> list[str]:
        """Sound layers for ``clip``'s type-9 (audio) layers, if any.

        Iterates ``clip.layers`` rather than ``clip.layer_order``: ToonSquid
        keeps audio layers out of the ordered draw list, but they are present in
        the layer table.
        """
        out: list[str] = []
        for lid in clip.layers:
            if clip.layer_type(lid) != LT_AUDIO:
                continue
            name = clip.layers.get(lid, {}).get("Na") or "Sound"
            xml = self._audio_layer_xml(clip, lid, color_index, name)
            if xml:
                out.append(xml)
                color_index += 1
        return out


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

    # Any audio referenced by a type-9 layer anywhere in the project.
    used_audio: list[str] = []
    seen_audio: set[str] = set()
    for clip in project.clips.values():
        for lid in clip.layers:
            if clip.layer_type(lid) != LT_AUDIO:
                continue
            aid = (clip.layers.get(lid, {}).get("aC") or {}).get("id")
            if aid and aid in project.audio and aid not in seen_audio:
                seen_audio.add(aid)
                used_audio.append(aid)
    for audio_id in used_audio:
        name = converter.audio_name(audio_id)
        label, export_format, samples = converter._audio_formats.get(
            audio_id, ("44kHz 16bit Stereo", 1, 0)
        )
        entry = project.audio.get(audio_id) or {}
        ext = entry.get("ext") or "wav"
        media_xml.append(
            f'<DOMSoundItem name={quoteattr(name)} itemID="{converter.item_id("snd:" + audio_id)}" '
            f'sourceExternalFilepath={quoteattr(".\\LIBRARY\\" + name)} '
            f'externalFileSize="{len(entry.get("data") or b"")}" '
            f'href={quoteattr(name)} soundDataHRef={quoteattr("M " + name + " 0.dat")} '
            f'format={quoteattr(label)} exportFormat="{export_format}" '
            f'sampleCount="{samples}"/>'
        )

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
            # the actual bitmap data (deflate-compressed A,B,G,R plane).
            zf.writestr(f"LIBRARY/{name}.png", _encode_png(w, h, raw))
            zf.writestr(
                zipfile.ZipInfo(f"bin/M {name} 0.dat"),
                _encode_dat_bitmap(w, h, raw),
                compress_type=zipfile.ZIP_STORED,
            )

        for audio_id in used_audio:
            entry = project.audio.get(audio_id) or {}
            data = entry.get("data")
            if not data:
                continue
            name = converter.audio_name(audio_id)
            # LIBRARY/<name> is the source file Animate imports; bin/M *.dat is
            # the decoded PCM (WAV) or the MP3 payload it plays.
            zf.writestr(f"LIBRARY/{name}", data)
            zf.writestr(
                zipfile.ZipInfo(f"bin/M {name} 0.dat"),
                converter._audio_dats.get(audio_id, data),
                compress_type=zipfile.ZIP_STORED,
            )

    return buf.getvalue(), converter.warnings



def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode an RGBA buffer as a PNG (pure Python, no Pillow).

    Only used for the ``LIBRARY/*.png`` preview Animate shows; the real bitmap
    data lives in ``bin/M *.dat``.
    """
    stride = width * 4
    # Each scanline is prefixed with a filter byte (0 = None).
    raw = bytearray(height * (stride + 1))
    for y in range(height):
        src = y * stride
        dst = y * (stride + 1)
        raw[dst + 1:dst + 1 + stride] = rgba[src:src + stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


def _encode_dat_bitmap(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA pixels as an Animate ``bin/M *.dat`` bitmap.

    Layout (little-endian), matching JPEXS' ``LosslessImageBinData`` reader and
    writer (major 3 = alpha, variant 1 = deflate)::

        u8  major=3, u8 minor=5
        u16 rowSize (bytes per row), u16 width, u16 height
        u32 frameLeft=0, u32 frameRight=width*20, u32 frameTop=0, u32 frameBottom=height*20
        u8  flags (bit 0 = hasAlpha)
        u8  variant = 1 (compressed)
        repeat { u16 chunkLen; chunkLen bytes } u16 0

    Pixels are stored A,B,G,R, colour premultiplied by alpha, with partial
    alpha biased by one (the reader subtracts one), per the reference encoder.
    Animate refuses the file if this header is malformed, so it must match.
    """
    header = bytearray()
    header += bytes((0x03, 0x05))
    header += struct.pack("<HHH", width * 4, width, height)
    header += struct.pack("<IIII", 0, width * TWIPS, 0, height * TWIPS)
    header += bytes((0x01, 0x01))  # flags: hasAlpha; variant: compressed

    raw = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        if a != 255:
            r = (r * a) // 256
            g = (g * a) // 256
            b = (b * a) // 256
        if a != 0 and a != 255:
            a += 1
        raw[i], raw[i + 1], raw[i + 2], raw[i + 3] = a, b, g, r

    data = zlib.compress(bytes(raw), 1)
    out = bytearray(header)
    for pos in range(0, len(data), 2048):  # large chunks crash Flash; 2048 = CS5
        chunk = data[pos:pos + 2048]
        out += struct.pack("<H", len(chunk)) + chunk
    out += struct.pack("<H", 0)
    return bytes(out)


def _decode_dat_bitmap(data: bytes) -> tuple[int, int, bytes]:
    """Decode a ``bin/M *.dat`` bitmap (variant 0 or 1) back to RGBA."""
    assert data[0] == 0x03 and data[1] == 0x05
    width = int.from_bytes(data[4:6], "little")
    height = int.from_bytes(data[6:8], "little")
    flags = data[24]
    has_alpha = bool(flags & 1)
    variant = data[25]
    body = data[26:]
    if variant == 1:
        buf = bytearray()
        pos = 0
        while pos + 2 <= len(body):
            length = int.from_bytes(body[pos:pos + 2], "little")
            pos += 2
            if length == 0:
                break
            buf += body[pos:pos + length]
            pos += length
        pixels = zlib.decompress(bytes(buf))
    else:
        pixels = body[:width * height * 4]
    out = bytearray(len(pixels))
    for i in range(0, len(pixels), 4):
        sa, sb, sg, sr = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
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
    if not has_alpha:
        for i in range(3, len(out), 4):
            out[i] = 255
    return width, height, bytes(out)


def _wav_params(wav: bytes) -> tuple[int, int, int, int]:
    """Return ``(sample_rate, channels, bits, sample_count)`` for a WAV blob."""
    channels = int.from_bytes(wav[22:24], "little")
    sample_rate = int.from_bytes(wav[24:28], "little")
    bits = int.from_bytes(wav[34:36], "little")
    # Walk RIFF chunks to find the ``data`` payload (its offset is not fixed).
    pos = 12
    data_size = 0
    while pos + 8 <= len(wav):
        cid = wav[pos:pos + 4]
        size = int.from_bytes(wav[pos + 4:pos + 8], "little")
        if cid == b"data":
            data_size = size
            break
        pos += 8 + size + (size & 1)
    bytes_per_sample = max(bits // 8, 1) * max(channels, 1)
    sample_count = data_size // bytes_per_sample if bytes_per_sample else 0
    return sample_rate, channels, bits, sample_count


def _encode_audio_dat(wav: bytes) -> tuple[bytes, str, int, int]:
    """Return ``(dat_bytes, format_label, export_format, sample_count)``.

    Animate stores a sound in two places: ``LIBRARY/<name>.<ext>`` holds the
    original file, while ``bin/M *.dat`` holds the *decoded* PCM for WAV (the
    MP3 payload verbatim for MP3).  The WAV RIFF header is stripped.
    """
    if wav[:4] == b"RIFF":  # WAV -> decoded 16-bit PCM
        rate, channels, bits, samples = _wav_params(wav)
        pos = wav.find(b"data")
        payload = wav[pos + 8:] if pos != -1 else wav[44:]
        return payload, _audio_label(rate, bits, channels), 1, samples

    if wav[:3] == b"ID3" or wav[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or wav[:2] == b"\xff\xfa":
        rate, bitrate = _mp3_params(wav)  # MP3 -> payload verbatim
        samples = int(len(wav) / max(bitrate, 1) * rate / 8) if bitrate and rate else 0
        return wav, _audio_label(rate, 16, 2), 1, samples

    return wav, "44kHz 16bit Stereo", 1, 0


def _audio_label(rate: int, bits: int, channels: int) -> str:
    khz = {44100: "44", 22050: "22", 11025: "11", 5500: "5.5"}.get(rate, str(round(rate / 1000)))
    return f"{khz}kHz {'16bit' if bits == 16 else '8bit'} {'Stereo' if channels == 2 else 'Mono'}"


def _mp3_params(data: bytes) -> tuple[int, int]:
    """Return ``(sample_rate, bitrate_bps)`` from the first MPEG audio frame."""
    pos = 0
    if data[:3] == b"ID3" and len(data) > 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        pos = 10 + size
    while pos + 4 <= len(data):
        if data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0:
            break
        pos += 1
    if pos + 4 > len(data):
        return 44100, 128000
    hdr = data[pos:pos + 4]
    version = (hdr[1] >> 3) & 0x03
    sample_rates = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
    rate = sample_rates.get(version, [44100])[(hdr[2] >> 2) & 0x03]
    bitrates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    bitrate = bitrates[(hdr[2] >> 4) & 0x0F] * 1000
    return rate, bitrate


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

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
import sqlite3
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from PIL import Image

from .tsproj import Clip, Project

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


def _edges_for_subpath_format_a(subpath: dict[str, Any]) -> str:
    points = subpath.get("cp") or []
    if not points:
        return ""
    closed = bool(subpath.get("cl"))
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


def _edges_for_subpath_format_b(subpath: dict[str, Any]) -> str:
    points = subpath.get("p") or []
    if not points:
        return ""
    closed = bool(subpath.get("c"))
    out: list[str] = []
    n = len(points)
    first = points[0]["p"]
    out.append(f"!{_fmt(first[0] * TWIPS)} {_fmt(first[1] * TWIPS)}")
    segments = n - 1 if not closed else n
    for k in range(segments):
        a = points[k]
        b = points[(k + 1) % n]
        c1 = [a["p"][0] + a.get("o", [0, 0])[0], a["p"][1] + a.get("o", [0, 0])[1]]
        c2 = [b["p"][0] + b.get("i", [0, 0])[0], b["p"][1] + b.get("i", [0, 0])[1]]
        e = b["p"]
        out.append(
            f"(;{_fmt(c1[0]*TWIPS)},{_fmt(c1[1]*TWIPS)} "
            f"{_fmt(c2[0]*TWIPS)},{_fmt(c2[1]*TWIPS)} "
            f"{_fmt(e[0]*TWIPS)},{_fmt(e[1]*TWIPS)});"
        )
    return "".join(out)


def path_to_edges(key_value: Any) -> str:
    """Convert a ToonSquid path keyframe value into an XFL edges string."""
    subpaths = _path_subpaths(key_value)
    parts = []
    for sub in subpaths:
        if "cp" in sub:
            parts.append(_edges_for_subpath_format_a(sub))
        elif "p" in sub:
            parts.append(_edges_for_subpath_format_b(sub))
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
def build_vector_element(layer_json: dict[str, Any], frame: int) -> str:
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
    # ToonSquid paths are in absolute canvas coordinates; the layer transform
    # (which already folds in the pivot) places them, so no pivot subtraction.
    edges = path_to_edges(key_value)
    if not edges:
        return ""

    fill = _solid_color_value(layer_json.get("fC"), frame)
    stroke_color = _solid_color_value(layer_json.get("sC"), frame)
    width = eval_track(layer_json.get("sW"), frame, 0.0)
    if fill and fill[1] <= 0:
        fill = None
    if stroke_color and (stroke_color[1] <= 0 or width <= 0):
        stroke_color = None

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

    fill_attr = ' fillStyle1="1"' if fill else ' fillStyle0="0" fillStyle1="0"'
    stroke_attr = ' strokeStyle="1"' if (stroke_color and width > 0) else ' strokeStyle="0"'
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
) -> str:
    x, y, _w, _h = rect
    matrix, _ = transform_matrix(layer_json, frame)
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


def build_text_element(layer_json: dict[str, Any], frame: int) -> str:
    text = layer_json.get("t") or ""
    if not text:
        return ""
    matrix, _ = transform_matrix(layer_json, frame)
    color, _ = _color_hex(layer_json.get("co") or {"r": 0, "g": 0, "b": 0, "a": 1})
    size = float(layer_json.get("fS") or 12) / TWIPS
    return (
        "<DOMStaticText>"
        f"<matrix>{matrix.to_xml()}</matrix>"
        f'<textRuns><DOMTextRun characters={quoteattr(text)}>'
        f'<textAttrs><DOMTextAttrs aliasText="false" size="{_fmt(size)}" '
        f'fillColor="{color}"/></textAttrs></DOMTextRun></textRuns>'
        "</DOMStaticText>"
    )


# --------------------------------------------------------------------------- #
# Converter
# --------------------------------------------------------------------------- #
@dataclass
class Converter:
    project: Project
    reverse_layers: bool = True
    symbol_names: dict[str, str] = field(default_factory=dict)
    texture_map: dict[str, tuple[str, tuple[int, int, int, int]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    current_clip: Clip | None = None
    _symbol_stack: list[str] = field(default_factory=list)
    _item_counter: int = 0
    _bitmap_names: dict[str, str] = field(default_factory=dict)

    # -- helpers ---------------------------------------------------------- #
    def new_item_id(self) -> str:
        self._item_counter += 1
        return f"{uuid.uuid4().hex[:8]}-{self._item_counter:08x}"

    def bitmap_name(self, texture_id: str) -> str:
        if texture_id not in self._bitmap_names:
            self._bitmap_names[texture_id] = f"ts_{texture_id[:8]}"
        return self._bitmap_names[texture_id]

    def symbol_name(self, clip_id: str) -> str:
        if clip_id not in self.symbol_names:
            clip = self.project.clips.get(clip_id)
            base = clip.name if clip else clip_id[:8]
            safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in base).strip() or "Clip"
            name = safe
            n = 2
            while name in self.symbol_names.values():
                name = f"{safe} {n}"
                n += 1
            self.symbol_names[clip_id] = name
        return self.symbol_names[clip_id]

    def layer_color(self, index: int) -> str:
        return LAYER_COLORS[index % len(LAYER_COLORS)]

    # -- drawing layer -> element ---------------------------------------- #
    def drawing_layer_element(self, layer_id: str, frame: int) -> str:
        assert self.current_clip is not None
        ltype = self.current_clip.layer_type(layer_id)
        layer_json = self.current_clip.layers.get(layer_id, {})
        if ltype in (LT_VECTOR, LT_SHAPE):
            return build_vector_element(layer_json, frame)
        if ltype == LT_PIXEL:
            entry = self.texture_map.get(layer_id)
            if entry is None or entry[0] not in self.project.textures:
                return ""
            texture_id, rect = entry
            return build_bitmap_element(layer_json, frame, self.bitmap_name(texture_id), rect)
        if ltype == LT_TEXT:
            return build_text_element(layer_json, frame)
        if ltype == LT_SYMBOL:
            return self.symbol_instance(layer_id, frame)
        if ltype == LT_GROUP:
            return "".join(
                p for p in (self.drawing_layer_element(c, frame) for c in layer_json.get("ch", [])) if p
            )
        return ""

    def symbol_instance(self, layer_id: str, frame: int) -> str:
        assert self.current_clip is not None
        layer_json = self.current_clip.layers.get(layer_id, {})
        ref = layer_json.get("cl") or {}
        clip_id = ref.get("id") if isinstance(ref, dict) else None
        if not clip_id or clip_id not in self.project.clips:
            return ""
        if clip_id in self._symbol_stack:
            self.warnings.append(f"recursive symbol {clip_id} skipped")
            return ""
        matrix, _ = transform_matrix(layer_json, frame)
        return (
            f'<DOMSymbolInstance libraryItemName={quoteattr(self.symbol_name(clip_id))} '
            'symbolType="movie clip" loop="loop">'
            f"<matrix>{matrix.to_xml()}</matrix>"
            "<transformationPoint><Point/></transformationPoint>"
            "</DOMSymbolInstance>"
        )

    # -- animation layer -> DOMLayer ------------------------------------- #
    def _layer_spans(self, layer_id: str) -> list[tuple[int, str]]:
        assert self.current_clip is not None
        layer_json = self.current_clip.layers.get(layer_id, {})
        ltype = self.current_clip.layer_type(layer_id)

        if ltype in (LT_SYMBOL, LT_VECTOR, LT_PIXEL, LT_TEXT, LT_SHAPE):
            return [(0, self.drawing_layer_element(layer_id, 0))]

        spans: list[tuple[int, str]] = []
        for drawing_id in layer_json.get("dr", []):
            drawing = self.current_clip.drawings.get(drawing_id)
            if not drawing:
                continue
            start = max(int(drawing.get("s", 1)) - 1, 0)
            parts = [self.drawing_layer_element(child, start) for child in drawing.get("ly", [])]
            spans.append((start, "".join(p for p in parts if p)))
        if not spans:
            spans = [(0, "")]
        spans.sort(key=lambda s: s[0])
        return spans

    def _dom_layer(self, layer_id: str, color_index: int) -> str:
        assert self.current_clip is not None
        ltype = self.current_clip.layer_type(layer_id)
        if ltype in (LT_CAMERA, LT_AUDIO, LT_EFFECT, LT_SCROLL, LT_BACKGROUND):
            return ""
        layer_json = self.current_clip.layers.get(layer_id, {})
        name = layer_json.get("Na") or "Layer"
        spans = self._layer_spans(layer_id)
        if not spans:
            return ""
        current = ' current="true" isSelected="true"' if color_index == 0 else ""
        frames = []
        for start, elements in spans:
            body = f"<elements>{elements}</elements>" if elements else "<elements/>"
            frames.append(f'<DOMFrame index="{start}" keyMode="9728">{body}</DOMFrame>')
        return (
            f'<DOMLayer name={quoteattr(name)} color="{self.layer_color(color_index)}"{current}>'
            f"<frames>{''.join(frames)}</frames>"
            "</DOMLayer>"
        )

    # -- clip -> DOMTimeline --------------------------------------------- #
    def clip_timeline(self, clip: Clip, name: str) -> str:
        prev = self.current_clip
        self.current_clip = clip
        try:
            order = list(reversed(clip.layer_order)) if self.reverse_layers else list(clip.layer_order)
            layers_xml: list[str] = []
            color_index = 0
            for lid in order:
                if clip.layer_type(lid) == LT_GROUP:
                    for child in clip.layers.get(lid, {}).get("ch", []):
                        xml = self._dom_layer(child, color_index)
                        if xml:
                            layers_xml.append(xml)
                            color_index += 1
                    continue
                xml = self._dom_layer(lid, color_index)
                if xml:
                    layers_xml.append(xml)
                    color_index += 1

            if not layers_xml:
                layers_xml.append(
                    '<DOMLayer name="Layer 1" color="#4FFF4F" current="true" isSelected="true">'
                    '<frames><DOMFrame index="0" keyMode="9728"><elements/></DOMFrame></frames>'
                    "</DOMLayer>"
                )
            return (
                f"<DOMTimeline name={quoteattr(name)}>"
                f"<layers>{''.join(layers_xml)}</layers>"
                "</DOMTimeline>"
            )
        finally:
            self.current_clip = prev

    # -- symbol library --------------------------------------------------- #
    def referenced_clips(self) -> list[str]:
        return list(self.symbol_names.keys())

    def library_symbol_xml(self, clip_id: str) -> str:
        name = self.symbol_name(clip_id)
        self._symbol_stack.append(clip_id)
        try:
            timeline = self.clip_timeline(self.project.clips[clip_id], name)
        finally:
            self._symbol_stack.pop()
        return (
            '<DOMSymbolItem xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xmlns="{XFL_NS}" name={quoteattr(name)} itemID="{self.new_item_id()}" '
            'symbolType="movie clip">'
            f"<timeline>{timeline}</timeline>"
            "</DOMSymbolItem>"
        )


def build_texture_map(project: Project) -> dict[str, tuple[str, tuple[int, int, int, int]]]:
    """Map drawing-layer id -> (texture id, destination rect (x, y, w, h))."""
    zf = zipfile.ZipFile(project.path)
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.write(zf.read(f"{project.id}/data"))
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    try:
        mapping = {}
        for owner, _otype, texture_id, _w, _h, x, y, w, h in conn.execute(
            "SELECT ownerId, ownerType, textureId, width, height, x, y, w, h "
            "FROM res_shared_textures"
        ):
            mapping[owner] = (texture_id, (int(x), int(y), int(w), int(h)))
        return mapping
    finally:
        conn.close()
        Path(tmp.name).unlink(missing_ok=True)


def collect_symbols(converter: Converter) -> None:
    seen: set[str] = set()

    def walk(clip: Clip) -> None:
        for lid, ltype in clip.layer_types.items():
            if ltype != LT_SYMBOL:
                continue
            ref = clip.layers.get(lid, {}).get("cl") or {}
            cid = ref.get("id") if isinstance(ref, dict) else None
            if cid and cid in converter.project.clips and cid not in seen:
                seen.add(cid)
                converter.symbol_name(cid)
                walk(converter.project.clips[cid])

    for clip in converter.project.clips.values():
        walk(clip)


def convert(project: Project, reverse_layers: bool = True) -> tuple[bytes, list[str]]:
    """Convert a parsed project into ``.fla`` bytes.  Returns ``(bytes, warnings)``."""
    converter = Converter(project=project, reverse_layers=reverse_layers)
    converter.texture_map = build_texture_map(project)
    collect_symbols(converter)

    scenes = project.scenes or list(project.clips.values())
    timelines = [converter.clip_timeline(clip, clip.name) for clip in scenes]

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
            f'<DOMBitmapItem name={quoteattr(name)} itemID="{converter.new_item_id()}" '
            'allowSmoothing="true" useImportedJPEGData="false" compressionType="lossless" '
            'originalCompressionType="lossless" quality="50" '
            f'href={quoteattr(name + ".png")} bitmapDataHRef={quoteattr("M " + name + " 0.dat")} '
            f'frameRight="{w * TWIPS}" frameBottom="{h * TWIPS}"/>'
        )

    symbols_xml = [
        f'<Include href={quoteattr(converter.symbol_name(cid) + ".xml")} '
        f'itemID="{converter.new_item_id()}"/>'
        for cid in converter.referenced_clips()
    ]

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
        zf.writestr("mimetype", "application/vnd.adobe.fla")
        zf.writestr(f"{project.name}.xfl", "PROXY-CS5")
        zf.writestr("DOMDocument.xml", dom)
        zf.writestr("PublishSettings.xml", _publish_settings(width, height, fps))
        zf.writestr("MobileSettings.xml", "")
        zf.writestr("META-INF/metadata.xml", _metadata_xml())

        for clip_id in converter.referenced_clips():
            zf.writestr(f"LIBRARY/{converter.symbol_name(clip_id)}.xml", converter.library_symbol_xml(clip_id))

        for texture_id in used_textures:
            if texture_id not in project.textures:
                continue
            w, h, raw = project.textures[texture_id]
            img = Image.frombytes("RGBA", (w, h), raw)
            png = io.BytesIO()
            img.save(png, format="PNG")
            name = converter.bitmap_name(texture_id)
            zf.writestr(f"LIBRARY/{name}.png", png.getvalue())
            zf.writestr(f"bin/M {name} 0.dat", png.getvalue())

    return buf.getvalue(), converter.warnings


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

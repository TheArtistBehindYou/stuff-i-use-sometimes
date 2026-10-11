"""Tests for the ToonSquid -> FLA converter.

A minimal synthetic ``.tsproj`` is built in-memory so the tests do not depend on
the (large) demo projects or on the network.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import struct
import tempfile
import zipfile
from pathlib import Path

import pytest

from tsproj2fla.converter import (
    Matrix,
    eval_track,
    path_to_edges,
    transform_matrix,
)
from tsproj2fla.dvtx import decode_dvtx
from tsproj2fla.tsproj import load_project

import lz4.block

TWIPS = 20


# --------------------------------------------------------------------------- #
# Builders for synthetic ToonSquid data
# --------------------------------------------------------------------------- #
def _sqlite_bytes(setup):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    setup(conn)
    conn.commit()
    conn.close()
    data = Path(tmp.name).read_bytes()
    Path(tmp.name).unlink()
    return data


def _encode_dvtx(width: int, height: int, rgba: bytes) -> bytes:
    out = bytearray(b"DVTX" + struct.pack("<H", 1) + struct.pack("<HH", width, height))
    prev = None
    for off in range(0, len(rgba), 65536):
        chunk = rgba[off:off + 65536]
        kwargs = {"store_size": False}
        if prev:
            kwargs["dict"] = prev
        comp = lz4.block.compress(chunk, **kwargs)
        out += b"bv41" + struct.pack("<II", len(chunk), len(comp)) + comp
        prev = chunk
    return bytes(out)


def _vector_layer(layer_id: str, color=(1.0, 0.0, 0.0, 1.0)) -> dict:
    return {
        "Na": "Vector",
        "Id": layer_id,
        "V": 3,
        "v": 1,
        "sW": 0,
        "cS": 0,
        "sA": 1,
        "fC": {"v": 1, "k": [{"f": 0, "vl": {"v": 1, "c": {"r": color[0], "g": color[1], "b": color[2], "a": color[3]}}}]},
        "sC": {"v": 1, "k": [{"f": 0, "vl": {"v": 1, "c": {"r": 0, "g": 0, "b": 0, "a": 0}}}]},
        "tf": {"v": 1, "pi": [0, 0], "pX": {"v": 1, "k": [{"f": 0, "vl": 0}]},
               "pY": {"v": 1, "k": [{"f": 0, "vl": 0}]},
               "sX": {"v": 1, "k": [{"f": 0, "vl": 1}]}, "sY": {"v": 1, "k": [{"f": 0, "vl": 1}]},
               "r": {"v": 1, "k": [{"f": 0, "vl": 0}]}},
        "p": {"v": 1, "k": [{"f": 0, "vl": {
            "v": 1, "cl": True,
            "cp": [
                {"po": [10.0, 10.0], "pT": 0, "ou": [0, 0], "in": [0, 0], "r": 0},
                {"po": [100.0, 10.0], "pT": 0, "ou": [0, 0], "in": [0, 0], "r": 0},
                {"po": [55.0, 80.0], "pT": 0, "ou": [0, 0], "in": [0, 0], "r": 0},
            ],
        }}]},
    }


def build_project_zip(path: Path) -> None:
    root = "11111111-2222-3333-4444-555555555555"
    scene_id = "AAAAAAA1-0000-0000-0000-000000000001"
    anim_layer_id = "BBBBBBB1-0000-0000-0000-000000000002"
    vector_id = "CCCCCCC1-0000-0000-0000-000000000003"
    drawing_id = "DDDDDDD1-0000-0000-0000-000000000004"
    texture_id = "EEEEEEE1-0000-0000-0000-000000000005"
    pixel_id = "FFFFFFF1-0000-0000-0000-000000000006"
    symbol_clip_id = "AAAAAAA2-0000-0000-0000-000000000007"
    symbol_layer_id = "BBBBBBB2-0000-0000-0000-000000000008"
    symbol_child_id = "CCCCCCC2-0000-0000-0000-000000000009"
    text_id = "DDDDDDD2-0000-0000-0000-00000000000A"

    def data_db(conn):
        conn.execute('CREATE TABLE meta_d (key TEXT PRIMARY KEY, value BLOB)')
        conn.execute('CREATE TABLE meta_i (key TEXT PRIMARY KEY, value INTEGER)')
        conn.execute('CREATE TABLE scenes (id TEXT PRIMARY KEY, "index" INTEGER)')
        conn.execute('CREATE TABLE res_anim_clips (id TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE res_audio_clips (id TEXT PRIMARY KEY, fileType INTEGER)')
        conn.execute('CREATE TABLE res_video_clips (id TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE res_shared_textures (ownerId TEXT, ownerType INTEGER, textureId TEXT, width INTEGER, height INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, PRIMARY KEY (ownerId, ownerType))')
        conn.execute('CREATE TABLE res_shared_owners (ownerId TEXT, ownerType INTEGER, textureId TEXT, "index" INTEGER, PRIMARY KEY (ownerId, ownerType))')
        conn.execute('CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT, lMD TEXT, children BLOB, otherData BLOB)')
        conn.execute(
            "INSERT INTO meta_d VALUES ('me', ?)",
            (json.dumps({"na": "Test Project", "re": {"w": 640, "h": 480}, "fr": 24, "id": root}).encode(),),
        )
        conn.execute("INSERT INTO scenes VALUES (?, 0)", (scene_id,))
        conn.execute("INSERT INTO res_anim_clips VALUES (?)", (scene_id,))
        conn.execute("INSERT INTO res_anim_clips VALUES (?)", (symbol_clip_id,))
        conn.execute(
            "INSERT INTO res_shared_textures VALUES (?, 1, ?, 4, 4, 300, 100, 4, 4)",
            (pixel_id, texture_id),
        )

    def anim_db(conn):
        conn.execute('CREATE TABLE animationClip (id TEXT PRIMARY KEY, data BLOB)')
        conn.execute('CREATE TABLE drawings (id TEXT PRIMARY KEY, data BLOB)')
        conn.execute('CREATE TABLE layers (id TEXT PRIMARY KEY, type INTEGER, data BLOB)')
        conn.execute('CREATE TABLE meta_i (key TEXT PRIMARY KEY, value INTEGER)')
        clip = {
            "na": "Scene 1", "fr": 24, "tF": 12, "re": {"w": 640, "h": 480},
            "id": scene_id, "v": 5,
            "lO": [anim_layer_id, vector_id, pixel_id, symbol_layer_id, text_id],
        }
        conn.execute("INSERT INTO animationClip VALUES (?, ?)", (scene_id, json.dumps(clip).encode()))

        drawing = {"id": drawing_id, "l": anim_layer_id, "s": 1, "e": 6, "v": 3, "ly": [vector_id]}
        conn.execute("INSERT INTO drawings VALUES (?, ?)", (drawing_id, json.dumps(drawing).encode()))

        conn.execute("INSERT INTO layers VALUES (?, 10, ?)",
                     (anim_layer_id, json.dumps({"Na": "Animation", "Id": anim_layer_id, "V": 3, "v": 2, "dr": [drawing_id]}).encode()))
        conn.execute("INSERT INTO layers VALUES (?, 3, ?)",
                     (vector_id, json.dumps(_vector_layer(vector_id)).encode()))
        conn.execute("INSERT INTO layers VALUES (?, 0, ?)",
                     (pixel_id, json.dumps({"Na": "Pixel", "Id": pixel_id, "V": 3, "v": 4,
                                            "tf": {"p": [300, 100], "pv": [300, 100]}}).encode()))
        conn.execute("INSERT INTO layers VALUES (?, 4, ?)",
                     (symbol_layer_id, json.dumps({"Na": "Squid", "Id": symbol_layer_id, "V": 3, "v": 4,
                                                   "cl": {"v": 1, "id": symbol_clip_id}}).encode()))
        conn.execute("INSERT INTO layers VALUES (?, 2, ?)",
                     (text_id, json.dumps({"Na": "Title", "Id": text_id, "V": 3, "v": 5,
                                           "t": "Hello", "fS": 40, "fN": "System Font Bold",
                                           "co": {"r": 1, "g": 1, "b": 1, "a": 1}, "al": 1,
                                           "tf": {"p": [10, 20], "pv": [10, 20]}}).encode()))

    def symbol_db(conn):
        conn.execute('CREATE TABLE animationClip (id TEXT PRIMARY KEY, data BLOB)')
        conn.execute('CREATE TABLE drawings (id TEXT PRIMARY KEY, data BLOB)')
        conn.execute('CREATE TABLE layers (id TEXT PRIMARY KEY, type INTEGER, data BLOB)')
        conn.execute('CREATE TABLE meta_i (key TEXT PRIMARY KEY, value INTEGER)')
        symbol_clip = {
            "na": "Squid", "fr": 24, "tF": 12, "re": {"w": 640, "h": 480},
            "id": symbol_clip_id, "v": 5,
            "lO": [symbol_child_id],
        }
        conn.execute("INSERT INTO animationClip VALUES (?, ?)",
                     (symbol_clip_id, json.dumps(symbol_clip).encode()))
        conn.execute("INSERT INTO layers VALUES (?, 3, ?)",
                     (symbol_child_id, json.dumps(_vector_layer(symbol_child_id)).encode()))

    rgba = bytes([255, 0, 0, 255]) * 16
    texture = _encode_dvtx(4, 4, rgba)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/data", _sqlite_bytes(data_db))
        zf.writestr(f"{root}/Animations/{scene_id}.tsanim", _sqlite_bytes(anim_db))
        zf.writestr(f"{root}/Animations/{symbol_clip_id}.tsanim", _sqlite_bytes(symbol_db))
        zf.writestr(f"{root}/Textures/{texture_id}", texture)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_dvtx_roundtrip():
    rgba = bytes([1, 2, 3, 4]) * (4 * 4)
    width, height, raw = decode_dvtx(_encode_dvtx(4, 4, rgba))
    assert (width, height) == (4, 4)
    assert raw == rgba


def test_eval_track_constant_and_keyframes():
    assert eval_track(3.5, 0) == 3.5
    track = {"v": 1, "k": [{"f": 0, "vl": 1}, {"f": 5, "vl": 9}]}
    assert eval_track(track, 0) == 1
    assert eval_track(track, 4) == 1
    assert eval_track(track, 5) == 9
    assert eval_track(None, 0, default=7) == 7


def test_transform_matrix_with_pivot():
    layer = {"tf": {"v": 1, "pi": [50, 50],
                    "pX": {"v": 1, "k": [{"f": 0, "vl": 150}]},
                    "pY": {"v": 1, "k": [{"f": 0, "vl": 150}]},
                    "sX": {"v": 1, "k": [{"f": 0, "vl": 1}]},
                    "sY": {"v": 1, "k": [{"f": 0, "vl": 1}]},
                    "r": {"v": 1, "k": [{"f": 0, "vl": 0}]}}}
    matrix, pivot = transform_matrix(layer, 0)
    assert pivot == (50.0, 50.0)
    # pivot point maps to the stored position
    assert matrix.apply(50, 50) == (150.0, 150.0)


def test_path_to_edges_format_a_closed():
    value = {"v": 1, "cl": True, "cp": [
        {"po": [0, 0], "pT": 0, "ou": [0, 0], "in": [0, 0]},
        {"po": [10, 0], "pT": 0, "ou": [0, 0], "in": [0, 0]},
    ]}
    edges = path_to_edges(value)
    assert edges.startswith("!0 0")
    assert "|200 0" in edges  # 10px * 20 twips
    assert edges.endswith("|0 0")


def test_path_to_edges_format_b_cubic():
    value = {"s": [{"c": False, "p": [
        {"p": [0, 0], "o": [1, 0], "i": [0, 0]},
        {"p": [10, 0], "o": [0, 0], "i": [-1, 0]},
    ]}]}
    edges = path_to_edges(value)
    # XFL's edges attribute has no cubic command, so cubics become polylines.
    assert edges.startswith("!0 0")
    assert "(;" not in edges
    assert edges.count("|") > 1
    assert edges.endswith("|200 0")  # 10px * 20 twips, endpoint preserved


def test_path_to_edges_force_close():
    value = {"s": [{"c": False, "p": [
        {"p": [0, 0], "o": [0, 0], "i": [0, 0]},
        {"p": [10, 0], "o": [0, 0], "i": [0, 0]},
        {"p": [10, 10], "o": [0, 0], "i": [0, 0]},
    ]}]}
    # Open path stays open ...
    open_edges = path_to_edges(value)
    assert not open_edges.endswith("|0 0")
    # ... but a fill needs the closing segment back to the start point.
    closed_edges = path_to_edges(value, force_close=True)
    assert closed_edges.endswith("|0 0")


def test_flatten_cubic_hits_endpoints():
    from tsproj2fla.converter import _flatten_cubic

    pts = _flatten_cubic((0, 0), (10, 10), (20, 10), (30, 0))
    assert pts[-1][0] == pytest.approx(30, abs=1e-6)
    assert pts[-1][1] == pytest.approx(0, abs=1e-6)
    assert len(pts) > 1


def test_dom_layers_are_contiguous_from_zero(tmp_path):
    """Every layer must have a frame starting at index 0 (Animate requirement)."""
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    project = load_project(src)
    from tsproj2fla.converter import convert
    import re

    data, _ = convert(project)
    dom = zipfile.ZipFile(io.BytesIO(data)).read("DOMDocument.xml").decode()
    for layer in re.findall(r"<DOMLayer[^>]*>(.*?)</DOMLayer>", dom, re.S):
        indices = [int(i) for i in re.findall(r'<DOMFrame index="(\d+)"', layer)]
        assert indices, "layer has no frames"
        assert indices[0] == 0, f"first frame index {indices[0]} != 0"


def test_library_item_ids_are_consistent(tmp_path):
    """Include/bitmap itemIDs must match the items they reference.

    Animate resolves library items by itemID; a mismatch opens an empty stage.
    """
    import re

    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    project = load_project(src)
    from tsproj2fla.converter import convert

    data, _ = convert(project)
    zf = zipfile.ZipFile(io.BytesIO(data))
    dom = zf.read("DOMDocument.xml").decode()
    for href, item_id in re.findall(r'<Include href="([^"]+)" itemID="([^"]+)"', dom):
        symbol = zf.read(f"LIBRARY/{href}").decode()
        assert f'itemID="{item_id}"' in symbol, f"itemID mismatch for {href}"


def test_mimetype_is_stored_and_first(tmp_path):
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    project = load_project(src)
    from tsproj2fla.converter import convert

    data, _ = convert(project)
    zf = zipfile.ZipFile(io.BytesIO(data))
    first = zf.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/vnd.adobe.fla"


def test_dat_bitmap_roundtrip():
    from tsproj2fla.converter import _encode_dat_bitmap, _decode_dat_bitmap

    pixels = bytes([
        10, 20, 30, 255,   # opaque
        40, 50, 60, 0,     # transparent
        70, 80, 90, 128,   # semi-transparent
    ])
    data = _encode_dat_bitmap(3, 1, pixels)
    assert data[0] == 0x03 and data[1] == 0x05  # 32-bit magic
    w, h, out = _decode_dat_bitmap(data)
    assert (w, h) == (3, 1)
    assert out[0:4] == bytes([10, 20, 30, 255])
    assert out[4:8] == bytes([0, 0, 0, 0])
    assert out[8] == 70 and out[11] == 128


def test_bin_dat_files_are_pixel_planes(tmp_path):
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    project = load_project(src)
    from tsproj2fla.converter import convert

    data, _ = convert(project)
    zf = zipfile.ZipFile(io.BytesIO(data))
    dats = [i for i in zf.infolist() if i.filename.startswith("bin/M ")]
    assert dats, "no bin/M *.dat entries written"
    for info in dats:
        blob = zf.read(info.filename)
        assert blob[0] == 0x03 and blob[1] == 0x05  # not a PNG
        assert info.compress_type == zipfile.ZIP_STORED



def test_end_to_end_conversion(tmp_path):
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)

    project = load_project(src)
    assert project.name == "Test Project"
    assert (project.width, project.height, project.fps) == (640, 480, 24)
    assert len(project.textures) == 1

    from tsproj2fla.converter import convert

    data, warnings = convert(project)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert zf.read("mimetype") == b"application/vnd.adobe.fla"
    assert "DOMDocument.xml" in names

    dom = zf.read("DOMDocument.xml").decode()
    assert 'width="640"' in dom and 'height="480"' in dom and 'frameRate="24"' in dom
    assert "<DOMShape>" in dom                     # vector layer converted
    assert "<DOMBitmapInstance" in dom             # pixel layer converted
    assert 'href="ts_' in dom                       # bitmap media item present
    # bitmap source + data written
    assert any(n.startswith("LIBRARY/ts_") and n.endswith(".png") for n in names)
    assert any(n.startswith("bin/M ts_") for n in names)
    # well-formed XML
    import xml.dom.minidom as M
    M.parseString(dom)


def test_reverse_layer_order(tmp_path):
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    project = load_project(src)
    from tsproj2fla.converter import convert
    import re

    fwd, _ = convert(project, reverse_layers=False)
    rev, _ = convert(project, reverse_layers=True)
    fwd_names = re.findall(r'<DOMLayer name="([^"]+)"', zipfile.ZipFile(io.BytesIO(fwd)).read("DOMDocument.xml").decode())
    rev_names = re.findall(r'<DOMLayer name="([^"]+)"', zipfile.ZipFile(io.BytesIO(rev)).read("DOMDocument.xml").decode())
    # The scene's Background layer is appended last and is unaffected by reversal.
    assert fwd_names[:-1] == list(reversed(rev_names[:-1]))
    assert fwd_names[-1] == rev_names[-1] == "Background"


def test_load_sqlite_deserialize_leaves_no_temp_files():
    """The in-memory path must not create temporary files at all."""
    import tsproj2fla.tsproj as tsproj

    before = set(Path(tempfile.gettempdir()).glob("*.sqlite"))
    handle = tsproj._load_sqlite(_sqlite_bytes(lambda c: c.execute("CREATE TABLE t (x)")))
    handle.close()
    after = set(Path(tempfile.gettempdir()).glob("*.sqlite"))
    assert before == after
    assert tsproj._HAS_DESERIALIZE  # this environment uses the in-memory path


def test_load_sqlite_fallback_removes_temp_file(monkeypatch):
    """Old-Python fallback must delete its temp file once the connection closes."""
    import tsproj2fla.tsproj as tsproj

    monkeypatch.setattr(tsproj, "_HAS_DESERIALIZE", False)
    handle = tsproj._load_sqlite(_sqlite_bytes(lambda c: c.execute("CREATE TABLE t (x)")))
    tmp_path = Path(handle.tmp_path)
    assert tmp_path.exists()
    # The file must still exist while the connection is open: this is the Windows
    # lock that made the old code fail when it deleted the file before closing.
    handle.close()
    assert not tmp_path.exists()


def test_load_project_leaves_no_temp_files(tmp_path):
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    before = set(Path(tempfile.gettempdir()).glob("*.sqlite"))
    load_project(src)
    after = set(Path(tempfile.gettempdir()).glob("*.sqlite"))
    assert before == after


def _dom(tmp_path):
    src = tmp_path / "Test.tsproj"
    build_project_zip(src)
    from tsproj2fla.converter import convert
    fla, _ = convert(load_project(src))
    return zipfile.ZipFile(io.BytesIO(fla)), fla


def test_symbols_are_flattened_into_layers(tmp_path):
    """Symbols must not survive as library clips; their layers are inlined."""
    z, fla = _dom(tmp_path)
    dom = z.read("DOMDocument.xml").decode()
    assert "<Include" not in dom
    assert "DOMSymbolInstance" not in dom
    assert not [n for n in z.namelist() if n.startswith("LIBRARY/") and n.endswith(".xml")]
    # The symbol's child vector layer is now a real layer on the scene timeline.
    assert 'name="Squid / Vector"' in dom
    squid = re.search(r'<DOMLayer name="Squid / Vector"[^>]*>(.*?)</DOMLayer>', dom, re.S)
    assert squid and "DOMShape" in squid.group(1)


def test_all_domlayers_have_contiguous_frames(tmp_path):
    """Every layer's frames start at 0 with no gaps (Animate rejects gaps)."""
    z, _ = _dom(tmp_path)
    dom = z.read("DOMDocument.xml").decode()
    for layer in re.findall(r"<DOMLayer[^>]*>(.*?)</DOMLayer>", dom, re.S):
        indices = [int(i) for i in re.findall(r'index="(-?\d+)"', layer)]
        assert indices and indices[0] == 0
        assert indices == list(range(len(indices)))


def test_text_uses_characters_element_and_face(tmp_path):
    z, _ = _dom(tmp_path)
    dom = z.read("DOMDocument.xml").decode()
    assert "<characters>Hello</characters>" in dom
    assert 'face="_sans"' in dom
    assert 'bold="true"' in dom
    assert 'size="40"' in dom
    # No bogus legacy attributes.
    assert "DOMTextRun characters=" not in dom
    assert 'face="Regular"' not in dom


def test_bitmap_dat_header_has_twip_dimensions(tmp_path):
    z, _ = _dom(tmp_path)
    name = next(n for n in z.namelist() if n.startswith("bin/M "))
    dat = z.read(name)
    width = int.from_bytes(dat[4:6], "little")
    height = int.from_bytes(dat[6:8], "little")
    assert int.from_bytes(dat[12:16], "little") == width * 20
    assert int.from_bytes(dat[20:24], "little") == height * 20


def test_symbol_instance_carries_transform(tmp_path):
    """The inlined symbol layer keeps its placement transform."""
    z, _ = _dom(tmp_path)
    dom = z.read("DOMDocument.xml").decode()
    # Pixel layer placed at (300, 100) must appear as a bitmap instance matrix.
    assert "DOMBitmapInstance" in dom

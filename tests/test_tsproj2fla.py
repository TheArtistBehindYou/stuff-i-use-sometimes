"""Tests for the ToonSquid -> FLA converter.

A minimal synthetic ``.tsproj`` is built in-memory so the tests do not depend on
the (large) demo projects or on the network.
"""
from __future__ import annotations

import io
import json
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
            "lO": [anim_layer_id, vector_id, pixel_id],
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

    rgba = bytes([255, 0, 0, 255]) * 16
    texture = _encode_dvtx(4, 4, rgba)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/data", _sqlite_bytes(data_db))
        zf.writestr(f"{root}/Animations/{scene_id}.tsanim", _sqlite_bytes(anim_db))
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
    assert edges.startswith("!0 0")
    assert "(;" in edges and ");" in edges


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
    assert fwd_names == list(reversed(rev_names))

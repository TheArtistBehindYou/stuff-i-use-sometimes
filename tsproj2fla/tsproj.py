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
from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dvtx import decode_dvtx

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

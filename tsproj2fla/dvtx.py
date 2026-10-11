"""Decoder for ToonSquid's ``DVTX`` pixel/texture container.

Layout (little-endian)::

    "DVTX" | u16 version | u16 width | u16 height | 4 reserved bytes
    then N blocks of: "bv41" | u32 uncompressed_len | u32 compressed_len | payload

Each block is an independent LZ4 block.  The uncompressed stream is a flat
RGBA8 buffer (``width * height * 4`` bytes, top-down).  Blocks that are not the
first are compressed against the previous block as an LZ4 dictionary, which is
how the encoder gets long-distance matches across block boundaries.
"""
from __future__ import annotations

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

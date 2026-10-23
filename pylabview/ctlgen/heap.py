"""Generic codec for the FPHb / BDHb object heaps.

A heap section is: u32 uncompressed-size + zlib( u32 heap-size + node stream ).
Each node in the stream:
  byte0 = sizeSpec(3) | hasAttr(1) | scope(2) | rawTag[9:8](2)
  byte1 = rawTag[7:0]                 rawTag == 1023 -> real tag follows as i32
  [attr count U124, then per attr: id S124, value S24]   if hasAttr
  [content length U124]               if sizeSpec == 6
  [content]                           sizeSpec 1..4 -> that many bytes; 0 = none/False; 7 = True
scope: 0 = open (children follow until a matching close), 1 = leaf, 2 = close.
tag = rawTag - 31.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from io import BytesIO

OPEN, LEAF, CLOSE = 0, 1, 2


# --- variable-size integers -------------------------------------------------

def rd_u124(b: BytesIO) -> int:
    v = b.read(1)[0]
    if v == 255:
        return int.from_bytes(b.read(2), "big")
    if v == 254:
        return int.from_bytes(b.read(4), "big")
    return v


def wr_u124(v: int) -> bytes:
    if v >= 0xFFFF:
        return b"\xfe" + v.to_bytes(4, "big")
    if v >= 0xFE:
        return b"\xff" + v.to_bytes(2, "big")
    return bytes([v])


def rd_s124(b: BytesIO) -> int:
    v = int.from_bytes(b.read(1), "big", signed=True)
    if v == -128:
        return int.from_bytes(b.read(2), "big", signed=True)
    if v == -127:
        return int.from_bytes(b.read(4), "big", signed=True)
    return v


def wr_s124(v: int) -> bytes:
    if -126 <= v <= 127:
        return v.to_bytes(1, "big", signed=True)
    if -0x8000 <= v <= 0x7FFF:
        return b"\x80" + v.to_bytes(2, "big", signed=True)
    return b"\x81" + v.to_bytes(4, "big", signed=True)


def rd_s24(b: BytesIO) -> int:
    v = int.from_bytes(b.read(2), "big", signed=True)
    if v == -0x8000:
        return int.from_bytes(b.read(4), "big", signed=True)
    return v


def wr_s24(v: int) -> bytes:
    if -0x8000 < v < 0x7FFF:
        return v.to_bytes(2, "big", signed=True)
    return b"\x80\x00" + v.to_bytes(4, "big", signed=True)


# --- node tree ---------------------------------------------------------------

@dataclass
class Node:
    tag: int
    attrs: list[tuple[int, int]] = field(default_factory=list)
    content: bytes | bool | None = None
    children: list["Node"] | None = None  # None -> leaf, list -> open/close pair
    # Rarely the close record carries its own attrs/content; keep it for fidelity.
    close: "Node | None" = None

    def find(self, tag: int) -> "Node | None":
        return next((c for c in self.children or [] if c.tag == tag), None)

    def find_all(self, tag: int) -> list["Node"]:
        return [c for c in self.children or [] if c.tag == tag]


def _read_record(b: BytesIO):
    h0, h1 = b.read(2)
    size_spec, has_attr, scope = h0 >> 5, (h0 >> 4) & 1, (h0 >> 2) & 3
    raw = ((h0 & 3) << 8) | h1
    tag = int.from_bytes(b.read(4), "big", signed=True) if raw == 1023 else raw - 31
    attrs = []
    if has_attr:
        for _ in range(rd_u124(b)):
            aid = rd_s124(b)
            attrs.append((aid, rd_s24(b)))
    if size_spec == 0:
        content = None
    elif size_spec == 7:
        content = True
    elif size_spec <= 4:
        content = b.read(size_spec)
    elif size_spec == 6:
        content = b.read(rd_u124(b))
    else:
        raise ValueError(f"sizeSpec {size_spec}")
    return scope, Node(tag, attrs, content)


def _write_record(out: bytearray, n: Node, scope: int) -> None:
    c = n.content
    if c is None:
        size_spec = 0
    elif c is True:
        size_spec = 7
    elif c is False:
        size_spec = 0
    elif len(c) <= 4 and len(c) != 0:
        size_spec = len(c)
    else:
        size_spec = 6
    raw = n.tag + 31 if n.tag + 31 < 1023 else 1023
    out.append((size_spec << 5) | ((1 if n.attrs else 0) << 4) | (scope << 2) | (raw >> 8))
    out.append(raw & 0xFF)
    if raw == 1023:
        out += n.tag.to_bytes(4, "big", signed=True)
    if n.attrs:
        out += wr_u124(len(n.attrs))
        for aid, val in n.attrs:
            out += wr_s124(aid) + wr_s24(val)
    if size_spec == 6:
        out += wr_u124(len(c)) + c
    elif 1 <= size_spec <= 4:
        out += c


def parse_nodes(raw: bytes) -> list[Node]:
    b = BytesIO(raw)
    roots: list[Node] = []
    stack: list[Node] = []
    while b.tell() < len(raw):
        scope, n = _read_record(b)
        if scope == CLOSE:
            opener = stack.pop()
            if opener.tag != n.tag:
                raise ValueError(f"close tag {n.tag} != open tag {opener.tag}")
            if n.attrs or n.content is not None:
                opener.close = n
            continue
        (stack[-1].children if stack else roots).append(n)
        if scope == OPEN:
            n.children = []
            stack.append(n)
    if stack:
        raise ValueError("unterminated heap")
    return roots


def emit_nodes(nodes: list[Node]) -> bytes:
    out = bytearray()

    def walk(n: Node) -> None:
        if n.children is None:
            _write_record(out, n, LEAF)
            return
        _write_record(out, n, OPEN)
        for c in n.children:
            walk(c)
        _write_record(out, n.close or Node(n.tag), CLOSE)

    for n in nodes:
        walk(n)
    return bytes(out)


# --- section-level helpers --------------------------------------------------

def unpack_section(data: bytes) -> bytes:
    """Section payload -> decompressed heap stream (without its u32 length)."""
    (usize,) = struct.unpack_from(">I", data)
    raw = zlib.decompress(data[4:])
    assert len(raw) == usize
    (hsize,) = struct.unpack_from(">I", raw)
    assert hsize == len(raw) - 4, (hsize, len(raw))
    return raw[4:]


def pack_section(stream: bytes) -> bytes:
    raw = struct.pack(">I", len(stream)) + stream
    return struct.pack(">I", len(raw)) + zlib.compress(raw, 9)


def load(data: bytes) -> list[Node]:
    return parse_nodes(unpack_section(data))


def dump(nodes: list[Node]) -> bytes:
    return pack_section(emit_nodes(nodes))

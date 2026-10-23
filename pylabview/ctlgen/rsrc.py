"""RSRC container: the outer file format shared by .vi/.ctl/.lvlib etc.

Layout (all big-endian):
  [RSRC header #1]                      32 bytes, info_offset points at header #2
  [data area]                           per section: u32 size + payload, padded to 4
  [RSRC header #2]                      identical copy of header #1
  [block-info list header]              5 x u32
  [block-info count-1]                  u32
  [block headers]                       ident(4) + sectioncount-1 + offset-to-section-list
  [section starts]                      idx, name_offset, int3, data_offset, int5 (5 x u32/i32)
  [name table]                          pascal strings
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

HDR = struct.Struct(">6sH4s4sIIII")
NO_NAME = 0xFFFFFFFF


@dataclass
class Section:
    index: int
    data: bytes
    name: bytes | None = None
    int3: int = 0
    int5: int = 0


@dataclass
class Block:
    ident: bytes
    sections: list[Section] = field(default_factory=list)


@dataclass
class Rsrc:
    ftype: bytes = b"LVCC"
    creator: bytes = b"LBVW"
    fmtver: int = 3
    blocks: list[Block] = field(default_factory=list)
    # Order in which section data appears in the data area, as (ident, section_index).
    # None = block order. LabVIEW does not always store data in block-header order.
    data_order: list[tuple[bytes, int]] | None = None
    # Order of names in the name table, same encoding. None = block order.
    name_order: list[tuple[bytes, int]] | None = None
    list_hdr: tuple[int, int, int] = (0, 0, 0x20)

    def block(self, ident: bytes) -> Block | None:
        return next((b for b in self.blocks if b.ident == ident), None)

    def section_data(self, ident: bytes, index: int | None = None) -> bytes:
        b = self.block(ident)
        if b is None:
            raise KeyError(ident)
        s = b.sections[0] if index is None else next(s for s in b.sections if s.index == index)
        return s.data


def read(buf: bytes) -> Rsrc:
    magic, fmtver, ftype, creator, info_off, info_size, data_off, data_size = HDR.unpack_from(buf, 0)
    if magic not in (b"RSRC\r\n", b"RSRC\0\0"):
        raise ValueError("not an RSRC file")
    pos = info_off + HDR.size
    d1, d2, d3, bi_off, bi_size = struct.unpack_from(">5I", buf, pos)
    bi = info_off + bi_off
    (count_m1,) = struct.unpack_from(">I", buf, bi)
    names_base = info_off + bi_size  # name table follows block info
    rs = Rsrc(ftype=ftype, creator=creator, fmtver=fmtver, list_hdr=(d1, d2, d3))
    data_pos: list[tuple[int, tuple[bytes, int]]] = []
    name_pos: list[tuple[int, tuple[bytes, int]]] = []
    for i in range(count_m1 + 1):
        ident, scount_m1, soff = struct.unpack_from(">4sII", buf, bi + 4 + i * 12)
        blk = Block(ident)
        for j in range(scount_m1 + 1):
            idx, name_off, int3, doff, int5 = struct.unpack_from(">iIIII", buf, bi + soff + j * 20)
            p = data_off + doff
            (size,) = struct.unpack_from(">I", buf, p)
            name = None
            if name_off != NO_NAME:
                n = buf[names_base + name_off]
                name = buf[names_base + name_off + 1: names_base + name_off + 1 + n]
                name_pos.append((name_off, (ident, idx)))
            blk.sections.append(Section(idx, buf[p + 4: p + 4 + size], name, int3, int5))
            data_pos.append((doff, (ident, idx)))
        rs.blocks.append(blk)
    natural = [(b.ident, s.index) for b in rs.blocks for s in b.sections]
    dord = [k for _, k in sorted(data_pos)]
    rs.data_order = None if dord == natural else dord
    nord = [k for _, k in sorted(name_pos)]
    natural_named = [(b.ident, s.index) for b in rs.blocks for s in b.sections if s.name is not None]
    rs.name_order = None if nord == natural_named else nord
    return rs


def write(rs: Rsrc) -> bytes:
    secs = {(b.ident, s.index): s for b in rs.blocks for s in b.sections}
    natural = [(b.ident, s.index) for b in rs.blocks for s in b.sections]

    # Data area
    data = bytearray()
    doffs: dict[tuple[bytes, int], int] = {}
    for key in rs.data_order or natural:
        s = secs[key]
        doffs[key] = len(data)
        data += struct.pack(">I", len(s.data)) + s.data
        data += b"\0" * (-len(data) % 4)

    # Name table
    names = bytearray()
    noffs: dict[tuple[bytes, int], int] = {}
    named = [k for k in natural if secs[k].name is not None]
    for key in rs.name_order or named:
        noffs[key] = len(names)
        n = secs[key].name
        names += bytes([len(n)]) + n

    data_off = HDR.size
    info_off = data_off + len(data)

    # Block info: count, headers, then section starts
    nblocks = len(rs.blocks)
    starts_off = 4 + nblocks * 12
    heads = bytearray(struct.pack(">I", nblocks - 1))
    starts = bytearray()
    for b in rs.blocks:
        heads += struct.pack(">4sII", b.ident, len(b.sections) - 1, starts_off + len(starts))
        for s in b.sections:
            key = (b.ident, s.index)
            starts += struct.pack(">iIIII", s.index, noffs.get(key, NO_NAME), s.int3, doffs[key], s.int5)
    bi_off = HDR.size + 20
    bi_size = bi_off + len(heads) + len(starts)
    info = bytearray()
    info += struct.pack(">5I", *rs.list_hdr, bi_off, bi_size)
    info += heads + starts + names
    info_size = HDR.size + len(info)

    magic = b"RSRC\r\n" if rs.fmtver >= 3 else b"RSRC\0\0"
    hdr = HDR.pack(magic, rs.fmtver, rs.ftype, rs.creator, info_off, info_size, data_off, len(data))
    return hdr + bytes(data) + hdr + bytes(info)

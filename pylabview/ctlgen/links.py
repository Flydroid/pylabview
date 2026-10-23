"""Link records: LIBN (owning libraries) and LIvi/LIfp/LIbd link tables.

Layouts, as LabVIEW 25 writes them (all big-endian):

  LIBN    u32 count, count x pascal string            outermost owner first
  LIxx    u16 1, 4cc kind (LVCC|FPHP|BDHP), u32 count, entries..., u16 3
  entry   u16 2, 4cc link type, link-type specific body
  VILB    "VI to library":  u32 0 (empty qualified name), PTH0 path to the owner file, u32 2
  PTH0    "PTH0", u32 length, u16 type (0 absolute, 1 relative), u16 count, count x pascal string
          A relative path is relative to the referencing file itself: an empty component
          means "up one level", so a file in the same folder is ["", "Name.lvlib"].
"""
from __future__ import annotations

import os
import struct
from pathlib import Path


def pstr(s: str) -> bytes:
    b = s.encode("mac_roman")
    return bytes([len(b)]) + b


def pth0(components: list[str], relative: bool = True) -> bytes:
    body = struct.pack(">HH", 1 if relative else 0, len(components)) + b"".join(pstr(c) for c in components)
    return b"PTH0" + struct.pack(">I", len(body)) + body


def relative_components(from_file: Path, to_file: Path) -> list[str]:
    """LabVIEW relative path from from_file to to_file: one "" per level up (the first one
    leaves the file itself), then the folder names down to the target."""
    rel = os.path.relpath(Path(to_file).resolve(), Path(from_file).resolve())
    parts = Path(rel).parts
    return ["" if p == ".." else p for p in parts]


def libn(owners: list[str]) -> bytes:
    return struct.pack(">I", len(owners)) + b"".join(pstr(o) for o in owners)


class Link:
    """One link-table entry. Its encoding depends on its offset in the section (alignment
    padding), so entries are rendered by link_table()."""

    def __init__(self, ident: bytes, render):
        self.ident = ident
        self.render = render  # f(offset of the body within the section) -> bytes


def _basic(off: int, qualname: list[str], path: bytes, save_flag: int) -> bytes:
    """BasicLinkSaveInfo: pad to 4, qualified name, pad to 2, path, u32 save flag."""
    out = b"\0" * (-off % 4)
    out += struct.pack(">I", len(qualname)) + b"".join(pstr(q) for q in qualname)
    out += b"\0" * (-(off + len(out)) % 2)
    return out + path + struct.pack(">I", save_flag)


# VILinkRefInfo packed into one byte (LV14+): FieldA | LibVersion << 1 | Field4 << 6.
# 0x42 = LibVersion 1, Field4 1: what LabVIEW 25 writes for links to typedefs.
VI_LINK_REF_COMPACT = 0x42


def _typed(off: int, qualname, path, save_flag, type_id: int) -> bytes:
    """TypedLinkSaveInfo: basic info, U2p2 TypeID, VILinkRefInfo, u32 typed-link flags."""
    out = _basic(off, qualname, path, save_flag)
    out += struct.pack(">H", type_id) + bytes([VI_LINK_REF_COMPACT]) + struct.pack(">I", 0)
    return out


def vilb(owner_path: list[str]) -> Link:
    """VI -> owning library/class."""
    return Link(b"VILB", lambda off: _basic(off, [], pth0(owner_path), 2))


def vicc(qualname: list[str], path: list[str], type_id: int, relative: bool = True) -> Link:
    """VI -> custom control (a typedef this file uses); type_id = top-level TypeID of its type."""
    return Link(b"VICC", lambda off: _typed(off, qualname, pth0(path, relative), 0, type_id))


def tdcc(qualname: list[str], path: list[str], uids: list[int], lib_path: list[str] | None,
         relative: bool = True) -> Link:
    """Front-panel heap -> custom control: the heap objects (by uid) that instantiate the
    typedef, plus the path of the typedef's owning library (empty PTH0 if it has none)."""
    def render(off):
        out = _typed(off, qualname, pth0(path, relative), 0, 0)
        out += struct.pack(">I", len(uids)) + b"".join(struct.pack(">I", u) for u in uids)
        # No owning library: an empty PTH0 (length 0) still followed by its 4 zero header bytes
        out += pth0(lib_path, relative) if lib_path else b"PTH0" + bytes(8)
        return out
    return Link(b"TDCC", render)


def link_table(kind: bytes, entries: list) -> bytes:
    """u16 1, kind, u32 count, then per entry u16 2 + ident + body, and u16 3 at the end."""
    out = b"\x00\x01" + kind + struct.pack(">I", len(entries))
    for e in entries:
        if isinstance(e, bytes):  # pre-rendered
            out += e
            continue
        out += b"\x00\x02" + e.ident
        out += e.render(len(out))
    return out + b"\x00\x03"

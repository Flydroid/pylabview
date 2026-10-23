"""VCTP: the consolidated type-descriptor table.

Section payload: u32 uncompressed-size + zlib(
    u32 count
    count x TD record: u16 len (incl. this 4-byte header), u8 flags, u8 type, body
    U2p2 top-level count, U2p2 x count  (flat TD index per top-level slot)
)
Other blocks (CONP, CPC2, DTHP, heap typeDesc nodes, LIfp ...) refer to types by
1-based top-level index ("TypeID"), not by flat index.

flags 0x40 = HasLabel: body ends with a pascal-string label.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

HAS_LABEL = 0x40

# Type codes
VOID, I8, I16, I32, I64, U8, U16, U32, U64, SGL, DBL, EXT = range(0x00, 0x0C)
UNIT_U8, UNIT_U16, UNIT_U32 = 0x15, 0x16, 0x17  # enums ("unit" ints with item labels)
BOOL = 0x21
STRING, PATH, TAG = 0x30, 0x32, 0x37
ARRAY, CLUSTER, VARIANT = 0x40, 0x50, 0x53
BLOCK, REPEATED_BLOCK = 0x60, 0x64
REFNUM, PTR, TYPEDEF = 0x70, 0x80, 0xF1

NUMERIC = {I8, I16, I32, I64, U8, U16, U32, U64, SGL, DBL, EXT}


@dataclass
class TD:
    type: int
    flags: int = 0
    body: bytes = b""  # everything after the 4-byte header, label included

    def encode(self) -> bytes:
        return struct.pack(">HBB", 4 + len(self.body), self.flags, self.type) + self.body


@dataclass
class Vctp:
    tds: list[TD] = field(default_factory=list)
    top: list[int] = field(default_factory=list)  # flat index per top-level slot

    # Builders --------------------------------------------------------------

    def add(self, td: TD) -> int:
        self.tds.append(td)
        return len(self.tds) - 1

    def add_top(self, flat: int) -> int:
        """Register a flat TD as top-level; returns its 1-based TypeID."""
        self.top.append(flat)
        return len(self.top)


def _rd_u2p2(b: bytes, p: int) -> tuple[int, int]:
    (v,) = struct.unpack_from(">H", b, p)
    if v & 0x8000:
        (lo,) = struct.unpack_from(">H", b, p + 2)
        return ((v & 0x7FFF) << 16) | lo, p + 4
    return v, p + 2


def _wr_u2p2(v: int) -> bytes:
    return struct.pack(">H", v) if v <= 0x7FFF else struct.pack(">I", v | 0x80000000)


def parse(raw: bytes) -> Vctp:
    (count,) = struct.unpack_from(">I", raw)
    p = 4
    v = Vctp()
    for _ in range(count):
        ln, flags, typ = struct.unpack_from(">HBB", raw, p)
        v.tds.append(TD(typ, flags, raw[p + 4: p + ln]))
        p += ln
    n, p = _rd_u2p2(raw, p)
    for _ in range(n):
        x, p = _rd_u2p2(raw, p)
        v.top.append(x)
    if p != len(raw):
        raise ValueError(f"trailing {len(raw) - p} bytes in VCTP")
    return v


def emit(v: Vctp) -> bytes:
    out = bytearray(struct.pack(">I", len(v.tds)))
    for td in v.tds:
        out += td.encode()
    out += _wr_u2p2(len(v.top))
    for x in v.top:
        out += _wr_u2p2(x)
    return bytes(out)


def unpack_section(data: bytes) -> bytes:
    (usize,) = struct.unpack_from(">I", data)
    raw = zlib.decompress(data[4:])
    assert len(raw) == usize
    return raw


def pack_section(raw: bytes) -> bytes:
    return struct.pack(">I", len(raw)) + zlib.compress(raw, 9)


# --- TD body helpers -----------------------------------------------------

def pstr(s: bytes) -> bytes:
    return bytes([len(s)]) + s


def labelled(typ: int, body: bytes, label: str | None) -> TD:
    """Attach optional label. LabVIEW pads the whole record to an even length."""
    flags = 0
    if label is not None:
        flags |= HAS_LABEL
        body += pstr(label.encode("mac_roman"))
    if (4 + len(body)) % 2:
        body += b"\0"
    return TD(typ, flags, body)


def td_label(td: TD) -> str | None:
    """Best-effort: read the trailing pascal label (for inspection only)."""
    if not td.flags & HAS_LABEL:
        return None
    b = td.body
    for start in range(len(b) - 1, -1, -1):
        n = b[start]
        end = start + 1 + n
        if end == len(b) or (end == len(b) - 1 and b[-1] == 0):
            if n and all(32 <= c < 127 or c > 127 for c in b[start + 1:end]):
                return b[start + 1:end].decode("mac_roman")
    return None

"""FTAB (font table) handling, so front-panel objects copied from other files keep their fonts.

FTAB layout (section 128): u16 1, u16 2, u16 3, u16 count, count x 16-byte records
(u32 offset of the font name from the section start, 12 bytes of font properties), then the
names as pascal strings. Heap 'fontRun' objects refer to fonts by index into this table, so an
object taken from another file must have its font ids translated to the destination table.
"""
from __future__ import annotations

import struct

from . import names

FONT_RUN = None  # resolved lazily (names imports pylabview)


def parse(ftab: bytes) -> tuple[bytes, list[tuple[str, bytes]]]:
    head = ftab[:6]
    (count,) = struct.unpack_from(">H", ftab, 6)
    fonts = []
    for i in range(count):
        (off,) = struct.unpack_from(">I", ftab, 8 + 16 * i)
        props = ftab[12 + 16 * i:24 + 16 * i]
        name = ftab[off + 1:off + 1 + ftab[off]].decode("mac_roman")
        fonts.append((name, props))
    return head, fonts


def build(head: bytes, fonts: list[tuple[str, bytes]]) -> bytes:
    recs, names_area = b"", b""
    base = 8 + 16 * len(fonts)
    for name, props in fonts:
        recs += struct.pack(">I", base + len(names_area)) + props
        n = name.encode("mac_roman")
        names_area += bytes([len(n)]) + n
    return head + struct.pack(">H", len(fonts)) + recs + names_area


class FontTable:
    """The destination table; grows when copied objects use fonts it does not have yet."""

    def __init__(self, ftab: bytes):
        self.head, self.fonts = parse(ftab)
        self.changed = False

    def index_of(self, font: tuple[str, bytes]) -> int:
        if font not in self.fonts:
            self.fonts.append(font)
            self.changed = True
        return self.fonts.index(font)

    def remap(self, obj, src_ftab: bytes | None, set_int):
        """Translate every fontRun font id in obj's subtree from src_ftab to this table."""
        if not src_ftab:
            return
        _, src = parse(src_ftab)
        fr_cls = names.class_id("fontRun")

        def walk(n, in_run=False):
            run = in_run or dict(n.attrs).get(-2) == fr_cls
            for ch in n.children or []:
                if run and ch.tag == 9 and isinstance(ch.content, bytes):  # fontRun.fontid
                    old = int.from_bytes(ch.content, "big", signed=True)
                    if 0 <= old < len(src):
                        set_int(ch, self.index_of(src[old]))
                walk(ch, run)
        walk(obj)

    def to_bytes(self) -> bytes:
        return build(self.head, self.fonts)

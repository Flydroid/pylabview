"""Human-readable dump of a .ctl using ctlkit codecs.  usage: dump.py file.ctl [--no-heap]"""
import struct
import sys
from pathlib import Path

from . import heap
from . import names
from . import rsrc
from . import vctp

TYPE_NAMES = {0: "Void", 1: "I8", 2: "I16", 3: "I32", 4: "I64", 5: "U8", 6: "U16", 7: "U32", 8: "U64",
              9: "SGL", 10: "DBL", 11: "EXT", 0x15: "EnumU8", 0x16: "EnumU16", 0x17: "EnumU32",
              0x21: "Bool", 0x30: "String", 0x32: "Path", 0x37: "Tag", 0x40: "Array", 0x50: "Cluster",
              0x53: "Variant", 0x60: "Block", 0x64: "RepeatedBlock", 0x70: "Refnum", 0x80: "Ptr", 0xF1: "TypeDef"}


def fmt_content(c, tag):
    if c is None:
        return ""
    if c is True:
        return " = True"
    if len(c) == 8 and tag in ("bounds", "docBounds", "dBounds"):
        return " = " + str(struct.unpack(">4h", c))
    if len(c) == 4 and tag in ("minPaneSize", "origin", "zoomCenter"):
        return " = " + str(struct.unpack(">2h", c))
    if tag == "text" or tag == "caption":
        try:
            return f' = "{c.decode("mac_roman")}"'
        except Exception:  # noqa: BLE001
            pass
    if len(c) <= 4:
        return f" = {int.from_bytes(c, 'big', signed=True)} [{c.hex()}]"
    return f" = <{len(c)}B {c[:24].hex()}{'..' if len(c) > 24 else ''}>"


def dump_heap(nodes, out, depth=0, ctx=None):
    for n in nodes:
        a = dict(n.attrs)
        cls = a.get(-2)
        if cls is not None:
            ctx_here = cls
        else:
            ctx_here = ctx
        tname = names.tag_name(n.tag, ctx)
        attrs = " ".join(
            f"{names.SYS_ATTR_NAME.get(k, k)}={names.class_name(v) if k == -2 else v}" for k, v in n.attrs)
        out.append(f"{'  ' * depth}{tname}({n.tag}){' ' + attrs if attrs else ''}{fmt_content(n.content, tname)}"
                   f"{' [close carries data]' if n.close else ''}")
        if n.children is not None:
            dump_heap(n.children, out, depth + 1, ctx_here)


def main():
    p = Path(sys.argv[1])
    r = rsrc.read(p.read_bytes())
    out = [f"# {p.name}  type={r.ftype} fmtver={r.fmtver} list_hdr={r.list_hdr}"]
    for b in r.blocks:
        for s in b.sections:
            if b.ident in (b"FPHb", b"BDHb", b"VCTP"):
                continue
            out.append(f"{b.ident.decode('latin1')}[{s.index}]{' name=' + repr(s.name) if s.name else ''}"
                       f"{' int3=' + str(s.int3) if s.int3 else ''} ({len(s.data)}B) {s.data[:48].hex()}"
                       f"{'..' if len(s.data) > 48 else ''}")
    v = vctp.parse(vctp.unpack_section(r.section_data(b"VCTP")))
    out.append("## VCTP")
    for i, td in enumerate(v.tds):
        out.append(f"  [{i:2}] {TYPE_NAMES.get(td.type, hex(td.type)):13} fl={td.flags:02x} "
                   f"label={vctp.td_label(td)!r:28} body={td.body.hex()}")
    out.append("  top: " + " ".join(f"{i + 1}->{x}" for i, x in enumerate(v.top)))
    if "--no-heap" not in sys.argv:
        for ident in (b"FPHb", b"BDHb"):
            out.append(f"## {ident.decode()}")
            dump_heap(heap.load(r.section_data(ident)), out, 1)
    print("\n".join(out))


if __name__ == "__main__":
    main()

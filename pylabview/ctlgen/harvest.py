"""Harvest front-panel object templates from a corpus of LabVIEW-saved .ctl files.

For every element control found inside a cluster's pane, key it by
(heap class, TD type) and keep the smallest example, preferring files whose
font table equals the skeleton's (so fontRun ids stay meaningful).

Templates from files edited in the preferred control style (--style, default fuse)
win over all others; within a style the smallest example wins.

Writes templates.json: { "<class>:<tdtype hex>": {"src", "style", "heap", "types"} }
usage: python -m pylabview.ctlgen.harvest <skeleton.ctl> <out.json> <corpus root>... [--style fuse]
"""
import argparse
import json
import struct
import sys
from pathlib import Path

from . import heap
from . import names
from . import rsrc
from . import vctp

T_ZPLANE = names.tag_id("zPlaneList")
T_PANE = names.tag_id("paneHierarchy")
T_TYPEDESC = names.tag_id("typeDesc")
T_DDO = names.tag_id("ddo")
STD_CLUST = names.class_id("stdClust")


def attrs(n):
    return dict(n.attrs)


def walk(nodes):
    for n in nodes:
        yield n
        if n.children:
            yield from walk(n.children)


def nested_typedescs(n, cls=None):
    """Heap type ids used anywhere in n's subtree (tag ids are class-relative)."""
    c = attrs(n).get(-2, cls)
    for ch in n.children or []:
        if names.tag_name(ch.tag, c) == "typeDesc" and isinstance(ch.content, bytes):
            yield int.from_bytes(ch.content, "big", signed=True)
        yield from nested_typedescs(ch, c)


def heap_type_resolver(r):
    v = vctp.parse(vctp.unpack_section(r.section_data(b"VCTP")))
    cnt, shift = struct.unpack(">HH", r.section_data(b"DTHP"))

    def resolve(hid):
        flat = v.top[shift + hid - 2]
        return v.tds[flat]
    return resolve


# Sources never used for templates: XNode support files are special-cased by LabVIEW
# and carry caveats that do not apply to ordinary controls.
EXCLUDE = ("xnode",)


# LVSR ctrlIndStyle (u16 @78): the control style the file was edited with.
LVSR_CTRL_STYLE = 78
STYLES = {"modern": 0, "classic": 1, "system": 2, "silver": 3, "fuse": 4}


def corpus_files(roots):
    for root in roots:
        for p in Path(root).rglob("*.ctl"):
            if not any(x in str(p).lower() for x in EXCLUDE):
                yield root, p


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("skeleton", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("roots", type=Path, nargs="+", help="folders of LabVIEW-saved .ctl files")
    ap.add_argument("--style", default="fuse", choices=sorted(STYLES),
                    help="prefer templates from files edited in this control style (default: fuse)")
    args = ap.parse_args()
    want_style = STYLES[args.style]
    sk = rsrc.read(args.skeleton.read_bytes())
    skel_ftab = sk.section_data(b"FTAB", 128)
    # Only harvest from files saved by the same LabVIEW major version as the skeleton
    skel_ver = sk.section_data(b"LVSR")[:1]
    best: dict[str, tuple] = {}
    for root, p in corpus_files(args.roots):
        try:
            r = rsrc.read(p.read_bytes())
            lvsr = r.section_data(b"LVSR")
            if lvsr[:1] != skel_ver:
                continue
            style_ok = struct.unpack_from(">H", lvsr, LVSR_CTRL_STYLE)[0] == want_style
            same_font = r.section_data(b"FTAB", 128) == skel_ftab
            resolve = heap_type_resolver(r)
            nodes = heap.load(r.section_data(b"FPHb"))
        except Exception:  # noqa: BLE001 - odd/old files are simply not template sources
            continue
        for n in walk(nodes):
            if attrs(n).get(-2) != STD_CLUST:
                continue
            pane = n.find(T_PANE)
            z = pane.find(T_ZPLANE) if pane else None
            for el in (z.children if z else []):
                td_node = el.find(T_TYPEDESC)
                if td_node is None:
                    continue
                td = resolve(int.from_bytes(td_node.content, "big", signed=True))
                # Typedef instances are kept too ("typeDef:f1"): the generator reuses the wrapper
                # object (label, annex, stamp) and swaps in the referenced control.
                key = f"{names.class_name(attrs(el)[-2])}:{td.type:02x}"
                blob = heap.emit_nodes([el])
                score = (0 if style_ok else 1, 0 if same_font else 1, len(blob))
                if key not in best or score < best[key][0]:
                    # Types of everything inside the template that has its own heap type
                    # (e.g. a path control's browse button), so the generator can recreate them.
                    tds = {}
                    for hid in nested_typedescs(el):
                        t = resolve(hid)
                        tds[str(hid)] = t.encode().hex()
                    best[key] = (score, str(p), blob.hex(), tds, r.section_data(b"FTAB", 128).hex())
    # ftab: the source's font table, to translate the template's font ids on use
    data = {k: {"src": s, "style": args.style if sc[0] == 0 else "other", "same_font": sc[1] == 0,
                "heap": h, "types": tds, "ftab": ft}
            for k, (sc, s, h, tds, ft) in sorted(best.items())}
    args.out.write_text(json.dumps(data, indent=1))
    for k, d in data.items():
        print(f"{k:22} style={d['style']:7} same_font={d['same_font']!s:5} {len(d['heap']) // 2:5}B  {d['src']}")


if __name__ == "__main__":
    main()

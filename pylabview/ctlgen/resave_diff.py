"""Compare a generated .ctl with the same file after LabVIEW opened and saved it.

usage: python -m pylabview.ctlgen.resave_diff generated.ctl labview_saved.ctl

LabVIEW always changes some things on save; those are reported separately as "expected":
  - VCTP gains its data-space types (udf/txd, DSInit, FP DCO table), shifting DTHP
  - HIST counters, FPSE estimate, RTSG guid
  - font ids among identical font table slots, pane docBounds
Everything else (types, labels, flags, geometry, links) should be identical; a difference
there is something the generator does not yet do the way LabVIEW does.
"""
import re
import struct
import sys
from pathlib import Path

from . import dump
from . import heap
from . import rsrc
from . import vctp

EXPECTED_BLOCKS = {b"RTSG", b"HIST", b"FPSE", b"DTHP", b"VCTP", b"FPHb"}
EXPECTED_HEAP = re.compile(r"^\s*(docBounds|fontid)\(")


def heap_lines(r: rsrc.Rsrc) -> list[str]:
    out: list[str] = []
    dump.dump_heap(heap.load(r.section_data(b"FPHb")), out, 0)
    # uids and heap type ids are renumbered by LabVIEW; compare structure and values only
    return [re.sub(r"uid=\d+", "uid=#", re.sub(r"typeDesc\(283\) = .*", "typeDesc(283) = #", x)) for x in out]


def user_types(r: rsrc.Rsrc) -> list[str]:
    """The labelled, user-visible TDs (data-space TDs LabVIEW adds are unlabelled or udf/txd)."""
    v = vctp.parse(vctp.unpack_section(r.section_data(b"VCTP")))
    out = []
    for td in v.tds:
        lab = vctp.td_label(td)
        if td.type == vctp.TYPEDEF or (lab and lab not in ("udf", "txd", "flg", "oRt", "eof")):
            out.append(f"{dump.TYPE_NAMES.get(td.type, hex(td.type))} {lab!r} {td.body.hex() if td.type != vctp.TYPEDEF else ''}")
    return sorted(set(out))


def main():
    a = rsrc.read(Path(sys.argv[1]).read_bytes())
    b = rsrc.read(Path(sys.argv[2]).read_bytes())
    problems = expected = 0

    ids_a = {x.ident for x in a.blocks}
    ids_b = {x.ident for x in b.blocks}
    for ident in sorted(ids_a | ids_b):
        if ident not in ids_a or ident not in ids_b:
            print(f"BLOCK {ident.decode('latin1')}: only in {'generated' if ident in ids_a else 'saved'}")
            problems += 1
            continue
        sa = {s.index: s.data for s in a.block(ident).sections}
        sb = {s.index: s.data for s in b.block(ident).sections}
        if sa != sb:
            if ident in EXPECTED_BLOCKS:
                expected += 1
            elif ident == b"LVSR":
                # signature hashes are per file; LabVIEW keeps them, so they should match too
                print("BLOCK LVSR differs:", " ".join(f"@{i}" for i in range(min(len(sa[0]), len(sb[0])))
                                                     if sa[0][i] != sb[0][i]))
                problems += 1
            else:
                print(f"BLOCK {ident.decode('latin1')} differs")
                problems += 1

    ta, tb = user_types(a), user_types(b)
    for x in sorted(set(ta) ^ set(tb)):
        print(("TYPE only generated: " if x in ta else "TYPE only saved:     ") + x)
        problems += 1

    import difflib
    ha, hb = heap_lines(a), heap_lines(b)
    for line in difflib.unified_diff(ha, hb, "generated", "saved", n=0, lineterm=""):
        if line.startswith(("---", "+++", "@@")):
            continue
        if EXPECTED_HEAP.match(line[1:]):
            expected += 1
            continue
        print("HEAP", line)
        problems += 1
    print(f"\n{problems} unexpected difference(s), {expected} expected LabVIEW save change(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

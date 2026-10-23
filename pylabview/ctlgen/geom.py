"""Print cluster/pane geometry of every stdClust in a .ctl (for comparing with LabVIEW re-saves)."""
import struct
import sys
from pathlib import Path

from . import heap
from . import names
from . import rsrc

T = names.tag_id
STD = names.class_id("stdClust")


def walk(n):
    yield n
    for c in n.children or []:
        yield from walk(c)


def r(n, tag):
    x = n.find(T(tag))
    return None if x is None else struct.unpack(f">{len(x.content) // 2}h", x.content)


def label(el):
    for p in el.find(T("partsList")).children:
        pid = p.find(T("partID"))
        if pid is not None and pid.content == b"\x10":
            t = p.find(T("textRec")).find(3)
            return t.content.decode() if t is not None else ""


(root,) = heap.load(rsrc.read(Path(sys.argv[1]).read_bytes()).section_data(b"FPHb"))
for c in (n for n in walk(root) if dict(n.attrs).get(-2) == STD):
    of = int.from_bytes(c.find(T("objFlags")).content, "big")
    print(f"cluster {label(c)!r} flags={of:#x} bounds={r(c, 'bounds')} savedSize={c.find(T('savedSize')).content.hex()}")
    for p in c.find(T("partsList")).children:
        pid = p.find(T("partID"))
        if p.find(T("bounds")) is not None:
            print(f"   part {int.from_bytes(pid.content, 'big')}: {r(p, 'bounds')}")
    pane = c.find(T("paneHierarchy"))
    print(f"   pane bounds={r(pane, 'bounds')} origin={r(pane, 'origin')} docBounds={r(pane, 'docBounds')}"
          f" paneFlags={int.from_bytes(pane.find(T('paneFlags')).content, 'big'):#x}")
    for p in pane.find(T("partsList")).children:
        pid = p.find(T("partID"))
        if p.find(T("bounds")) is not None:
            print(f"     pane part {int.from_bytes(pid.content, 'big')}: {r(p, 'bounds')}")
    for el in pane.find(T("zPlaneList")).children[:3]:
        print(f"     el {label(el)!r} {r(el, 'bounds')}")

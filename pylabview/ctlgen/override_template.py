"""Replace one templates.json entry with the first element of a given class found inside a cluster of a given file.

usage: override_template.py <templates.json> <key e.g. stdBool:21> <source .ctl> [element label]
"""
import json
import sys
from pathlib import Path

from . import heap
from . import names
from . import rsrc


def walk(n):
    yield n
    for c in n.children or []:
        yield from walk(c)


def label_of(el):
    for p in el.find(names.tag_id("partsList")).children:
        pid = p.find(names.tag_id("partID"))
        if pid is not None and pid.content == b"\x10":
            t = p.find(names.tag_id("textRec")).find(3)
            return t.content.decode("mac_roman") if t is not None else None


tpl_path, key, src = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
want_label = sys.argv[4] if len(sys.argv) > 4 else None
cls = names.class_id(key.split(":")[0])
(root,) = heap.load(rsrc.read(src.read_bytes()).section_data(b"FPHb"))
el = next(n for n in walk(root) if dict(n.attrs).get(-2) == cls and (want_label is None or label_of(n) == want_label))
tpl = json.loads(tpl_path.read_text())
tpl[key] = {"src": str(src), "same_font": None, "heap": heap.emit_nodes([el]).hex()}
tpl_path.write_text(json.dumps(tpl, indent=1))
print("replaced", key, "with", label_of(el), "from", src.name)

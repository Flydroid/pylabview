"""JSON -> .ctl (type definition of a cluster) binary writer.

usage: python -m pylabview.ctlgen.gen [--assets DIR] spec.json out.ctl

spec:
{
  "label":  "My Config",                        # cluster label (default: from file name)
  "fields": [ {"label": "Rate", "type": "DBL"},  # DBL SGL I8 I16 I32 I64 U8 U16 U32 String Bool Path
              {"label": "Mode", "type": "Enum", "rep": "U16", "values": ["A", "B"]},
              {"label": "Sub",  "type": "Cluster", "fields": [ ... ]},
              ... ]
}

Blocks that carry no information about the type (version, icon, fonts, history,
empty block diagram, ...) come from a LabVIEW-saved skeleton file. Everything
that describes the type -- VCTP, DTHP, CONP/CPC2, FPHb, MUID, FPSE -- is
generated. Front-panel objects for each element are instantiated from
templates harvested out of LabVIEW-saved files (templates.json).

Assets (skeleton.ctl, templates.json) are looked up in --assets, else
$CTLGEN_ASSETS, else the package's assets/ folder. They are built with
make_skeleton.py and harvest.py from LabVIEW-saved controls and are not
versioned, as they are derived from whatever controls they were taken from.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import struct
import time
import uuid
from pathlib import Path

from . import fonts
from . import heap
from . import links
from . import names
from . import rsrc
from . import typedef_link
from . import vctp
from .heap import Node

ASSETS = Path(os.environ.get("CTLGEN_ASSETS", Path(__file__).parent / "assets"))
SKELETON = ASSETS / "skeleton.ctl"
TEMPLATES = ASSETS / "templates.json"


def set_assets(path: Path):
    global ASSETS, SKELETON, TEMPLATES
    ASSETS = Path(path)
    SKELETON = ASSETS / "skeleton.ctl"
    TEMPLATES = ASSETS / "templates.json"

T = names.tag_id
CLASS, UID = -2, -3

# JSON type name -> (VCTP type code, template key)
SCALARS = {
    "I8": (vctp.I8, "stdNum:01"), "I16": (vctp.I16, "stdNum:02"), "I32": (vctp.I32, "stdNum:03"),
    "I64": (vctp.I64, "stdNum:04"), "U8": (vctp.U8, "stdNum:05"), "U16": (vctp.U16, "stdNum:06"),
    "U32": (vctp.U32, "stdNum:07"), "SGL": (vctp.SGL, "stdNum:09"), "DBL": (vctp.DBL, "stdNum:0a"),
    "String": (vctp.STRING, "stdString:30"), "Bool": (vctp.BOOL, "stdBool:21"),
    "Path": (vctp.PATH, "stdPath:32"), "U64": (vctp.U64, "stdNum:08"), "EXT": (vctp.EXT, "stdNum:0b"),
}

# LVSR (LabVIEW Save Record) layout, see pylabview.LVinstrument.LVSRData
LVSR_EXEC_FLAGS = 4             # u32; TypeDefVI = 1 << 20, StrictTypeDefVI = 1 << 21
LVSR_VI_SIGNATURE = 52          # 16 bytes, unique per file (GUID-like)
LVSR_FIELD50 = 80               # 16 bytes, unique per file in NI's files
LVSR_FIELD78 = 120              # 16 bytes, zero in ~43% of NI's LV25 controls
TYPEDEF_VI, STRICT_TYPEDEF_VI = 1 << 20, 1 << 21
LV_EPOCH_OFFSET = 2082844800    # seconds between 1904-01-01 (LabVIEW epoch) and 1970-01-01

# Geometry LabVIEW uses for an auto-arranged (vertical) cluster
EL_TOP, EL_LEFT = -47, -30
PANE_TOP, PANE_LEFT = 21, 4
CLUST_POS = (30, 30)
AUTOSIZE_VERTICAL = 0x2000000


def text_width(s: str) -> int:
    return round(6.2 * len(s)) + 5


# --- VCTP -------------------------------------------------------------------

def pstr_even(s: bytes) -> bytes:
    b = bytes([len(s)]) + s
    return b + (b"\0" if len(b) % 2 else b"")


def labelled_td(code: int, body: bytes, label: str | None) -> vctp.TD:
    """Append the label (even-padded pascal string) and set HasLabel; no label for None or ""."""
    if not label:
        return vctp.TD(code, 0, body)
    return vctp.TD(code, vctp.HAS_LABEL, body + pstr_even(label.encode("mac_roman")))


def td_scalar(code: int, label: str | None) -> vctp.TD:
    if code in vctp.NUMERIC:
        body = b"\0"
    elif code in (vctp.STRING, vctp.PATH):
        body = b"\xff\xff\xff\xff"
    elif code == vctp.BOOL:
        body = b""
    else:
        raise ValueError(hex(code))
    return labelled_td(code, body, label)


def td_cluster(kids: list[int], label: str | None) -> vctp.TD:
    return labelled_td(vctp.CLUSTER, struct.pack(f">H{len(kids)}H", len(kids), *kids), label)


def td_array(elem: int, label: str | None) -> vctp.TD:
    """1-D array: u16 ndims, per dim u32 (flags << 24 | fixed size; 0xFFFFFFFF = variable), u16 element TD."""
    return labelled_td(vctp.ARRAY, struct.pack(">HIH", 1, 0xFFFFFFFF, elem), label)


def td_typedef(qualified_name: list[str], nested: vctp.TD, strict_stamp: int = 0) -> vctp.TD:
    """strict_stamp: 0 for a plain typedef; for a strict typedef LabVIEW stores a timestamp
    (seconds since 1904) here -- non-zero in all 1319 strict NI LV25 controls, zero in all 2257 plain ones."""
    body = struct.pack(">II", strict_stamp, len(qualified_name))
    for part in qualified_name:
        p = part.encode("mac_roman")
        body += bytes([len(p)]) + p
    rec = nested.encode()
    # LabVIEW stores the nested record's length as (real length + 4)
    rec = struct.pack(">H", len(rec) + 4) + rec[2:]
    return vctp.TD(vctp.TYPEDEF, 0, body + rec)


def td_simple(code: int, body: bytes = b"") -> vctp.TD:
    return vctp.TD(code, 0, body)


ENUM_REPS = {"U8": vctp.UNIT_U8, "U16": vctp.UNIT_U16, "U32": vctp.UNIT_U32}


def td_enum(rep: str, values: list[str], label: str) -> vctp.TD:
    items = b"".join(bytes([len(x)]) + x for x in (s.encode("mac_roman") for s in values))
    body = struct.pack(">H", len(values)) + items
    if len(body) % 2:
        body += b"\0"
    return labelled_td(ENUM_REPS[rep], body + b"\0", label)


def add_field_td(v: vctp.Vctp, f: dict) -> int:
    """Add the TD(s) for one field (recursing into clusters); remembers the flat index on f."""
    t = f["type"]
    if t == "Cluster":
        kids = [add_field_td(v, c) for c in f["fields"]]
        f["_flat"] = v.add(td_cluster(kids, f["label"]))
    elif t == "Enum":
        f["_flat"] = v.add(td_enum(f.get("rep", "U16"), f["values"], f["label"]))
    elif t == "Array":
        elem = add_field_td(v, f["element"])
        f["_flat"] = v.add(td_array(elem, f["label"]))
        f["_index_flat"] = v.add(td_simple(vctp.I32, b"\0"))  # type of the index display
    elif t == "TypeDef":
        typedef_link.add_vctp(v, v._importer, f)
    else:
        f["_flat"] = v.add(td_scalar(SCALARS[t][0], f["label"]))
    return f["_flat"]


def alloc_heap_ids(v: vctp.Vctp, f: dict, shift: int):
    """Heap type ids in the order LabVIEW assigns them while writing the heap:
    a cluster before its elements; for an array the index display, the array, then the element;
    for a typedef instance the inner control, then the wrapper."""
    def hid(flat):
        return v.add_top(flat) - shift + 1
    if f["type"] == "Array":
        f["_index_hid"] = hid(f["_index_flat"])
        f["_hid"] = hid(f["_flat"])
        alloc_heap_ids(v, f["element"], shift)
        return
    if f["type"] == "TypeDef":
        f["_inner_hid"] = hid(f["_inner_flat"])
    f["_hid"] = hid(f["_flat"])
    for c in f.get("fields", []) if f["type"] == "Cluster" else []:
        alloc_heap_ids(v, c, shift)


def linked_fields(fields):
    for f in fields:
        if f["type"] == "TypeDef":
            yield f
        elif f["type"] == "Cluster":
            yield from linked_fields(f["fields"])
        elif f["type"] == "Array":
            yield from linked_fields([f["element"]])


def build_vctp(spec, fname):
    v = vctp.Vctp()
    v._importer = typedef_link.Importer(v)
    # Referenced typedefs come first in the top-level list (LabVIEW puts them before the
    # file's own typedef); their TypeIDs are what the VICC link records point at.
    refs = []
    for f in linked_fields(spec["fields"]):
        if all(r.path != f["_ref"].path for r, _ in refs):
            refs.append((f["_ref"], v.add_top(v._importer.td(f["_ref"], f["_ref"].td_flat))))
    spec["_refs"] = refs
    elem_flat = [add_field_td(v, f) for f in spec["fields"]]
    clust_label = spec["label"]
    stamp = int(time.time()) + LV_EPOCH_OFFSET if spec.get("strict") else 0
    typedef = v.add(td_typedef(spec["_qname"], td_cluster(elem_flat, clust_label), stamp))
    u8 = v.add(td_simple(vctp.U8, b"\0"))
    rb_u8 = v.add(td_simple(vctp.REPEATED_BLOCK, struct.pack(">IH", 792, u8)))
    i32 = v.add(td_simple(vctp.I32, b"\0"))
    rb_i32 = v.add(td_simple(vctp.REPEATED_BLOCK, struct.pack(">IH", 51, i32)))
    void = v.add(td_simple(vctp.VOID))
    clust = v.add(td_cluster(elem_flat, clust_label))

    conp = v.add_top(typedef)
    v.add_top(rb_u8)
    v.add_top(rb_i32)
    v.add_top(void)
    heap_shift = len(v.top) + 1
    heap_ids = {}
    heap_ids["fpdco"] = v.add_top(clust) - heap_shift + 1
    heap_ids["clust"] = v.add_top(clust) - heap_shift + 1
    for f in spec["fields"]:
        alloc_heap_ids(v, f, heap_shift)
    global _HEAP
    _HEAP = (v, heap_shift)  # more heap types get added while templates are instantiated
    return v, conp, heap_shift, heap_ids


_HEAP = None  # (Vctp, heap shift) of the file being generated


def new_heap_type(td: vctp.TD) -> int:
    v, shift = _HEAP
    return v.add_top(v.add(td)) - shift + 1


# --- FPHb -------------------------------------------------------------------

def child(n: Node, tag: str) -> Node:
    c = n.find(T(tag))
    if c is None:
        raise KeyError(tag)
    return c


def set_int(n: Node, val: int):
    """Store an integer the way LabVIEW does: minimal big-endian signed width."""
    for w in (1, 2, 3, 4):
        if -(1 << (8 * w - 1)) <= val < (1 << (8 * w - 1)):
            n.content = val.to_bytes(w, "big", signed=True)
            return
    raise ValueError(val)


def get_rect(n: Node) -> tuple[int, int, int, int]:
    return struct.unpack(">4h", n.content)


def set_rect(n: Node, r):
    n.content = struct.pack(">4h", *r)


def part(obj: Node, part_id: int) -> Node | None:
    for p in child(obj, "partsList").children:
        pid = p.find(T("partID"))
        if pid is not None and int.from_bytes(pid.content, "big", signed=True) == part_id:
            return p
    return None


def set_label(obj: Node, text: str) -> int:
    """Set an object's label (part 16). Returns the label's new right edge."""
    lab = part(obj, 16)
    of = child(lab, "objFlags")  # a template's label may be hidden; ours are shown
    set_int(of, int.from_bytes(of.content, "big", signed=True) & ~PART_HIDDEN)
    tr = child(lab, "textRec")
    txt = tr.find(3)  # textHair 'text'
    if txt is None:
        txt = Node(3)
        tr.children.insert(next(i for i, c in enumerate(tr.children) if c.tag == 2) + 1, txt)
    txt.content = text.encode("mac_roman")
    b = child(lab, "bounds")
    t, l, _, old_r = get_rect(b)
    new_r = l + text_width(text)
    set_rect(b, (t, l, t + 17, new_r))
    # Parts sharing the label's row and sitting right of it move with the label's right edge,
    # otherwise a longer label runs over them (e.g. label-left booleans).
    delta = new_r - old_r
    if delta > 0:
        for p in child(obj, "partsList").children:
            pb = p.find(T("bounds"))
            if p is lab or pb is None:
                continue
            pt, pl, pbt, pr = get_rect(pb)
            overlap = min(pbt, t + 17) - max(pt, t)
            if overlap > 4 and pl >= l and pl < new_r:
                set_rect(pb, (pt, pl + delta, pbt, pr + delta))
    return new_r


def parts_extent(obj: Node) -> tuple[int, int]:
    h = w = 0
    for p in child(obj, "partsList").children:
        b = p.find(T("bounds"))
        if getattr(p, "_hidden_by_gen", False):
            continue  # a part we hid (boolean text) no longer counts towards the object's size.
            # Not a generic objFlags test: an enum's digital display carries the same bit
            # and LabVIEW still includes it.
        if b is not None:
            t, l, bt, r = get_rect(b)
            h, w = max(h, bt), max(w, r)
    return h, w


def load_template(key: str, rebase: bool = True) -> Node:
    """rebase=False for templates whose nested controls the caller replaces or types itself
    (arrays: index display + element; typedef wrappers: the inner control)."""
    tpl = json.loads(TEMPLATES.read_text())
    if key not in tpl:
        raise KeyError(f"no front-panel template for {key}")
    (node,) = heap.parse_nodes(bytes.fromhex(tpl[key]["heap"]))
    if rebase:
        rebase_nested_types(node, tpl[key].get("types", {}))
    if _FONTS is not None and tpl[key].get("ftab"):
        _FONTS.remap(node, bytes.fromhex(tpl[key]["ftab"]), set_int)
    return node


_FONTS: "fonts.FontTable | None" = None  # font table of the file being generated


def rebase_nested_types(obj: Node, types: dict):
    """Give controls nested inside a template object (a path's browse button, an array's index
    display, ...) heap types of their own in the file being generated. The object's own typeDesc
    is set by the caller."""
    own = obj.find(T("typeDesc"))
    memo: dict[int, int] = {}

    def walk_td(n, cls):
        c = dict(n.attrs).get(CLASS, cls)
        for ch in n.children or []:
            if ch is not own and names.tag_name(ch.tag, c) == "typeDesc" and isinstance(ch.content, bytes):
                old = int.from_bytes(ch.content, "big", signed=True)
                if old not in memo:
                    rec = bytes.fromhex(types[str(old)])
                    ln, flags, typ = struct.unpack_from(">HBB", rec)
                    if typ in (vctp.CLUSTER, vctp.ARRAY, vctp.TYPEDEF, vctp.REPEATED_BLOCK):
                        raise NotImplementedError(f"template has a nested control of type {typ:#x}")
                    memo[old] = new_heap_type(vctp.TD(typ, flags, rec[4:ln]))
                set_int(ch, memo[old])
            walk_td(ch, c)
    walk_td(obj, None)


def walk(n: Node):
    yield n
    for c in n.children or []:
        yield from walk(c)


def renumber_uids(subtree: Node, next_uid: int) -> int:
    """Give every object in subtree a fresh uid, per node (several instances of one
    template share the same original uids, so a value->value remap would merge them).
    Class-less uid refs are ddoList entries, which the caller rebuilds afterwards."""
    for n in walk(subtree):
        a = dict(n.attrs)
        if UID in a and CLASS in a:
            n.attrs = [(k, next_uid if k == UID else v) for k, v in n.attrs]
            next_uid += 1
    return next_uid


def build_fphb(skel_nodes, spec, heap_ids):
    (root_obj,) = copy.deepcopy(skel_nodes)
    supc = child(root_obj, "root")
    pane = child(supc, "paneHierarchy")
    (fpdco,) = child(pane, "zPlaneList").children
    # The top-level cluster uses the cluster template too, so its frame matches the style of
    # the element templates (the skeleton's own cluster is just a placeholder).
    old = child(fpdco, "ddo")
    clust = load_template("stdClust:50")
    clust.tag = old.tag
    fpdco.children[fpdco.children.index(old)] = clust
    cpane = child(clust, "paneHierarchy")

    set_int(child(fpdco, "typeDesc"), heap_ids["fpdco"])
    set_int(child(clust, "typeDesc"), heap_ids["clust"])

    clusters: list[tuple[Node, list[Node]]] = []
    h, w = fill_cluster(clust, spec["label"], spec["fields"], clusters)
    offset_nested(clust)
    ct, cl = CLUST_POS
    set_rect(child(clust, "bounds"), (ct, cl, ct + h, cl + w))

    # uids: keep the skeleton's fixed panel-level uids, renumber the control subtree;
    # LabVIEW gives the fPDCO the highest uid.
    fixed_max = max(dict(n.attrs).get(UID, 0) for n in walk(root_obj) if not _inside(n, fpdco))
    nxt = renumber_uids(clust, fixed_max + 1)
    fpdco.attrs = [(k, nxt if k == UID else v) for k, v in fpdco.attrs]
    max_uid = nxt

    def uid_of(n):
        return dict(n.attrs)[UID]

    for c, elems in clusters:
        ddo = child(c, "ddoList")
        ddo.children = [Node(-6, [(UID, uid_of(e))]) for e in elems]
        ddo.attrs = [(k, len(elems) if k == -5 else v) for k, v in ddo.attrs]
    (ref,) = child(supc, "ddoList").children
    ref.attrs = [(UID, uid_of(fpdco))]

    n_objs = sum(1 for n in walk(root_obj) if UID in dict(n.attrs))
    # uids of the typedef-instance wrappers, per referenced file (for the TDCC links)
    wrappers: dict = {}
    for n in walk(root_obj):
        ref = getattr(n, "_link_ref", None)
        if ref is not None:
            wrappers.setdefault(ref.path, []).append(uid_of(n))
    spec["_wrappers"] = wrappers
    return [root_obj], max_uid, n_objs


def make_element(f: dict, clusters) -> tuple[Node, int, int]:
    """Instantiate the front-panel object for one field; returns (node, height, width)."""
    t = f["type"]
    if t == "Cluster":
        el = load_template("stdClust:50")
        h, w = fill_cluster(el, f["label"], f["fields"], clusters)
    elif t == "Enum":
        el = load_template(f"stdRing:{ENUM_REPS[f.get('rep', 'U16')]:02x}")
        set_label(el, f["label"])
        set_enum_items(el, f["values"])
        h, w = parts_extent(el)
    elif t == "Array":
        el, h, w = make_array(f, clusters)
    elif t == "TypeDef":
        # the wrapper's typeDesc is set inside make_wrapper; set again below (same value)
        el, h, w = typedef_link.make_wrapper(f, load_template, set_label, set_int, set_rect,
                                             parts_extent, hide_part, part, _FONTS)
    else:
        el = load_template(SCALARS[t][1])
        set_label(el, f["label"])
        if t == "Bool":
            hide_part(el, 22)  # boolean text ("OFF/ON"), hidden like LabVIEW's default checkbox
        h, w = parts_extent(el)
    set_int(child(el, "typeDesc"), f["_hid"])
    return el, h, w


def make_array(f: dict, clusters) -> tuple[Node, int, int]:
    """1-D array control (indArr): label, index display, data area holding one element 'ddo'.
    The template's data-area parts are resized around the new element."""
    el = load_template("indArr:40", rebase=False)
    set_label(el, f["label"])
    index = part(el, 8002)  # index display: a numeric of the I32 index type
    set_int(child(index, "typeDesc"), f["_index_hid"])

    old = child(el, "ddo")
    ot, ol, ob, orr = get_rect(child(old, "bounds"))
    item, eh, ew = make_element(f["element"], clusters)
    hide_part(item, 16)  # array elements carry a hidden label
    item.tag = old.tag   # the element sits under the array's 'ddo' tag
    eh, ew = parts_extent(item)
    set_rect(child(item, "bounds"), (ot, ol, ot + eh, ol + ew))
    el.children[el.children.index(old)] = item

    dh, dw = eh - (ob - ot), ew - (orr - ol)

    def grow(pid, top=0, left=0, bottom=0, right=0):
        p = part(el, pid)
        if p is not None:
            t, l, b, r = get_rect(child(p, "bounds"))
            set_rect(child(p, "bounds"), (t + top, l + left, b + bottom, r + right))
    grow(28, bottom=dh, right=dw)                         # data area
    grow(39, left=dw, bottom=dh, right=dw)                # (hidden) vertical scrollbar
    grow(38, top=dh, bottom=dh, right=dw)                 # (hidden) horizontal scrollbar
    grow(44, top=dh, left=dw, bottom=dh, right=dw)        # (hidden) corner
    grow(9, bottom=dh, right=dw)                          # frame
    h, w = parts_extent(el)
    return el, h, w


PART_HIDDEN = 0x8  # objFlags bit LabVIEW sets on a hidden part (seen on boolean text in re-saves)


def hide_part(el: Node, part_id: int):
    p = part(el, part_id)
    if p is not None:
        of = child(p, "objFlags")
        set_int(of, int.from_bytes(of.content, "big", signed=True) | PART_HIDDEN)
        p._hidden_by_gen = True


def translate_pane(clust: Node, dy: int, dx: int):
    """Shift the elements of one cluster pane (not nested panes) and its docBounds by (dy, dx)."""
    pane = child(clust, "paneHierarchy")
    for el in child(pane, "zPlaneList").children:
        b = child(el, "bounds")
        t, l, bt, r = get_rect(b)
        set_rect(b, (t + dy, l + dx, bt + dy, r + dx))
    d = child(pane, "docBounds")
    t, l, bt, r = get_rect(d)
    set_rect(d, (t + dy, l + dx, bt + dy, r + dx))


def offset_nested(clust: Node, acc=(0, 0)):
    """LabVIEW stores a nested cluster's elements so that, mapped back to the top-level
    cluster's pane frame, they start at (EL_TOP, EL_LEFT): stored = layout - cumulative pane offset.
    Verified for one nesting level from a LabVIEW re-save: 'ok' at y = -47 - (533 + 21) = -601,
    x = -30 - (-30 + 4) = -4. Deeper levels assume the offsets accumulate."""
    for el in child(child(clust, "paneHierarchy"), "zPlaneList").children:
        if dict(el.attrs).get(CLASS) == names.class_id("stdClust"):
            t, l, _, _ = get_rect(child(el, "bounds"))
            cum = (acc[0] + t + PANE_TOP, acc[1] + l + PANE_LEFT)
            translate_pane(el, -cum[0], -cum[1])
            offset_nested(el, cum)


def set_enum_items(el: Node, values: list[str]):
    """Ring/enum text part (12) holds the item strings as concatenated pascal strings."""
    ml = part(el, 12)
    child(ml, "buf").content = b"".join(bytes([len(x)]) + x for x in (s.encode("mac_roman") for s in values))
    # Widen the display to the longest item; the frame (9) and arrows follow.
    t, l, b, r = get_rect(child(ml, "bounds"))
    grow = max(0, l + text_width(max(values, key=len)) + 4 - r)
    set_rect(child(ml, "bounds"), (t, l, b, r + grow))
    for frame_id in (9, 28):  # classic/modern rings frame with part 9, Fuse rings with part 28
        fp = part(el, frame_id)
        if fp is not None:
            ft, fl, fb, fr = get_rect(child(fp, "bounds"))
            set_rect(child(fp, "bounds"), (ft, fl, fb, fr + grow))
    nl = part(el, 10)  # hidden digital display sits right of the text
    if nl is not None:
        t2, l2, b2, r2 = get_rect(child(nl, "bounds"))
        set_rect(child(nl, "bounds"), (t2, l2 + grow, b2, r2 + grow))


def fill_cluster(clust: Node, label: str, fields: list[dict], clusters) -> tuple[int, int]:
    """Populate a stdClust with element objects stacked vertically (LabVIEW's
    auto-arrange) and size its pane/frame. Returns the cluster's (height, width)."""
    cpane = child(clust, "paneHierarchy")
    # objFlags bit 0x2000000 = Autosizing: Arrange Vertically (set on 2286/3191 corpus clusters;
    # confirmed by a LabVIEW re-save after enabling it)
    of = child(clust, "objFlags")
    set_int(of, int.from_bytes(of.content, "big", signed=True) | AUTOSIZE_VERTICAL)
    elems = []
    y = EL_TOP
    content_w = 0
    for f in fields:
        el, h, w = make_element(f, clusters)
        set_rect(child(el, "bounds"), (y, EL_LEFT, y + h, EL_LEFT + w))
        y += h
        content_w = max(content_w, w)
        elems.append(el)
    content_h = y - EL_TOP

    zl = child(cpane, "zPlaneList")
    zl.children = elems
    zl.attrs = [(k, len(elems) if k == -5 else v) for k, v in zl.attrs]
    clusters.append((clust, elems))

    pane_rect = (PANE_TOP, PANE_LEFT, PANE_TOP + content_h, PANE_LEFT + content_w)
    set_rect(child(cpane, "bounds"), pane_rect)
    # Pane parts are relative to the pane: content area, scrollbars (17px) and corner.
    H, W = content_h, content_w
    pane_parts = {28: (0, 0, H, W), 39: (0, W, H, W + 17), 38: (H, 0, H + 17, W), 44: (H, W, H + 17, W + 17)}
    for pid, r in pane_parts.items():
        p = part(cpane, pid)
        if p is not None:
            set_rect(child(p, "bounds"), r)
    cpane_origin = cpane.find(T("origin"))
    if cpane_origin is not None:
        cpane_origin.content = struct.pack(">2h", -4, -4)
    set_rect(child(cpane, "docBounds"), (EL_TOP - 27, EL_LEFT - 10, y + 69, EL_LEFT + content_w + 18))
    set_rect(child(part(clust, 28), "bounds"), pane_rect)
    frame = (PANE_TOP - 4, 0, pane_rect[2] + 4, pane_rect[3] + 4)
    set_rect(child(part(clust, 9), "bounds"), frame)
    label_right = set_label(clust, label)
    return frame[2], max(frame[3], label_right)


def _inside(n: Node, sub: Node) -> bool:
    return any(x is n for x in walk(sub))


# --- assemble ---------------------------------------------------------------

def generate(spec: dict, out: Path):
    fname = out.name
    spec = dict(spec)
    spec.setdefault("label", fname.rsplit(".", 1)[0].split("--")[0])
    spec["fields"] = copy.deepcopy(spec["fields"])
    # Qualified name: owning library/class first, then the file (["My.lvlib", "X--cluster.ctl"])
    owners = spec.get("owners") or ([spec["library"]] if spec.get("library") else [])
    if owners and not spec.get("owner_file"):
        raise ValueError("'owners' needs 'owner_file' (path to the immediate .lvlib/.lvclass)")
    spec["_owners"] = owners
    spec["_qname"] = owners + [fname]

    def check(fields):
        for f in fields:
            if f["type"] == "Cluster":
                check(f["fields"])
            elif f["type"] == "Enum":
                if f.get("rep", "U16") not in ENUM_REPS:
                    raise ValueError(f"enum rep {f['rep']!r}")
            elif f["type"] == "Array":
                e = f["element"]
                e.setdefault("label", "")
                if e["type"] in ("Cluster", "Array"):
                    # Not yet verified against a LabVIEW re-save (pane coordinates inside arrays)
                    raise ValueError(f"array of {e['type']} is not supported yet ({f['label']!r})")
                check([e])
            elif f["type"] == "TypeDef":
                f["_ref"] = typedef_link.RefCtl(f["path"])
            elif f["type"] not in SCALARS:
                raise ValueError(f"unsupported type {f['type']!r} "
                                 f"(supports {sorted(SCALARS)} + Cluster, Enum, Array, TypeDef)")
    check(spec["fields"])

    skel = rsrc.read(SKELETON.read_bytes())
    global _FONTS
    _FONTS = fonts.FontTable(skel.section_data(b"FTAB", 128))
    v, conp, heap_shift, heap_ids = build_vctp(spec, fname)
    fp_nodes, max_uid, n_objs = build_fphb(heap.load(skel.section_data(b"FPHb")), spec, heap_ids)
    if _FONTS.changed:
        next(s for s in skel.block(b"FTAB").sections if s.index == 128).data = _FONTS.to_bytes()
    dthp = (len(v.top) - heap_shift + 1, heap_shift)

    gen = {
        b"VCTP": vctp.pack_section(vctp.emit(v)),
        b"FPHb": heap.dump(fp_nodes),
        b"DTHP": struct.pack(">HH", *dthp),
        b"CONP": struct.pack(">H", conp),
        b"CPC2": struct.pack(">H", conp),
        b"MUID": struct.pack(">I", max_uid),
        # "FP size estimate": in LabVIEW-saved files it is ~1.2x the number of uid objects
        b"FPSE": struct.pack(">I", n_objs * 6 // 5),
        b"RTSG": uuid.uuid4().bytes,
    }
    for b in skel.blocks:
        for s in b.sections:
            if b.ident in gen:
                s.data = gen[b.ident]
            if b.ident == b"LVSR":
                s.name = fname.encode("mac_roman")
                s.data = patch_lvsr(s.data, strict=bool(spec.get("strict")))
    vi_links, fp_links = typedef_link.link_entries(out, spec["_refs"], spec["_wrappers"])
    if spec["_owners"]:
        vi_links.insert(0, links.vilb(links.relative_components(out, Path(spec["owner_file"]))))
        skel.blocks.insert(0, rsrc.Block(b"LIBN", [rsrc.Section(0, links.libn(spec["_owners"]))]))
    skel.block(b"LIvi").sections[0].data = links.link_table(b"LVCC", vi_links)
    skel.block(b"LIfp").sections[0].data = links.link_table(b"FPHP", fp_links)
    # FPEx: u32 count of front-panel links, then one u32 per link (0 in all files seen)
    skel.block(b"FPEx").sections[0].data = struct.pack(">I", len(fp_links)) + bytes(4 * len(fp_links))
    out.write_bytes(rsrc.write(skel))


def register(out: Path, owner_file: Path, strict: bool = False):
    """Add the generated file as an item of its owning .lvlib / .lvclass (text insertion, so the
    rest of the XML stays byte-identical). LabVIEW must not have the owner open while this runs."""
    owner_file = Path(owner_file)
    text = owner_file.read_text(encoding="utf-8")
    url = "../" + Path(os.path.relpath(Path(out).resolve(), owner_file.resolve().parent)).as_posix()
    name = Path(out).name
    if f'URL="{url}"' in text:
        return False
    if owner_file.suffix.lower() == ".lvclass":
        # Same properties LabVIEW writes for a typedef member of a class (public scope)
        flags = 0x900000 | (STRICT_TYPEDEF_VI if strict else 0)
        item = (f'\t<Item Name="{name}" Type="VI" URL="{url}">\n'
                '\t\t<Property Name="NI.ClassItem.ExecutionSystem" Type="Int">-1</Property>\n'
                f'\t\t<Property Name="NI.ClassItem.Flags" Type="Int">{flags}</Property>\n'
                '\t\t<Property Name="NI.ClassItem.InvokeUsage" Type="UInt">9</Property>\n'
                '\t\t<Property Name="NI.ClassItem.MethodScope" Type="UInt">1</Property>\n'
                '\t\t<Property Name="NI.ClassItem.Priority" Type="Int">1</Property>\n'
                '\t\t<Property Name="NI.ClassItem.State" Type="Int">1082143234</Property>\n'
                '\t</Item>\n')
        close = "</LVClass>"
    else:
        item = f'\t<Item Name="{name}" Type="VI" URL="{url}"/>\n'
        close = "</Library>"
    i = text.rindex(close)
    owner_file.write_text(text[:i] + item + text[i:], encoding="utf-8", newline="")
    return True


def patch_lvsr(lvsr: bytes, strict: bool) -> bytes:
    """Fresh per-file identity (never reuse the skeleton's: LabVIEW's compiled-object cache and
    event registration key on these) and the typedef / strict typedef flags."""
    b = bytearray(lvsr)
    (flags,) = struct.unpack_from(">I", b, LVSR_EXEC_FLAGS)
    flags |= TYPEDEF_VI
    flags = flags | STRICT_TYPEDEF_VI if strict else flags & ~STRICT_TYPEDEF_VI
    struct.pack_into(">I", b, LVSR_EXEC_FLAGS, flags)
    b[LVSR_VI_SIGNATURE:LVSR_VI_SIGNATURE + 16] = uuid.uuid4().bytes
    b[LVSR_FIELD50:LVSR_FIELD50 + 16] = uuid.uuid4().bytes
    b[LVSR_FIELD78:LVSR_FIELD78 + 16] = bytes(16)
    return bytes(b)


def main():
    ap = argparse.ArgumentParser(description="Generate a LabVIEW cluster type definition (.ctl) from a JSON spec")
    ap.add_argument("spec", type=Path, help="JSON spec (see module docstring)")
    ap.add_argument("out", type=Path, help="output .ctl; its file name becomes the typedef name")
    ap.add_argument("--assets", type=Path, help="folder with skeleton.ctl and templates.json")
    ap.add_argument("--register", action="store_true",
                    help="also add the file to its owner (.lvlib/.lvclass); close it in LabVIEW first")
    args = ap.parse_args()
    if args.assets:
        set_assets(args.assets)
    for p in (SKELETON, TEMPLATES):
        if not p.exists():
            ap.error(f"missing asset {p} (build it with make_skeleton.py / harvest.py, or pass --assets)")
    spec = json.loads(args.spec.read_text())
    if spec.get("owner_file"):  # relative to the spec file
        spec["owner_file"] = str((args.spec.parent / spec["owner_file"]).resolve())
    for f in linked_fields(spec["fields"]):  # so are linked typedef paths
        f["path"] = str((args.spec.parent / f["path"]).resolve())
    out = args.out.resolve()
    generate(spec, out)
    print("wrote", out, os.path.getsize(out), "bytes")
    if args.register:
        if not spec.get("owner_file"):
            ap.error("--register needs 'owner_file' in the spec")
        added = register(out, Path(spec["owner_file"]), bool(spec.get("strict")))
        print(("added to " if added else "already in ") + spec["owner_file"])


if __name__ == "__main__":
    main()

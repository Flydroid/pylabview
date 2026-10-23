"""Linked typedefs: use an existing .ctl type definition as an element of a generated cluster.

What LabVIEW writes for an element that is an instance of another typedef (checked against
NI's IB_Tree_Source.ctl, which links <resource>/.../ItemRef.ctl):

VCTP   - the referenced typedef's TypeDef TD, copied as-is (its own label). Its top-level
         TypeID is what the LIvi link record refers to.
       - a second copy of that TypeDef whose nested type carries the element's label; this
         is the instance used inside the cluster.
       - an unlabeled copy of the underlying type, used by the control inside the wrapper.
FPHb   - a 'typeDef' wrapper object: label (element name), annex, the referenced control as
         part 8010 (its own label hidden), typeDesc = instance, stamp = the referenced
         typedef's stamp, kSLHDefaultValueMatchesCtlVI.
LIvi   - VICC link: qualified name + path of the referenced file, TypeID of the copied TD.
LIfp   - TDCC link: same, plus the uids of the wrapper objects and the path of the
         referenced typedef's owning library (empty if it has none).

Supported: typedefs whose underlying type is a scalar, enum, string, bool, path (one control,
no nested panes) and which do not themselves link further typedefs.
"""
from __future__ import annotations

import copy
import struct
from pathlib import Path

from . import heap
from . import links
from . import names
from . import rsrc
from . import vctp

T = names.tag_id
CLASS, UID = -2, -3
INNER_PART_ID = 8010  # part id of the referenced control inside a 'typeDef' wrapper


# --- TD helpers ---------------------------------------------------------------

def _pstr_list(b: bytes, p: int, n: int):
    out = []
    for _ in range(n):
        out.append(b[p + 1:p + 1 + b[p]].decode("mac_roman"))
        p += 1 + b[p]
    return out, p


def parse_typedef(td: vctp.TD):
    """-> (stamp, qualified name, nested TD)"""
    b = td.body
    stamp, n = struct.unpack_from(">II", b)
    qname, p = _pstr_list(b, 8, n)
    ln, flags, typ = struct.unpack_from(">HBB", b, p)
    nested = vctp.TD(typ, flags, b[p + 4:p + ln - 4])  # stored length is real length + 4
    return stamp, qname, nested


def build_typedef(stamp: int, qname: list[str], nested: vctp.TD) -> vctp.TD:
    body = struct.pack(">II", stamp, len(qname)) + b"".join(links.pstr(q) for q in qname)
    rec = nested.encode()
    return vctp.TD(vctp.TYPEDEF, 0, body + struct.pack(">H", len(rec) + 4) + rec[2:])


def core_len(td: vctp.TD) -> int:
    """Length of a TD body without its label."""
    b, t = td.body, td.type
    if t in vctp.NUMERIC:
        return 1
    if t in (vctp.UNIT_U8, vctp.UNIT_U16, vctp.UNIT_U32):  # enum: items, pad to even, 1 byte
        (n,) = struct.unpack_from(">H", b)
        p = 2
        for _ in range(n):
            p += 1 + b[p]
        return p + (p % 2) + 1
    if t in (vctp.STRING, vctp.PATH):
        return 4
    if t in (vctp.BOOL, vctp.VOID):
        return 0
    if t == vctp.CLUSTER:
        return 2 + 2 * struct.unpack_from(">H", b)[0]
    if t == vctp.ARRAY:
        return 2 + 4 * struct.unpack_from(">H", b)[0] + 2
    raise NotImplementedError(f"TD type {t:#x} in a linked typedef")


def relabel(td: vctp.TD, label: str | None) -> vctp.TD:
    core = td.body[:core_len(td)]
    if not label:
        return vctp.TD(td.type, td.flags & ~vctp.HAS_LABEL, core)
    lab = links.pstr(label)
    lab += b"\0" * (len(lab) % 2)
    return vctp.TD(td.type, td.flags | vctp.HAS_LABEL, core + lab)


def remap(td: vctp.TD, f) -> vctp.TD:
    """Copy of td with every flat-index reference r replaced by f(r)."""
    b, t = bytearray(td.body), td.type
    if t == vctp.CLUSTER:
        (n,) = struct.unpack_from(">H", b)
        for i in range(n):
            struct.pack_into(">H", b, 2 + 2 * i, f(struct.unpack_from(">H", b, 2 + 2 * i)[0]))
    elif t == vctp.ARRAY:
        p = 2 + 4 * struct.unpack_from(">H", b)[0]
        struct.pack_into(">H", b, p, f(struct.unpack_from(">H", b, p)[0]))
    elif t == vctp.REPEATED_BLOCK:
        struct.pack_into(">H", b, 4, f(struct.unpack_from(">H", b, 4)[0]))
    elif t == vctp.TYPEDEF:
        stamp, qname, nested = parse_typedef(td)
        return build_typedef(stamp, qname, remap(nested, f))
    elif t in (vctp.REFNUM, vctp.VARIANT) or t >= 0x60:
        raise NotImplementedError(f"TD type {t:#x} in a linked typedef")
    return vctp.TD(t, td.flags, bytes(b))


# --- the referenced file ---------------------------------------------------

def _find_path(data: bytes, start: int) -> list[str] | None:
    i = data.find(b"PTH0", start)
    if i < 0:
        return None
    ln, tp, n = struct.unpack_from(">IHH", data, i + 4)
    if ln == 0:
        return None
    comps, _ = _pstr_list(data, i + 12, n)
    return comps


def _resolve(base: Path, comps: list[str]) -> Path:
    """LabVIEW relative path (from a file) -> absolute path."""
    p = base
    for c in comps:
        p = p.parent if c == "" else p / c
    return p


class RefCtl:
    """An existing typedef .ctl referenced by a generated one."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.r = rsrc.read(self.path.read_bytes())
        self.v = vctp.parse(vctp.unpack_section(self.r.section_data(b"VCTP")))
        (conp,) = struct.unpack(">H", self.r.section_data(b"CONP"))
        self.td_flat = self.v.top[conp - 1]
        td = self.v.tds[self.td_flat]
        if td.type != vctp.TYPEDEF:
            raise ValueError(f"{self.path.name} is not a type definition")
        self.stamp, self.qname, self.nested = parse_typedef(td)

        livi = self.r.section_data(b"LIvi")
        if b"VICC" in livi:
            raise NotImplementedError(f"{self.path.name} links further typedefs (not supported yet)")
        i = livi.find(b"VILB")
        self.owner_file = _resolve(self.path, _find_path(livi, i)) if i >= 0 else None

        (root,) = heap.load(self.r.section_data(b"FPHb"))
        supc = root.find(T("root"))
        (fpdco,) = supc.find(T("paneHierarchy")).find(T("zPlaneList")).children
        self.ddo = fpdco.find(T("ddo"))
        self.dthp = struct.unpack(">HH", self.r.section_data(b"DTHP"))
        typedescs = [n for n in _walk(self.ddo) if n.tag == T("typeDesc")]
        if len(typedescs) != 1:
            raise NotImplementedError(f"{self.path.name}: typedefs with nested controls are not supported yet")

    def heap_flat(self, hid: int) -> int:
        _, shift = self.dthp
        return self.v.top[shift + hid - 2]


def _walk(n):
    yield n
    for c in n.children or []:
        yield from _walk(c)


class Importer:
    """Copies TDs from referenced files into the VCTP being generated (once per source TD)."""

    def __init__(self, v: vctp.Vctp):
        self.v = v
        self.memo: dict[tuple[Path, int], int] = {}

    def td(self, ref: RefCtl, flat: int) -> int:
        key = (ref.path, flat)
        if key not in self.memo:
            new = remap(ref.v.tds[flat], lambda f: self.td(ref, f))
            self.memo[key] = self.v.add(new)
        return self.memo[key]


# --- generator hooks ----------------------------------------------------------

def add_vctp(v: vctp.Vctp, imp: Importer, f: dict) -> int:
    """TDs for a linked-typedef field. Sets f['_flat'] (instance), f['_inner_flat'],
    f['_link_flat'] (the as-is copy the VICC link refers to)."""
    ref: RefCtl = f["_ref"]
    f["_link_flat"] = imp.td(ref, ref.td_flat)
    stamp, qname, nested = parse_typedef(v.tds[f["_link_flat"]])
    f["_flat"] = v.add(build_typedef(stamp, qname, relabel(nested, f["label"])))
    inner_src = ref.heap_flat(int.from_bytes(next(
        n for n in _walk(ref.ddo) if n.tag == T("typeDesc")).content, "big", signed=True))
    f["_inner_flat"] = v.add(relabel(v.tds[imp.td(ref, inner_src)], None))
    return f["_flat"]


def make_wrapper(f: dict, load_template, set_label, set_int, set_rect, parts_extent, hide_part, part,
                 font_table=None):
    """The 'typeDef' wrapper object holding a copy of the referenced control."""
    ref: RefCtl = f["_ref"]
    w = load_template("typeDef:f1", rebase=False)
    plist = w.find(T("partsList"))
    plist.children = [p for p in plist.children if not _is_part(p, INNER_PART_ID)]
    label_right = set_label(w, f["label"])

    inner = copy.deepcopy(ref.ddo)
    inner.tag = -6  # arrayElement, as a member of the wrapper's partsList
    if font_table is not None:  # the control comes with the referenced file's font ids
        font_table.remap(inner, ref.r.section_data(b"FTAB", 128), set_int)
    pid = heap.Node(T("partID"))
    set_int(pid, INNER_PART_ID)
    inner.children.insert(1, pid)
    hide_part(inner, 16)
    set_int(inner.find(T("typeDesc")), f["_inner_hid"])
    h, wd = parts_extent(inner)
    set_rect(inner.find(T("bounds")), (19, 0, 19 + h, wd))
    plist.children.append(inner)
    plist.attrs = [(k, len(plist.children) if k == -5 else v) for k, v in plist.attrs]

    set_int(w.find(T("typeDesc")), f["_hid"])
    st = w.find(T("stamp"))
    if st is not None:
        st.content = struct.pack(">i", ref.stamp - (1 << 32) if ref.stamp >= 1 << 31 else ref.stamp)
    w._link_ref = ref
    hh, ww = parts_extent(w)
    return w, hh, max(ww, label_right)


def _is_part(p, pid) -> bool:
    n = p.find(T("partID"))
    return n is not None and int.from_bytes(n.content, "big", signed=True) == pid


def link_entries(out: Path, refs: list[tuple[RefCtl, int]], wrappers: dict[Path, list[int]]):
    """(LIvi entries, LIfp entries) for the referenced typedefs. refs: (ref, TypeID of its link TD)."""
    vi, fp = [], []
    for ref, type_id in refs:
        path = links.relative_components(out, ref.path)
        vi.append(links.vicc(ref.qname, path, type_id))
        lib = links.relative_components(out, ref.owner_file) if ref.owner_file else None
        fp.append(links.tdcc(ref.qname, path, wrappers.get(ref.path, []), lib))
    return vi, fp

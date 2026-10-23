# -*- coding: utf-8 -*-

""" Tests for pylabview.ctlgen (byte-level RSRC/heap/VCTP codecs and the .ctl generator).

    Run using `pytest` in project root folder.

    Layers, from always-available to opt-in:
      - codec unit tests                         always
      - codec round-trip on ./examples           always (whatever RSRC files are there)
      - codec round-trip on a corpus             CTLGEN_CORPUS=<folder>  (e.g. a LabVIEW vi.lib)
      - link encoders vs NI files                if LabVIEW 2025 is installed
      - generator                                if ctlgen assets exist (see ctlgen/README.md)
      - validation in LabVIEW                    CTLGEN_LABVIEW=1 (Windows, LabVIEW + LabVIEWCLI)
"""

import glob
import itertools
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from pylabview.ctlgen import corpus_check, heap, links, rsrc, vctp

ROOT = Path(__file__).resolve().parent.parent
SPECS = Path(__file__).resolve().parent / "ctlgen_specs"
LV2025 = Path(r"C:\Program Files\National Instruments\LabVIEW 2025")
ASSETS = Path(os.environ.get("CTLGEN_ASSETS", ROOT / "pylabview" / "ctlgen" / "assets"))
HAVE_ASSETS = (ASSETS / "skeleton.ctl").exists() and (ASSETS / "templates.json").exists()


# --- codec units --------------------------------------------------------------

@pytest.mark.parametrize("v", [0, 1, 126, 127, -1, -126, -127, -128, 200, -200, 0x7FFF, -0x8000, 0x8000, -0x8001,
                               0x12345678, -0x12345678])
def test_varint_s124_roundtrip(v):
    from io import BytesIO
    assert heap.rd_s124(BytesIO(heap.wr_s124(v))) == v


@pytest.mark.parametrize("v", [0, 1, 0xFD, 0xFE, 0xFF, 0xFFFE, 0xFFFF, 0x10000, 0x7FFFFFFF])
def test_varint_u124_roundtrip(v):
    from io import BytesIO
    assert heap.rd_u124(BytesIO(heap.wr_u124(v))) == v


def test_heap_tree_roundtrip():
    tree = [heap.Node(-7, [(-2, 5), (-3, 1)], None, [
        heap.Node(14, [], struct.pack(">4h", 1, 2, 3, 4)),
        heap.Node(3, [], b"a longer string content"),
        heap.Node(54, [(-5, 2)], None, [heap.Node(-6, [(-3, 300)]), heap.Node(-6, [(-3, 70000)])]),
        heap.Node(615, [], True),
        heap.Node(2000, [], b"\x01"),  # tag beyond the 10-bit range
    ])]
    raw = heap.emit_nodes(tree)
    assert heap.emit_nodes(heap.parse_nodes(raw)) == raw
    assert heap.unpack_section(heap.dump(tree)) == raw


def test_vctp_roundtrip():
    v = vctp.Vctp()
    a = v.add(vctp.TD(vctp.DBL, vctp.HAS_LABEL, b"\x00\x04Rate\x00"))
    c = v.add(vctp.TD(vctp.CLUSTER, 0, struct.pack(">HH", 1, a)))
    v.add_top(c)
    v.add_top(a)
    raw = vctp.emit(v)
    assert vctp.emit(vctp.parse(raw)) == raw


def test_relative_components():
    assert links.relative_components(r"C:\a\b\x.ctl", r"C:\a\b\L.lvlib") == ["", "L.lvlib"]
    assert links.relative_components(r"C:\a\b\c\x.ctl", r"C:\a\L.lvlib") == ["", "", "", "L.lvlib"]


# --- codec round-trips on real files ---------------------------------------------

EXAMPLES = sorted(itertools.chain.from_iterable(
    glob.glob(str(ROOT / "examples" / "**" / f"*.{e}"), recursive=True) for e in ("vi", "ctl", "vit", "ctt")))


@pytest.mark.parametrize("path", EXAMPLES)
def test_codecs_roundtrip_examples(path):
    assert corpus_check.check(Path(path).read_bytes()) == []


@pytest.mark.skipif(not os.environ.get("CTLGEN_CORPUS"), reason="set CTLGEN_CORPUS to a folder of RSRC files")
def test_codecs_roundtrip_corpus():
    files = [p for e in ("ctl", "vi") for p in Path(os.environ["CTLGEN_CORPUS"]).rglob(f"*.{e}")]
    bad = [(p, corpus_check.check(p.read_bytes())) for p in files]
    bad = [(p, b) for p, b in bad if b]
    assert not bad, f"{len(bad)} of {len(files)} files did not round-trip, e.g. {bad[:5]}"


# --- link records vs files LabVIEW wrote ------------------------------------------

def _section(path, ident):
    return rsrc.read(Path(path).read_bytes()).section_data(ident)


@pytest.mark.skipif(not LV2025.exists(), reason="needs LabVIEW 2025's vi.lib")
def test_links_match_labview():
    attr = LV2025 / "vi.lib" / "InstallerBuilder" / "XML Parser" / "Attribute.ctl"
    assert links.libn(["xml.lvlib"]) == _section(attr, b"LIBN")
    vilb_abs = (b"\x00\x02VILB" + bytes(4) + links.pth0(["<vilib>", "InstallerBuilder", "XML Parser", "xml.lvlib"],
                                                        relative=False) + struct.pack(">I", 2))
    assert links.link_table(b"LVCC", [vilb_abs]) == _section(attr, b"LIvi")

    src = LV2025 / "vi.lib" / "AppBuilder" / "IB_Classes" / "Tree_Source" / "IB_Tree_Source.ctl"
    path = ["<resource>", "Framework", "Providers", "API", "ItemRef.ctl"]
    assert links.link_table(b"LVCC", [links.vicc(["ItemRef.ctl"], path, 1, relative=False)]) == _section(src, b"LIvi")
    assert links.link_table(b"FPHP", [links.tdcc(["ItemRef.ctl"], path, [75], None, relative=False)]) == \
        _section(src, b"LIfp")


# --- generator ------------------------------------------------------------------------

SPEC_FILES = sorted(SPECS.glob("*.json"))


def _generate(spec_file, out_dir):
    from pylabview.ctlgen import gen
    gen.set_assets(ASSETS)
    spec = json.loads(spec_file.read_text())
    for f in gen.linked_fields(spec["fields"]):
        f["path"] = str((spec_file.parent / f["path"]).resolve())
        if not Path(f["path"]).exists():
            pytest.skip(f"referenced typedef not available: {f['path']}")
    out = out_dir / spec.get("file", spec_file.stem + "--cluster.ctl")
    gen.generate(spec, out)
    return out, spec


def _walk(n):
    yield n
    for c in n.children or []:
        yield from _walk(c)


@pytest.mark.skipif(not HAVE_ASSETS, reason="ctlgen assets not built (see pylabview/ctlgen/README.md)")
@pytest.mark.parametrize("spec_file", SPEC_FILES, ids=[p.stem for p in SPEC_FILES])
def test_generate_structure(spec_file, tmp_path):
    out, spec = _generate(spec_file, tmp_path)
    buf = out.read_bytes()
    assert corpus_check.check(buf) == []           # our own codecs read back what we wrote
    r = rsrc.read(buf)
    v = vctp.parse(vctp.unpack_section(r.section_data(b"VCTP")))
    (conp,) = struct.unpack(">H", r.section_data(b"CONP"))
    assert v.tds[v.top[conp - 1]].type == vctp.TYPEDEF
    cnt, shift = struct.unpack(">HH", r.section_data(b"DTHP"))
    assert shift + cnt - 1 == len(v.top)
    (root,) = heap.load(r.section_data(b"FPHb"))
    objs = [dict(n.attrs) for n in _walk(root) if -2 in dict(n.attrs) and -3 in dict(n.attrs)]
    uids = [a[-3] for a in objs]
    assert len(uids) == len(set(uids)), "duplicate uids"
    assert struct.unpack(">I", r.section_data(b"MUID"))[0] == max(uids)
    from pylabview.ctlgen import names

    def typedescs(n, cls=None):                  # tag ids are class-relative: resolve by name
        c = dict(n.attrs).get(-2, cls)
        for ch in n.children or []:
            if names.tag_name(ch.tag, c) == "typeDesc" and isinstance(ch.content, bytes):
                yield int.from_bytes(ch.content, "big", signed=True)
            yield from typedescs(ch, c)
    hids = list(typedescs(root))
    assert hids and all(1 <= h <= cnt for h in hids), "heap type id outside DTHP range"


# --- validation in LabVIEW ----------------------------------------------------------------

@pytest.mark.skipif(not (os.environ.get("CTLGEN_LABVIEW") and HAVE_ASSETS and sys.platform == "win32"),
                    reason="set CTLGEN_LABVIEW=1 to validate in an isolated LabVIEW instance")
@pytest.mark.parametrize("spec_file", SPEC_FILES, ids=[p.stem for p in SPEC_FILES])
def test_generate_loads_in_labview(spec_file, tmp_path):
    out, _ = _generate(spec_file, tmp_path)
    work = Path(os.environ.get("CTLGEN_LV_WORK", tmp_path.parent / "ctlgen-lv"))
    tools = ROOT / "pylabview" / "ctlgen" / "tools"
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(tools / "lvcheck.ps1"), "-Dir", str(out.parent), "-Work", str(work), "-Fresh"],
                         capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, res.stdout + res.stderr

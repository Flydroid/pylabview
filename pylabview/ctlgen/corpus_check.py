"""Round-trip every RSRC file under a root through the ctlgen codecs and report mismatches.

usage: python -m pylabview.ctlgen.corpus_check <root> [ext ...]      (default ext: ctl)

Checks, per file:
  rsrc  - container read+write is byte-identical
  heap  - FPHb/BDHb node stream parse+emit is byte-identical (decompressed)
  vctp  - VCTP type list parse+emit is byte-identical (decompressed)
"""
import collections
import sys
from pathlib import Path

from . import heap
from . import rsrc
from . import vctp


def check(buf: bytes) -> list:
    """Return the list of layers that did NOT round-trip (empty = all good)."""
    bad = []
    r = rsrc.read(buf)
    if rsrc.write(r) != buf:
        bad.append("rsrc")
    for ident in (b"FPHb", b"BDHb"):
        if r.block(ident) is not None:
            s = heap.unpack_section(r.section_data(ident))
            if heap.emit_nodes(heap.parse_nodes(s)) != s:
                bad.append(ident.decode())
    if r.block(b"VCTP") is not None:
        raw = vctp.unpack_section(r.section_data(b"VCTP"))
        if vctp.emit(vctp.parse(raw)) != raw:
            bad.append("VCTP")
    return bad


def main():
    root = Path(sys.argv[1])
    exts = sys.argv[2:] or ["ctl"]
    files = sorted(p for e in exts for p in root.rglob(f"*.{e}"))
    stats = collections.Counter()
    fails = []
    for p in files:
        try:
            bad = check(p.read_bytes())
        except Exception as e:  # noqa: BLE001
            bad = [f"{type(e).__name__}: {e}"]
        stats["ok" if not bad else "failed"] += 1
        if bad:
            fails.append((p, bad))
    print(f"{len(files)} files: {stats['ok']} ok, {stats['failed']} failed")
    kinds = collections.Counter(b if ":" not in b else b.split(":")[0] for _, bs in fails for b in bs)
    for k, c in kinds.most_common():
        print(f"  {c:5}  {k}")
    for p, bad in fails[:20]:
        print(f"  {p.relative_to(root)}  {bad}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

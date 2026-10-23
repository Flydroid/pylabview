"""Decode + re-encode every codec layer (forces fresh zlib streams). usage: reencode.py in.ctl out.ctl"""
import sys
from pathlib import Path

from . import heap
from . import rsrc
from . import vctp

r = rsrc.read(Path(sys.argv[1]).read_bytes())
for b in r.blocks:
    for s in b.sections:
        if b.ident in (b"FPHb", b"BDHb"):
            s.data = heap.dump(heap.load(s.data))
        elif b.ident == b"VCTP":
            s.data = vctp.pack_section(vctp.emit(vctp.parse(vctp.unpack_section(s.data))))
Path(sys.argv[2]).write_bytes(rsrc.write(r))

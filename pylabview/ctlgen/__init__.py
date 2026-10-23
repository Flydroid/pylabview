"""ctlgen: write LabVIEW .ctl type definitions from a JSON description.

Byte-level codecs for the parts of an RSRC file a type definition is made of:

- rsrc:  the RSRC container (headers, block/section tables, name table)
- heap:  FPHb/BDHb object heaps (generic tag tree)
- vctp:  VCTP consolidated type descriptors
- gen:   the JSON -> .ctl generator built on top of them

All three codecs reproduce LabVIEW-saved files byte for byte (see corpus_check.py).
See README.md in this folder for the format notes and the validation workflow.
"""

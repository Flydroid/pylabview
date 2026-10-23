# ctlgen — write LabVIEW `.ctl` type definitions

`ctlgen` generates a LabVIEW cluster type definition (`.ctl`) from a JSON description,
writing the RSRC file directly (no LabVIEW needed to produce it). It is built on three
byte-level codecs that reproduce LabVIEW-saved files exactly:

| module  | what                                                  | verified                                        |
|---------|-------------------------------------------------------|-------------------------------------------------|
| `rsrc`  | RSRC container (headers, block/section tables, names) | byte-identical on 26 149 `.vi`/`.ctl` of LabVIEW 2025 `vi.lib` |
| `heap`  | `FPHb`/`BDHb` object heaps as a generic tag tree      | same                                            |
| `vctp`  | `VCTP` type descriptors + top-level type list         | same                                            |
| `links` | `LIBN`, `LIvi`/`LIfp` link tables (`VILB`, `VICC`, `TDCC`) | byte-identical to NI and user files         |

Target: **LabVIEW 2025 (25.x)**. Requires Python 3.7+ (dataclasses, `from __future__ import annotations`).

## Usage

```
python -m pylabview.ctlgen.gen [--assets DIR] [--register] spec.json "Out Name--cluster.ctl"
```

```json
{
  "label": "Status",
  "strict": false,
  "owners": ["My.lvlib", "My.lvclass"], "owner_file": "../My_lvclass/My.lvclass",
  "fields": [
    {"label": "rate",  "type": "DBL"},
    {"label": "state", "type": "Enum", "rep": "U16", "values": ["IDLE", "RUNNING"]},
    {"label": "mode",  "type": "TypeDef", "path": "Mode--enum.ctl"},
    {"label": "data",  "type": "Array", "element": {"type": "DBL"}},
    {"label": "sub",   "type": "Cluster", "fields": [{"label": "ok", "type": "Bool"}]}
  ]
}
```

* scalar types: `I8 I16 I32 I64 U8 U16 U32 U64 SGL DBL EXT String Bool Path`
* `Enum` (`rep` U8/U16/U32), `Cluster` (nested), `Array` (1-D, element: scalar/enum)
* `TypeDef`: element is an instance of an existing typedef `.ctl` (linked, not copied)
* `strict: true` makes it a strict type definition
* `owners` / `owner_file`: library/class membership; `--register` also adds the `<Item>`
  to the owner `.lvlib`/`.lvclass` (close it in LabVIEW first)
* paths in the spec are relative to the spec file
* every cluster is created with *Autosizing → Arrange Vertically*

Not supported yet: arrays of clusters/arrays, linked typedefs whose type is a cluster (or
that link further typedefs), default values, class membership is implemented but untested.

## Assets

The generator copies everything that is not type-specific (version, icon, fonts, history,
empty block diagram) from a LabVIEW-saved **skeleton**, and instantiates front-panel objects
from **templates** harvested out of LabVIEW-saved controls. Both are derived from other
people's files and are therefore not versioned (`assets/` is gitignored). Build them from
your LabVIEW installation:

```
python -m pylabview.ctlgen.make_skeleton "<LabVIEW 2025>/vi.lib/AppBuilder/IB_Classes/Tree_Dest/destEdits_remove.ctl" pylabview/ctlgen/assets/skeleton.ctl
python -m pylabview.ctlgen.harvest "<LabVIEW 2025>" pylabview/ctlgen/assets/skeleton.ctl pylabview/ctlgen/assets/templates.json
```

The skeleton must be a standalone, non-strict cluster typedef saved by the target LabVIEW
version; templates are only taken from files saved by that version (XNode files are skipped).
`override_template.py` replaces one template with a control of your choice (e.g. your
preferred checkbox style).

## Validation workflow

A clean load is not enough — front-panel "insanities" are only written to LabVIEW's internal
log. The loop that found every bug so far:

1. **Codecs**: `python -m pylabview.ctlgen.corpus_check <folder> ctl vi` — byte-exact round trip.
2. **Structure**: `pytest tests/test_ctlgen.py` (unique uids, MUID, heap type ids within DTHP…).
3. **LabVIEW**: `CTLGEN_LABVIEW=1 pytest tests/test_ctlgen.py -k labview` or
   `tools/lvcheck.ps1 -Dir <folder> -Work <workdir> -Fresh`. Runs a mass compile in an
   isolated LabVIEW instance (`tools/lvinstance.ps1`: own ini with `AllowMultipleInstances`,
   `DefaultDataFileLocation`, `CompiledObjCacheRoot`, VI server port 3683) and scans its
   `lvlog*.txt` for `DWarn`/`DAbort`. It never touches a LabVIEW you have open; `-Fresh`
   restarts it so files with equal names from earlier runs cannot interfere.
4. **Re-save diff**: open the generated file in LabVIEW, save it, then
   `python -m pylabview.ctlgen.resave_diff generated.ctl saved.ctl`. LabVIEW's routine save
   changes are reported as expected; anything else is a difference to fix in the generator.
5. **Negative controls** for links: move the linked/owner file away and expect LabVIEW's
   "Search failed to find …" — proves the link is actually followed.

## Format notes

Things not (or not correctly) documented elsewhere, all confirmed against LabVIEW 2025:

**VCTP**
* A TypeDef's nested TD record stores its length as *real length + 4*.
* Labels are pascal strings padded to even length.
* Minimal top-level list LabVIEW accepts: `TypeDef, RepeatedBlock(792×U8), RepeatedBlock(51×I32),
  Void`, then the heap types. LabVIEW adds its data-space types (`udf`/`txd`, FP DCO table) on save.
* `DTHP` = (number of heap types, top-level index of heap type 1); heap `typeDesc` values are
  1-based within that slice, assigned in heap write order (an array: index display, array,
  element; a typedef instance: inner control, wrapper).
* Strict typedef: `LVSR` exec flags bit 21 (bit 20 = typedef), and the TypeDef TD's first
  u32 is a timestamp (seconds since 1904); 0 for plain typedefs (1319/1319 vs 2257/2257 NI files).

**LVSR**: `viSignature` (@52) and the 16 bytes @80 are unique per file and must not be copied
between files; @120 is zero in ~43 % of NI files.

**Front-panel heap**
* Heap tag ids are class-relative (e.g. 283 is `typeDesc` only in some classes).
* Cluster `objFlags` bit `0x2000000` = Autosizing: Arrange Vertically.
* Part `objFlags` bit `0x8` = hidden part. LabVIEW sizes an object without parts it hid,
  but still counts an enum's hidden digital display.
* Pane parts are relative to the pane: content `(0,0,H,W)`, scrollbars `(0,W,H,W+17)` and
  `(H,0,H+17,W)`, corner `(H,W,H+17,W+17)`.
* Elements of a **nested** cluster are stored relative to the top-level cluster pane frame:
  `element = (-47, -30) − (nested cluster top + 21, left + 4)` (one level verified).
* `ddoList` entries refer to element uids; every object needs a distinct uid, `MUID` = max uid.
* A typedef instance is a `typeDef` wrapper (label, annex, inner control as part 8010 with a
  hidden label, `stamp` = referenced typedef's stamp); `TDCC` link "offsets" are wrapper **uids**.

**Links**: tables are `u16 1, kind, u32 count, {u16 2, ident, body}*, u16 3`. Relative
`PTH0` paths start from the referencing *file*: each `""` goes up one level. An empty path in
`TDCC` is `PTH0` + length 0 + 4 zero bytes. `.lvclass` items carry six `NI.ClassItem.*`
properties; `.lvlib` items are a single `<Item>`.

**pylabview**: `LVmisc.prepareVariableSizeFieldS124` writes `0x80`+4 bytes but the reader
(and LabVIEW) use `0x80`+2 / `0x81`+4.

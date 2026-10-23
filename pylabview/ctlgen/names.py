"""Heap tag / class / attribute name <-> id, borrowed from pylabview's enums.

Only a convenience for inspection and for writing the generator readably; the
generator itself only needs the numeric ids it emits.
"""
import pylabview.LVrsrcontainer  # noqa: F401  (import order avoids pylabview's circular import)
from pylabview import LVheap as H

SYS_ATTR = {"class": -2, "uid": -3, "stockObj": -4, "elements": -5, "index": -6, "stockSource": -7}
SYS_ATTR_NAME = {v: k for k, v in SYS_ATTR.items()}


def class_id(name: str) -> int:
    return H.SL_CLASS_TAGS["SL__" + name].value


def class_name(cid: int) -> str:
    return H.SL_CLASS_TAGS(cid).name[4:] if H.SL_CLASS_TAGS.has_value(cid) else f"Class{cid}"


def _tag_lists(class_ctx):
    lists = []
    if class_ctx is not None and H.SL_CLASS_TAGS.has_value(class_ctx):
        en = H.SL_CLASS_TAGS(class_ctx)
        if en in H.CLASS_EN_TO_TAG_LIST_MAPPING:
            lists.append(H.CLASS_EN_TO_TAG_LIST_MAPPING[en])
    lists.append(H.OBJ_FIELD_TAGS)
    return lists


def tag_id(name: str, class_ctx: int | None = None) -> int:
    if H.SL_SYSTEM_TAGS.has_name("SL__" + name):
        return H.SL_SYSTEM_TAGS["SL__" + name].value
    for lst in _tag_lists(class_ctx):
        prefix = next(iter(lst.__members__))[:4]
        if lst.has_name(prefix + name):
            return lst[prefix + name].value
    raise KeyError(f"unknown tag {name!r} (class ctx {class_ctx})")


def tag_name(tid: int, class_ctx: int | None = None) -> str:
    if H.SL_SYSTEM_TAGS.has_value(tid):
        return H.SL_SYSTEM_TAGS(tid).name[4:]
    for lst in _tag_lists(class_ctx):
        if lst.has_value(tid):
            return lst(tid).name[4:]
    return f"Tag{tid}"

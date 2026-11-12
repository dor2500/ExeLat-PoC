"""
Delphi / C++Builder form resources (binary DFM).

Programs built with Delphi do not use Win32 dialog templates. Each form is an
RT_RCDATA resource that starts with the signature "TPF0" and holds a tree of
components with their published properties:

    object  := [prefix] ClassName ObjectName property* 0x00 object* 0x00
    property:= Name value
    value   := a type byte followed by type-specific data (see _read_value)

parse_form() / build_form() are lossless: building an unmodified parse gives
back the original bytes. The patcher checks that before changing a form.

Translation replaces string values of user-visible properties (Caption, Hint,
...) with wide strings, which every Delphi version from 6 on reads into both
ANSI and Unicode string properties.
"""

from __future__ import annotations

import struct

from pe_resources import ResourceFormatError

SIGNATURE = b"TPF0"

# TValueType
(VA_NULL, VA_LIST, VA_INT8, VA_INT16, VA_INT32, VA_EXTENDED, VA_STRING, VA_IDENT, VA_FALSE, VA_TRUE,
 VA_BINARY, VA_SET, VA_LSTRING, VA_NIL, VA_COLLECTION, VA_SINGLE, VA_CURRENCY, VA_DATE, VA_WSTRING,
 VA_INT64, VA_UTF8STRING, VA_DOUBLE) = range(22)

_FIXED_SIZE = {VA_INT8: 1, VA_INT16: 2, VA_INT32: 4, VA_EXTENDED: 10, VA_SINGLE: 4,
               VA_CURRENCY: 8, VA_DATE: 8, VA_INT64: 8, VA_DOUBLE: 8}
_STRING_TYPES = (VA_STRING, VA_LSTRING, VA_WSTRING, VA_UTF8STRING)

# Properties whose text the user sees. Matched on the last part of the name,
# so "EditLabel.Caption" counts as "Caption".
TEXT_PROPERTIES = {"Caption", "Hint", "Title", "TextHint", "DisplayLabel", "SimpleText"}
# String lists shown to the user: combo/list items, tab names, radio group items.
LIST_PROPERTIES = {"Items.Strings", "Tabs.Strings", "Pages.Strings", "Sections.Strings"}

ANSI_CODEPAGE = "cp1251"      # best guess for non-ASCII bytes in pre-Unicode Delphi programs


class _Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data, self.offset = data, offset

    def raw(self, count: int) -> bytes:
        if count < 0 or self.offset + count > len(self.data):
            raise ResourceFormatError("read past end of form")
        chunk = self.data[self.offset: self.offset + count]
        self.offset += count
        return chunk

    def u8(self) -> int:
        return self.raw(1)[0]

    def peek(self) -> int:
        if self.offset >= len(self.data):
            raise ResourceFormatError("read past end of form")
        return self.data[self.offset]

    def i32(self) -> int:
        return struct.unpack("<i", self.raw(4))[0]

    def short_string(self) -> bytes:
        return self.raw(self.u8())


def _read_value(r: _Reader, depth: int = 0):
    """Read one value. Returns (type, payload); payload keeps raw bytes so the
    value can be written back exactly."""
    if depth > 64:
        raise ResourceFormatError("form nesting too deep")
    kind = r.u8()
    if kind in (VA_NULL, VA_FALSE, VA_TRUE, VA_NIL):
        return (kind, None)
    if kind in _FIXED_SIZE:
        return (kind, r.raw(_FIXED_SIZE[kind]))
    if kind in (VA_STRING, VA_IDENT):
        return (kind, r.short_string())
    if kind in (VA_LSTRING, VA_UTF8STRING, VA_BINARY):
        return (kind, r.raw(r.i32()))
    if kind == VA_WSTRING:
        return (kind, r.raw(r.i32() * 2))
    if kind == VA_SET:
        names = []
        while True:
            name = r.short_string()
            if not name:
                return (kind, names)
            names.append(name)
    if kind == VA_LIST:
        values = []
        while r.peek() != VA_NULL:
            values.append(_read_value(r, depth + 1))
        r.u8()
        return (kind, values)
    if kind == VA_COLLECTION:
        items = []
        while r.peek() != VA_NULL:
            order = None
            if r.peek() in (VA_INT8, VA_INT16, VA_INT32):
                order = _read_value(r, depth + 1)
            if r.u8() != VA_LIST:
                raise ResourceFormatError("collection item does not start with a list")
            items.append({"order": order, "props": _read_properties(r, depth + 1)})
        r.u8()
        return (kind, items)
    raise ResourceFormatError(f"unknown value type {kind}")


def _read_properties(r: _Reader, depth: int) -> list:
    props = []
    while True:
        name = r.short_string()
        if not name:
            return props
        props.append([name.decode("latin-1"), _read_value(r, depth)])


def _read_object(r: _Reader, depth: int = 0) -> dict:
    if depth > 64:
        raise ResourceFormatError("form nesting too deep")
    obj: dict = {"flags": None, "position": None}
    if r.peek() & 0xF0 == 0xF0:                     # prefix: inherited / inline / child position
        obj["flags"] = r.u8()
        if obj["flags"] & 0x02:
            obj["position"] = _read_value(r, depth)
    obj["class"] = r.short_string()
    obj["name"] = r.short_string()
    obj["props"] = _read_properties(r, depth)
    obj["children"] = []
    while r.peek() != 0:
        obj["children"].append(_read_object(r, depth + 1))
    r.u8()
    return obj


def is_form(blob: bytes) -> bool:
    return blob[:4] == SIGNATURE


def parse_form(blob: bytes) -> dict:
    if not is_form(blob):
        raise ResourceFormatError("not a Delphi form")
    reader = _Reader(blob, 4)
    root = _read_object(reader)
    root["trailing"] = blob[reader.offset:]
    return root


def _write_value(out: bytearray, value) -> None:
    kind, payload = value
    out.append(kind)
    if kind in (VA_NULL, VA_FALSE, VA_TRUE, VA_NIL):
        return
    if kind in _FIXED_SIZE:
        out += payload
    elif kind in (VA_STRING, VA_IDENT):
        out.append(len(payload)); out += payload
    elif kind in (VA_LSTRING, VA_UTF8STRING, VA_BINARY):
        out += struct.pack("<i", len(payload)) + payload
    elif kind == VA_WSTRING:
        out += struct.pack("<i", len(payload) // 2) + payload
    elif kind == VA_SET:
        for name in payload:
            out.append(len(name)); out += name
        out.append(0)
    elif kind == VA_LIST:
        for item in payload:
            _write_value(out, item)
        out.append(VA_NULL)
    elif kind == VA_COLLECTION:
        for item in payload:
            if item["order"] is not None:
                _write_value(out, item["order"])
            out.append(VA_LIST)
            _write_properties(out, item["props"])
        out.append(VA_NULL)


def _write_properties(out: bytearray, props: list) -> None:
    for name, value in props:
        encoded = name.encode("latin-1")
        out.append(len(encoded)); out += encoded
        _write_value(out, value)
    out.append(0)


def _write_object(out: bytearray, obj: dict) -> None:
    if obj["flags"] is not None:
        out.append(obj["flags"])
        if obj["position"] is not None:
            _write_value(out, obj["position"])
    out.append(len(obj["class"])); out += obj["class"]
    out.append(len(obj["name"])); out += obj["name"]
    _write_properties(out, obj["props"])
    for child in obj["children"]:
        _write_object(out, child)
    out.append(0)


def build_form(root: dict) -> bytes:
    out = bytearray(SIGNATURE)
    _write_object(out, root)
    return bytes(out) + root.get("trailing", b"")


# ---------------------------------------------------------------------------
# Text access
# ---------------------------------------------------------------------------
def decode_string(value) -> str | None:
    kind, payload = value
    if kind == VA_WSTRING:
        return payload.decode("utf-16-le", errors="replace")
    if kind == VA_UTF8STRING:
        return payload.decode("utf-8", errors="replace")
    if kind in (VA_STRING, VA_LSTRING):
        try:
            return payload.decode("ascii")
        except UnicodeDecodeError:
            return payload.decode(ANSI_CODEPAGE, errors="replace")
    return None


def encode_string(text: str):
    return (VA_WSTRING, text.encode("utf-16-le"))


def text_slots(root: dict):
    """Yield (container, index) for every user-visible string in the form.
    `container[index]` is a (type, payload) value that can be read with
    decode_string() and replaced with encode_string()."""
    def from_props(props):
        for prop in props:
            name, value = prop
            kind, payload = value
            leaf = name.rsplit(".", 1)[-1]
            if kind in _STRING_TYPES and leaf in TEXT_PROPERTIES:
                yield prop, 1
            elif kind == VA_LIST and any(name.endswith(suffix) for suffix in LIST_PROPERTIES):
                for index, item in enumerate(payload):
                    if item[0] in _STRING_TYPES:
                        yield payload, index
            elif kind == VA_COLLECTION:
                for item in payload:
                    yield from from_props(item["props"])

    def from_object(obj):
        yield from from_props(obj["props"])
        for child in obj["children"]:
            yield from from_object(child)

    yield from from_object(root)


def form_texts(root: dict) -> list[str]:
    return [text for container, index in text_slots(root) if (text := decode_string(container[index]))]


def set_right_to_left(root: dict) -> bool:
    """Give a form BiDiMode = bdRightToLeft, which its controls inherit.
    Only applied to roots that look like a visual form: data modules have no
    such property, and setting one a class lacks stops the program loading."""
    names = {name for name, _ in root["props"]}
    if not names & {"TextHeight", "ClientWidth", "ClientHeight"}:
        return False
    value = (VA_IDENT, b"bdRightToLeft")
    for prop in root["props"]:
        if prop[0] == "BiDiMode":
            if prop[1] == value:
                return False
            prop[1] = value
            return True
    root["props"].insert(0, ["BiDiMode", value])
    return True


def form_view(root: dict) -> dict:
    """Small JSON-friendly summary for the app."""
    def count(obj):
        return 1 + sum(count(child) for child in obj["children"])
    caption = next((decode_string(v) for n, v in root["props"] if n == "Caption"), "") or ""
    return {"class": root["class"].decode("latin-1"), "name": root["name"].decode("latin-1"),
            "caption": caption, "components": count(root), "texts": form_texts(root)}

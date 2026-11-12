"""
Parsers and serializers for the Win32 resource formats that carry UI text.

Each format has a parse_* that turns the raw bytes into plain Python data and a
build_* that turns that data back into bytes. The pair is lossless: building an
unmodified parse reproduces the original bytes. The patcher relies on that as a
safety check, and refuses to touch any resource that does not round-trip.

    RT_STRING   parse_string_block / build_string_block
    RT_DIALOG   parse_dialog       / build_dialog
    RT_MENU     parse_menu         / build_menu
    RT_VERSION  parse_version      / build_version
"""

from __future__ import annotations

import struct

RT_MENU, RT_DIALOG, RT_STRING, RT_VERSION = 4, 5, 6, 16

DS_SETFONT = 0x40
WS_VISIBLE = 0x10000000
WS_EX_LAYOUTRTL = 0x00400000    # mirror the window: origin moves to the right edge

MF_POPUP, MF_END = 0x0010, 0x0080           # classic menu item flags
MFR_POPUP, MFR_END = 0x01, 0x80             # extended menu "resinfo" flags

CLASS_ORDINALS = {0x80: "button", 0x81: "edit", 0x82: "static",
                  0x83: "listbox", 0x84: "scrollbar", 0x85: "combobox"}


class ResourceFormatError(ValueError):
    """The bytes do not look like the resource format we expected."""


# ---------------------------------------------------------------------------
# Byte helpers
# ---------------------------------------------------------------------------
class Reader:
    """Sequential little-endian reader over a resource blob."""

    def __init__(self, data: bytes, offset: int = 0):
        self.data, self.offset = data, offset

    def _take(self, fmt: str):
        try:
            (value,) = struct.unpack_from(fmt, self.data, self.offset)
        except struct.error as error:
            raise ResourceFormatError(str(error)) from error
        self.offset += struct.calcsize(fmt)
        return value

    def u8(self): return self._take("<B")
    def u16(self): return self._take("<H")
    def i16(self): return self._take("<h")
    def u32(self): return self._take("<I")

    def raw(self, count: int) -> bytes:
        if count < 0 or self.offset + count > len(self.data):
            raise ResourceFormatError("read past end of resource")
        chunk = self.data[self.offset: self.offset + count]
        self.offset += count
        return chunk

    def align(self, boundary: int) -> None:
        self.offset = (self.offset + boundary - 1) & ~(boundary - 1)

    def sz(self) -> str:
        """Null-terminated UTF-16 string."""
        start = self.offset
        while self.u16() != 0:
            pass
        return self.data[start: self.offset - 2].decode("utf-16-le", errors="surrogatepass")

    def sz_or_ord(self):
        """'String or ordinal': the word 0xFFFF means a 16-bit number follows
        instead of text. Returns str, or int for an ordinal."""
        if self.offset + 2 > len(self.data):
            raise ResourceFormatError("read past end of resource")
        if struct.unpack_from("<H", self.data, self.offset)[0] == 0xFFFF:
            self.offset += 2
            return self.u16()
        return self.sz()


class Writer:
    def __init__(self):
        self.data = bytearray()

    def u8(self, v): self.data += struct.pack("<B", v)
    def u16(self, v): self.data += struct.pack("<H", v)
    def i16(self, v): self.data += struct.pack("<h", v)
    def u32(self, v): self.data += struct.pack("<I", v)
    def raw(self, b): self.data += b

    def align(self, boundary: int) -> None:
        self.data += b"\x00" * (-len(self.data) % boundary)

    def sz(self, text: str) -> None:
        self.data += text.encode("utf-16-le", errors="surrogatepass") + b"\x00\x00"

    def sz_or_ord(self, value) -> None:
        if isinstance(value, int):
            self.u16(0xFFFF)
            self.u16(value)
        else:
            self.sz(value)


def same_ignoring_padding(original: bytes, rebuilt: bytes) -> bool:
    """True if `rebuilt` equals `original` apart from trailing zero padding."""
    shorter, longer = sorted((original, rebuilt), key=len)
    return longer[:len(shorter)] == shorter and not any(longer[len(shorter):])


# ---------------------------------------------------------------------------
# RT_STRING
# ---------------------------------------------------------------------------
# Strings are grouped into blocks of 16. Block N holds string IDs
# (N - 1) * 16 .. (N - 1) * 16 + 15. Each entry is a 16-bit character count
# followed by that many UTF-16 characters, with no terminator; a count of 0
# means "no string with this ID".

def parse_string_block(blob: bytes) -> list[str]:
    reader, strings = Reader(blob), []
    for _ in range(16):
        if reader.offset + 2 > len(blob):
            strings.append("")
            continue
        length = reader.u16()
        strings.append(reader.raw(length * 2).decode("utf-16-le", errors="surrogatepass"))
    return strings


def build_string_block(strings: list[str]) -> bytes:
    writer = Writer()
    for text in strings:
        encoded = text.encode("utf-16-le", errors="surrogatepass")
        writer.u16(len(encoded) // 2)
        writer.raw(encoded)
    return bytes(writer.data)


# ---------------------------------------------------------------------------
# RT_DIALOG
# ---------------------------------------------------------------------------
# A dialog is a DLGTEMPLATE, or the newer DLGTEMPLATEEX (recognisable by the
# words 0x0001, 0xFFFF at offset 0), followed by one item per control. The two
# versions hold the same information in a different field order. Every item
# starts on a 4-byte boundary. Positions are in dialog units, not pixels.

def parse_dialog(blob: bytes) -> dict:
    extended = len(blob) >= 4 and struct.unpack_from("<HH", blob, 0) == (1, 0xFFFF)
    r = Reader(blob, 4 if extended else 0)
    d: dict = {"extended": extended}
    if extended:
        d["helpID"], d["exStyle"], d["style"] = r.u32(), r.u32(), r.u32()
    else:
        d["style"], d["exStyle"] = r.u32(), r.u32()
    count = r.u16()
    d["x"], d["y"], d["cx"], d["cy"] = r.i16(), r.i16(), r.i16(), r.i16()
    d["menu"], d["windowClass"], d["title"] = r.sz_or_ord(), r.sz_or_ord(), r.sz_or_ord()
    d["font"] = None
    if d["style"] & DS_SETFONT:
        font = {"size": r.u16()}
        if extended:
            font["weight"], font["italic"], font["charset"] = r.u16(), r.u8(), r.u8()
        font["face"] = r.sz()
        d["font"] = font
    if count > 2000:
        raise ResourceFormatError("implausible control count")

    d["items"] = []
    for _ in range(count):
        r.align(4)
        item: dict = {}
        if extended:
            item["helpID"], item["exStyle"], item["style"] = r.u32(), r.u32(), r.u32()
        else:
            item["style"], item["exStyle"] = r.u32(), r.u32()
        item["x"], item["y"], item["cx"], item["cy"] = r.i16(), r.i16(), r.i16(), r.i16()
        item["id"] = r.u32() if extended else r.u16()
        item["class"], item["text"] = r.sz_or_ord(), r.sz_or_ord()
        item["extra"] = r.raw(r.u16())            # creation data, kept verbatim
        d["items"].append(item)
    return d


def build_dialog(d: dict) -> bytes:
    w = Writer()
    extended = d["extended"]
    if extended:
        w.u16(1); w.u16(0xFFFF)
        w.u32(d["helpID"]); w.u32(d["exStyle"]); w.u32(d["style"])
    else:
        w.u32(d["style"]); w.u32(d["exStyle"])
    w.u16(len(d["items"]))
    for key in ("x", "y", "cx", "cy"):
        w.i16(d[key])
    w.sz_or_ord(d["menu"]); w.sz_or_ord(d["windowClass"]); w.sz_or_ord(d["title"])
    if d["font"] is not None:
        font = d["font"]
        w.u16(font["size"])
        if extended:
            w.u16(font["weight"]); w.u8(font["italic"]); w.u8(font["charset"])
        w.sz(font["face"])
    for item in d["items"]:
        w.align(4)
        if extended:
            w.u32(item["helpID"]); w.u32(item["exStyle"]); w.u32(item["style"])
        else:
            w.u32(item["style"]); w.u32(item["exStyle"])
        for key in ("x", "y", "cx", "cy"):
            w.i16(item[key])
        (w.u32 if extended else w.u16)(item["id"])
        w.sz_or_ord(item["class"]); w.sz_or_ord(item["text"])
        w.u16(len(item["extra"])); w.raw(item["extra"])
    return bytes(w.data)


def control_kind(cls, style: int) -> str:
    """Reduce a window class + style to the handful of looks the preview draws."""
    name = CLASS_ORDINALS.get(cls, "") if isinstance(cls, int) else str(cls).lower()
    if name == "button":
        button_type = style & 0xF
        if button_type in (2, 3, 5, 6):
            return "checkbox"
        if button_type in (4, 9):
            return "radio"
        if button_type == 7:
            return "groupbox"
        return "default-button" if button_type == 1 else "button"
    if name == "static":
        static_type = style & 0x1F
        if static_type in (3, 0xE, 0xF):
            return "image"
        if static_type in (4, 5, 6, 7, 8, 9, 0x10, 0x11, 0x12):
            return "frame"
        return {1: "text-center", 2: "text-right"}.get(static_type, "text")
    if name in ("edit", "listbox", "combobox", "scrollbar"):
        return name
    return "custom"


def dialog_view(d: dict) -> dict:
    """JSON-friendly summary of a parsed dialog for the preview."""
    return {
        "title": d["title"] if isinstance(d["title"], str) else "",
        "cx": d["cx"], "cy": d["cy"],
        "font": d["font"] and {"size": d["font"]["size"], "face": d["font"]["face"]},
        "alreadyRtl": bool(d["exStyle"] & WS_EX_LAYOUTRTL),
        "items": [{
            "id": item["id"],
            "class": CLASS_ORDINALS.get(item["class"], f"#{item['class']}") if isinstance(item["class"], int) else item["class"],
            "kind": control_kind(item["class"], item["style"]),
            "text": item["text"] if isinstance(item["text"], str) else "",   # an ordinal is an icon id
            "x": item["x"], "y": item["y"], "cx": item["cx"], "cy": item["cy"],
            "visible": bool(item["style"] & WS_VISIBLE),
        } for item in d["items"]],
    }


# ---------------------------------------------------------------------------
# RT_MENU
# ---------------------------------------------------------------------------
# Classic menus are a flat stream of items: flags, then an ID (leaf) or nothing
# (popup, whose children follow), then the text. MF_END marks the last item of
# each level. Extended menus (version 1) add type/state/help fields and align
# every item to 4 bytes.

def parse_menu(blob: bytes) -> dict:
    r = Reader(blob)
    version, header_size = r.u16(), r.u16()
    if version == 0:
        header = r.raw(header_size)

        def read_level():
            items = []
            while True:
                flags = r.u16()
                if flags & MF_POPUP:
                    item = {"flags": flags, "text": r.sz()}
                    item["children"] = read_level()
                else:
                    item = {"flags": flags, "id": r.u16(), "text": r.sz()}
                items.append(item)
                if flags & MF_END:
                    return items
                if len(items) > 5000:
                    raise ResourceFormatError("menu level never ends")

        return {"extended": False, "header": header, "items": read_level()}

    if version == 1:
        header = r.raw(header_size)

        def read_level_ex():
            items = []
            while True:
                r.align(4)
                item = {"type": r.u32(), "state": r.u32(), "id": r.u32(), "resinfo": r.u16(), "text": r.sz()}
                if item["resinfo"] & MFR_POPUP:
                    r.align(4)
                    item["helpID"] = r.u32()
                    item["children"] = read_level_ex()
                items.append(item)
                if item["resinfo"] & MFR_END:
                    return items
                if len(items) > 5000:
                    raise ResourceFormatError("menu level never ends")

        return {"extended": True, "header": header, "items": read_level_ex()}

    raise ResourceFormatError(f"unknown menu version {version}")


def build_menu(menu: dict) -> bytes:
    w = Writer()
    w.u16(1 if menu["extended"] else 0)
    w.u16(len(menu["header"]))
    w.raw(menu["header"])

    def write_level(items):
        for item in items:
            if menu["extended"]:
                w.align(4)
                w.u32(item["type"]); w.u32(item["state"]); w.u32(item["id"]); w.u16(item["resinfo"])
                w.sz(item["text"])
                if "children" in item:
                    w.align(4)
                    w.u32(item["helpID"])
                    write_level(item["children"])
            else:
                w.u16(item["flags"])
                if "children" in item:
                    w.sz(item["text"])
                    write_level(item["children"])
                else:
                    w.u16(item["id"]); w.sz(item["text"])

    write_level(menu["items"])
    return bytes(w.data)


def walk_menu(items):
    """Yield every item of a parsed menu, depth first."""
    for item in items:
        yield item
        if "children" in item:
            yield from walk_menu(item["children"])


# ---------------------------------------------------------------------------
# RT_VERSION
# ---------------------------------------------------------------------------
# A tree of nodes: { wLength, wValueLength, wType, key, padding, value,
# padding, children }. Only the string leaves interest us (FileDescription).

_VERSION_CONTAINERS = ("VS_VERSION_INFO", "StringFileInfo", "VarFileInfo")


def _is_container(key: str, depth: int) -> bool:
    return key in _VERSION_CONTAINERS or (depth == 2 and len(key) == 8)   # "040904B0" string table


def parse_version(blob: bytes) -> dict:
    def read_node(offset: int, depth: int) -> tuple[dict, int]:
        r = Reader(blob, offset)
        length, value_length, node_type = r.u16(), r.u16(), r.u16()
        if length < 6 or offset + length > len(blob):
            raise ResourceFormatError("bad version node length")
        end = offset + length
        key = r.sz()
        r.align(4)
        node = {"key": key, "type": node_type, "children": []}
        if _is_container(key, depth):
            node["value"] = r.raw(min(value_length, end - r.offset))
            r.align(4)
            while r.offset < end:
                child, r.offset = read_node(r.offset, depth + 1)
                node["children"].append(child)
                r.align(4)
        elif node_type == 1:                          # text: value length counts characters
            raw = r.raw(max(0, min(value_length * 2, end - r.offset)))
            node["text"] = raw.decode("utf-16-le", errors="surrogatepass").rstrip("\x00")
        else:
            node["value"] = r.raw(max(0, min(value_length, end - r.offset)))
        return node, end

    root, _ = read_node(0, 0)
    return root


def build_version(root: dict) -> bytes:
    def write_node(node: dict) -> bytes:
        w = Writer()
        w.u16(0)                                      # length, patched below
        if "text" in node:
            w.u16(len(node["text"]) + 1 if node["text"] else 0)
        else:
            w.u16(len(node["value"]))
        w.u16(node["type"])
        w.sz(node["key"])
        w.align(4)
        if "text" in node:
            if node["text"]:
                w.sz(node["text"])
        else:
            w.raw(node["value"])
        for child in node["children"]:
            w.align(4)
            w.raw(write_node(child))
        struct.pack_into("<H", w.data, 0, len(w.data))
        return bytes(w.data)

    return write_node(root)


def version_strings(root: dict):
    """Yield every string leaf (e.g. FileDescription) of a parsed version resource."""
    for child in root["children"]:
        if "text" in child:
            yield child
        yield from version_strings(child)

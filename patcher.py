"""
Writes a translated copy of a Windows executable.

The heavy lifting of rebuilding the resource section is done by Windows itself,
through the documented resource-update API (BeginUpdateResource /
UpdateResource / EndUpdateResource). ExeLat's job is to produce the new bytes
for each string table, dialog and menu, and to deal with the two things that
API does not: the digital signature and data appended after the PE image.

The original file is never modified; everything happens on a copy.
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

import pefile

from pe_resources import (
    RT_DIALOG, RT_MENU, RT_STRING, RT_VERSION, WS_EX_LAYOUTRTL, ResourceFormatError,
    build_dialog, build_menu, build_string_block, build_version,
    parse_dialog, parse_menu, parse_string_block, parse_version,
    same_ignoring_padding, version_strings, walk_menu,
)

IMAGE_DIRECTORY_ENTRY_SECURITY = 4
LRM = "‎"


def translate_text(source: str, translations: dict[str, str]) -> str:
    """Look up one resource string. Menu items keep their shortcut hint
    ("Open\\tCtrl+O") even when the translation was written without it."""
    hebrew = translations.get(source, "").strip()
    if not hebrew:
        return source
    if "\t" in source and "\t" not in hebrew:
        hebrew += "\t" + source.split("\t", 1)[1]
    return hebrew


def iter_resources(pe, wanted_type: int):
    """Yield (name, language id, bytes) for every resource of one type.
    `name` is an int for numbered resources and a str for named ones."""
    if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        return
    for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if type_entry.id != wanted_type:
            continue
        for name_entry in type_entry.directory.entries:
            name = name_entry.id if name_entry.id is not None else str(name_entry.name)
            for lang_entry in name_entry.directory.entries:
                data = lang_entry.data.struct
                yield name, lang_entry.id or 0, pe.get_data(data.OffsetToData, data.Size)


def plan_updates(pe, translations: dict[str, str], mirror_dialogs: bool, mirror_process: bool):
    """Work out the new bytes for every resource that changes.
    Returns (updates, report); updates are (type, name, lang, bytes)."""
    updates = []
    report = {"strings": 0, "dialogs": 0, "dialogsMirrored": 0, "menus": 0, "menuItems": 0,
              "controls": 0, "skipped": 0, "processMirrored": False}

    for name, lang, blob in iter_resources(pe, RT_STRING):
        strings = parse_string_block(blob)
        new = [translate_text(s, translations) for s in strings]
        changed = sum(a != b for a, b in zip(strings, new))
        if changed and same_ignoring_padding(blob, build_string_block(strings)):
            updates.append((RT_STRING, name, lang, build_string_block(new)))
            report["strings"] += changed
        elif changed:
            report["skipped"] += 1

    for name, lang, blob in iter_resources(pe, RT_DIALOG):
        try:
            dialog = parse_dialog(blob)
            if not same_ignoring_padding(blob, build_dialog(dialog)):
                raise ResourceFormatError("dialog does not round-trip")
        except ResourceFormatError:
            report["skipped"] += 1
            continue
        changed = 0
        if isinstance(dialog["title"], str):
            new_title = translate_text(dialog["title"], translations)
            changed += new_title != dialog["title"]
            dialog["title"] = new_title
        for item in dialog["items"]:
            if isinstance(item["text"], str):
                new_text = translate_text(item["text"], translations)
                changed += new_text != item["text"]
                item["text"] = new_text
        mirrored = False
        if mirror_dialogs and not dialog["exStyle"] & WS_EX_LAYOUTRTL:
            # Windows mirrors the whole dialog, children included, at creation
            # time. Coordinates stay as they are.
            dialog["exStyle"] |= WS_EX_LAYOUTRTL
            mirrored = True
        if changed or mirrored:
            updates.append((RT_DIALOG, name, lang, build_dialog(dialog)))
            report["dialogs"] += 1
            report["controls"] += changed
            report["dialogsMirrored"] += mirrored

    for name, lang, blob in iter_resources(pe, RT_MENU):
        try:
            menu = parse_menu(blob)
            if not same_ignoring_padding(blob, build_menu(menu)):
                raise ResourceFormatError("menu does not round-trip")
        except ResourceFormatError:
            report["skipped"] += 1
            continue
        changed = 0
        for item in walk_menu(menu["items"]):
            new_text = translate_text(item["text"], translations)
            changed += new_text != item["text"]
            item["text"] = new_text
        if changed:
            updates.append((RT_MENU, name, lang, build_menu(menu)))
            report["menus"] += 1
            report["menuItems"] += changed

    if mirror_process:
        # Windows turns on right-to-left layout for every top-level window of a
        # process whose version resource has a FileDescription starting with
        # two LEFT-TO-RIGHT MARK characters. No code change needed.
        for name, lang, blob in iter_resources(pe, RT_VERSION):
            try:
                version = parse_version(blob)
                if not same_ignoring_padding(blob, build_version(version)):
                    raise ResourceFormatError("version info does not round-trip")
            except ResourceFormatError:
                report["skipped"] += 1
                continue
            touched = False
            for leaf in version_strings(version):
                if leaf["key"] == "FileDescription" and not leaf["text"].startswith(LRM * 2):
                    leaf["text"] = LRM * 2 + leaf["text"]
                    touched = True
            if touched:
                updates.append((RT_VERSION, name, lang, build_version(version)))
                report["processMirrored"] = True

    return updates, report


# ---------------------------------------------------------------------------
# Windows resource-update API
# ---------------------------------------------------------------------------
def _resource_id(value):
    """MAKEINTRESOURCE for numbers, a wide string pointer for names."""
    if isinstance(value, int):
        return ctypes.c_void_p(value), None
    buffer = ctypes.create_unicode_buffer(value)
    return ctypes.cast(buffer, ctypes.c_void_p), buffer    # keep `buffer` alive


def write_resources(path: str, updates) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL

    handle = kernel32.BeginUpdateResourceW(path, False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for res_type, name, lang, data in updates:
            type_ptr, _keep_type = _resource_id(res_type)
            name_ptr, _keep_name = _resource_id(name)
            buffer = ctypes.create_string_buffer(data, len(data))
            if not kernel32.UpdateResourceW(handle, type_ptr, name_ptr, lang, buffer, len(data)):
                raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        kernel32.EndUpdateResourceW(handle, True)      # discard
        raise
    if not kernel32.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def _split_image(source: str):
    """Return (image bytes without signature, appended data, was_signed).

    A PE file can carry two things after its last section: an Authenticode
    signature (pointed to by the security data directory) and arbitrary
    appended data such as an installer's payload. The signature is dropped,
    because it cannot be valid for a modified file. The appended data is kept
    and put back after the resources are rewritten.
    """
    pe = pefile.PE(source, fast_load=True)
    try:
        with open(source, "rb") as handle:
            data = bytearray(handle.read())
        image_end = max((s.PointerToRawData + s.SizeOfRawData for s in pe.sections), default=len(data))
        image_end = min(image_end, len(data))
        security = pe.OPTIONAL_HEADER.DATA_DIRECTORY[IMAGE_DIRECTORY_ENTRY_SECURITY]
        signed = security.Size > 0
        cert_start, cert_end = security.VirtualAddress, security.VirtualAddress + security.Size
        if signed:
            struct.pack_into("<II", data, security.get_file_offset(), 0, 0)
        tail = bytes(data[image_end:])
        if signed and cert_start >= image_end:
            tail = tail[: cert_start - image_end] + tail[cert_end - image_end:]
        return bytes(data[:image_end]), tail.rstrip(b"\x00") and tail, signed
    finally:
        pe.close()


def _fix_checksum(path: str) -> None:
    pe = pefile.PE(path, fast_load=True)
    try:
        offset = pe.OPTIONAL_HEADER.get_file_offset() + 0x40      # CheckSum field
        checksum = pe.generate_checksum()
    finally:
        pe.close()
    with open(path, "r+b") as handle:
        handle.seek(offset)
        handle.write(struct.pack("<I", checksum))


def build_translated(source: str, target: str, translations: dict[str, str],
                     mirror_dialogs: bool = True, mirror_process: bool = False) -> dict:
    """Write `target`, a copy of `source` with translated, mirrored resources."""
    if os.path.abspath(source).lower() == os.path.abspath(target).lower():
        raise ValueError("The output file must be different from the original.")

    pe = pefile.PE(source)
    try:
        updates, report = plan_updates(pe, translations, mirror_dialogs, mirror_process)
    finally:
        pe.close()
    if not updates:
        raise ValueError("Nothing to change: no translated text matches this file's resources.")

    image, appended, signed = _split_image(source)
    with open(target, "wb") as handle:
        handle.write(image)
    try:
        write_resources(target, updates)
        if appended:
            with open(target, "ab") as handle:
                handle.write(appended)
        _fix_checksum(target)
    except Exception:
        if os.path.exists(target):
            os.remove(target)
        raise

    report.update({
        "output": target,
        "size": os.path.getsize(target),
        "signatureRemoved": signed,
        "appendedBytes": len(appended) if appended else 0,
        "resourcesWritten": len(updates),
    })
    return report

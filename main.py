"""
ExeLat (Executable Translator) -- core analysis and command line.

Localises a compiled Windows program from an LTR language (Russian, English)
to Hebrew (RTL), without its source code:

    1. UNPACK        read the PE file's resources: string tables, dialogs, menus
    2. TRANSLATE     offline glossary first, then an LLM for everything else
    3. UI RTL FIX    mark every dialog (optionally every window) as mirrored
    4. REPACK        write a translated copy of the executable

Where each step lives:
    pe_resources.py   resource formats (parse + build)
    translator.py     glossary + LLM translation
    patcher.py        writing the new executable
    app.py            desktop app around all of it

Command line:
    python main.py app.exe                         # inspect only
    python main.py app.exe --out app.he.exe        # glossary translation + build
    python main.py app.exe --out app.he.exe --ai cli   # translate with Claude first

Only use this on software you own or are licensed to modify.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pefile

from delphi_forms import form_view, is_form, parse_form
from glossary import lookup
from patcher import RT_RCDATA, build_translated, iter_resources
from pe_resources import (
    RT_DIALOG, RT_MENU, RT_STRING, ResourceFormatError,
    dialog_view, parse_dialog, parse_menu, parse_string_block, walk_menu,
)

_MACHINES = {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64", 0x01C4: "ARM"}
_RESOURCE_TYPES = {1: "Cursor", 2: "Bitmap", 3: "Icon", 4: "Menu", 5: "Dialog", 6: "String table",
                   9: "Accelerator", 10: "RCDATA", 11: "Message table", 12: "Cursor group",
                   14: "Icon group", 16: "Version", 24: "Manifest"}
IMAGE_DIRECTORY_ENTRY_SECURITY = 4


def analyze(path: str) -> dict:
    """Everything the dashboard shows for one file, as JSON-ready data."""
    pe = pefile.PE(path)
    try:
        with open(path, "rb") as handle:
            head = handle.read(144)

        resource_counts: dict[str, int] = {}
        if hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
            for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
                label = _RESOURCE_TYPES.get(type_entry.id, str(type_entry.name or type_entry.id))
                resource_counts[label] = len(type_entry.directory.entries)

        strings = []
        for block_id, lang, blob in iter_resources(pe, RT_STRING):
            for index, text in enumerate(parse_string_block(blob)):
                if text and isinstance(block_id, int):
                    strings.append({"id": (block_id - 1) * 16 + index, "lang": lang, "source": text})

        dialogs, unparsed = [], 0
        for name, lang, blob in iter_resources(pe, RT_DIALOG):
            try:
                dialogs.append({"name": str(name), "lang": lang, **dialog_view(parse_dialog(blob))})
            except ResourceFormatError:
                unparsed += 1

        menus = []
        for name, lang, blob in iter_resources(pe, RT_MENU):
            try:
                items = [item["text"] for item in walk_menu(parse_menu(blob)["items"]) if item["text"]]
                menus.append({"name": str(name), "lang": lang, "items": items})
            except ResourceFormatError:
                unparsed += 1

        forms = []                                   # Delphi / C++Builder forms
        for name, lang, blob in iter_resources(pe, RT_RCDATA):
            if not is_form(blob):
                continue
            try:
                forms.append({"resource": str(name), "lang": lang, **form_view(parse_form(blob))})
            except (ResourceFormatError, RecursionError):
                unparsed += 1

        # One glossary lookup per distinct piece of text, wherever it appears.
        texts = {s["source"] for s in strings}
        for form in forms:
            texts.update(form["texts"])
        for dialog in dialogs:
            texts.add(dialog["title"])
            texts.update(item["text"] for item in dialog["items"])
        for menu in menus:
            texts.update(menu["items"])
        translations = {text: hebrew for text in texts if text and (hebrew := lookup(text))}

        overlay = pe.get_overlay_data_start_offset()
        security = pe.OPTIONAL_HEADER.DATA_DIRECTORY[IMAGE_DIRECTORY_ENTRY_SECURITY]
        size = os.path.getsize(path)
        appended = 0 if overlay is None else size - overlay - (security.Size if security.VirtualAddress >= (overlay or 0) else 0)
        return {
            "file": {
                "name": os.path.basename(path), "path": path, "size": size,
                "machine": _MACHINES.get(pe.FILE_HEADER.Machine, hex(pe.FILE_HEADER.Machine)),
                "signed": security.Size > 0,
                "appended": max(appended, 0),
                "head": head.hex(),
            },
            "sections": [{
                "name": section.Name.rstrip(b"\x00").decode("ascii", errors="replace"),
                "virtualSize": section.Misc_VirtualSize,
                "rawSize": section.SizeOfRawData,
            } for section in pe.sections],
            "resources": resource_counts,
            "strings": strings, "dialogs": dialogs, "menus": menus, "forms": forms,
            "unparsed": unparsed,
            "translations": translations,
        }
    finally:
        pe.close()


def translatable_texts(analysis: dict) -> list[str]:
    """Distinct texts worth sending to a translator, in a stable order."""
    seen: dict[str, None] = {}
    for s in analysis["strings"]:
        seen.setdefault(s["source"])
    for dialog in analysis["dialogs"]:
        seen.setdefault(dialog["title"])
        for item in dialog["items"]:
            if item["kind"] != "edit":
                seen.setdefault(item["text"])
    for menu in analysis["menus"]:
        for text in menu["items"]:
            seen.setdefault(text)
    for form in analysis["forms"]:
        for text in form["texts"]:
            seen.setdefault(text)
    return [text for text in seen if text and any(ch.isalpha() for ch in text)]


def main() -> int:
    parser = argparse.ArgumentParser(description="ExeLat: translate a Windows executable to Hebrew.")
    parser.add_argument("exe", help="path to a .exe / .dll / .mui you are allowed to modify")
    parser.add_argument("--out", help="write a translated copy here")
    parser.add_argument("--ai", choices=["none", "cli", "api"], default="none",
                        help="translate texts the glossary does not know: 'cli' uses the Claude Code "
                             "command line, 'api' uses the Anthropic API (credentials from the environment)")
    parser.add_argument("--no-mirror", action="store_true", help="do not mirror dialogs")
    parser.add_argument("--mirror-windows", action="store_true",
                        help="also mirror the program's main windows (experimental)")
    parser.add_argument("--json", action="store_true", help="print the analysis as JSON and stop")
    args = parser.parse_args()

    analysis = analyze(args.exe)
    if args.json:
        print(json.dumps(analysis, ensure_ascii=False, indent=2))
        return 0

    texts = translatable_texts(analysis)
    translations = dict(analysis["translations"])
    print(f"[unpack]    {len(analysis['strings'])} strings, {len(analysis['dialogs'])} dialogs, "
          f"{len(analysis['menus'])} menus, {len(analysis['forms'])} Delphi forms -> {len(texts)} distinct texts")
    print(f"[glossary]  {sum(t in translations for t in texts)} translated offline")

    if args.ai != "none":
        from translator import translate
        missing = [t for t in texts if t not in translations]
        result = translate(missing, provider=args.ai,
                           on_progress=lambda done, total: print(f"[ai]        {done}/{total}", end="\r"))
        translations.update(result["translations"])
        print(f"[ai]        {len(result['translations'])} translated, {len(result['rejected'])} rejected"
              + (f" ({result['error']})" if result["error"] else ""))

    if not args.out:
        print("[repack]    skipped (no --out)")
        return 0
    report = build_translated(args.exe, args.out, translations,
                              mirror_dialogs=not args.no_mirror, mirror_process=args.mirror_windows)
    print(f"[repack]    {report['output']}: {report['strings']} strings, {report['dialogs']} dialogs "
          f"({report['dialogsMirrored']} mirrored), {report['menus']} menus, "
          f"{report['forms']} Delphi forms ({report['formTexts']} texts), {report['skipped']} skipped")
    if report["signatureRemoved"]:
        print("            the original signature was removed; sign the new file yourself")
    if report["appendedBytes"]:
        print(f"            {report['appendedBytes']} bytes of appended data were carried over")
    return 0


if __name__ == "__main__":
    sys.exit(main())

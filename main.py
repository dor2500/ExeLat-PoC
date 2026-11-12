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
from patcher import PE_EXTENSIONS, RT_RCDATA, build_program, build_translated, collect_pe_files, iter_resources
from pe_resources import (
    RT_DIALOG, RT_MENU, RT_STRING, ResourceFormatError,
    dialog_view, parse_dialog, parse_menu, parse_string_block, walk_menu,
)

_MACHINES = {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64", 0x01C4: "ARM"}
_RESOURCE_TYPES = {1: "Cursor", 2: "Bitmap", 3: "Icon", 4: "Menu", 5: "Dialog", 6: "String table",
                   9: "Accelerator", 10: "RCDATA", 11: "Message table", 12: "Cursor group",
                   14: "Icon group", 16: "Version", 24: "Manifest"}
IMAGE_DIRECTORY_ENTRY_SECURITY = 4


_PACKER_SECTIONS = {"UPX0": "UPX", "UPX1": "UPX", ".aspack": "ASPack", ".vmp0": "VMProtect",
                    ".themida": "Themida", ".enigma1": "Enigma", ".MPRESS1": "MPRESS", "PEC2": "PECompact"}


def detect_tech(pe, has_forms: bool) -> list[str]:
    """What the file is built with, as far as it matters for translation.

    'dotnet', 'qt' and 'packed' are the important ones: such a file keeps its
    interface somewhere ExeLat cannot reach, or will not survive being changed.
    """
    tags = []
    if has_forms:
        tags.append("delphi")
    clr = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14] if len(pe.OPTIONAL_HEADER.DATA_DIRECTORY) > 14 else None
    if clr is not None and clr.VirtualAddress:
        tags.append("dotnet")
    imports = set()
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        try:
            imports.add(entry.dll.decode("ascii", errors="replace").lower())
        except AttributeError:
            pass
    if any(name.startswith(("qt5", "qt6", "qtcore", "qtgui", "qtwidgets")) for name in imports):
        tags.append("qt")
    if any(name.startswith("mfc") for name in imports):
        tags.append("mfc")
    if "msvbvm60.dll" in imports or "msvbvm50.dll" in imports:
        tags.append("vb6")
    names = {section.Name.rstrip(b"\x00").decode("ascii", errors="replace") for section in pe.sections}
    if names & set(_PACKER_SECTIONS):
        tags.append("packed")
    return tags


_UNSAFE_NAMES = {"desktop", "downloads", "documents", "program files", "program files (x86)",
                 "windows", "system32", "syswow64", "appdata", "local", "roaming", "programs", "users"}
_LANG_DIRS = {"lang", "langs", "language", "languages", "locale", "locales", "i18n", "translations", "l10n"}
_LANG_EXTENSIONS = {".lng", ".lang", ".qm", ".mo", ".po", ".xlf", ".resx"}


def check_program_folder(folder: str) -> None:
    """Refuse folders that are clearly not one program, such as Downloads or a
    drive root: scanning and copying them would sweep up unrelated files."""
    path = os.path.abspath(folder)
    if not os.path.isdir(path):
        raise ValueError("That is not a folder.")
    home = os.path.abspath(os.path.expanduser("~"))
    windows = os.path.abspath(os.environ.get("WINDIR", r"C:\Windows"))
    if (os.path.dirname(path) == path or path == home or path == windows
            or path.lower().startswith(windows.lower() + os.sep)
            or os.path.basename(path).lower() in _UNSAFE_NAMES):
        raise ValueError("Choose the program's own folder (for example C:\\Program Files\\7-Zip), "
                         "not a general folder such as Downloads, Desktop or Program Files.")


def scan_program(folder: str, on_progress=None) -> dict:
    """Analyse every executable, DLL and .mui of a program folder and merge
    the results into one analysis, with a per-file breakdown in 'parts'."""
    check_program_folder(folder)
    root = os.path.abspath(folder)
    files = collect_pe_files(root)
    merged = {"strings": [], "dialogs": [], "menus": [], "forms": [], "translations": {}}
    parts, unparsed, tech, main_part = [], 0, set(), None

    for index, rel in enumerate(files):
        if on_progress:
            on_progress(index, len(files))
        path = os.path.join(root, rel)
        try:
            part = analyze(path)
        except Exception as error:
            parts.append({"file": rel, "size": os.path.getsize(path), "error": type(error).__name__, "texts": 0, "tech": []})
            continue
        count = len(translatable_texts(part))
        parts.append({"file": rel, "size": part["file"]["size"], "texts": count, "tech": part["tech"],
                      "signed": part["file"]["signed"], "appended": part["file"]["appended"],
                      "strings": len(part["strings"]), "dialogs": len(part["dialogs"]),
                      "menus": len(part["menus"]), "forms": len(part["forms"])})
        tech.update(part["tech"])
        unparsed += part["unparsed"]
        if rel.lower().endswith(".exe") and os.sep not in rel and (main_part is None or count > main_part[0]):
            main_part = (count, part)
        for kind in ("strings", "dialogs", "menus", "forms"):
            for entry in part[kind]:
                entry["part"] = rel
            merged[kind].extend(part[kind])
        merged["translations"].update(part["translations"])

    # Things in the folder that ExeLat does not translate but the user should know about.
    lang_files, lang_dir, electron, total_files, hebrew_file = 0, None, False, 0, None
    for directory, _subdirs, names in os.walk(root):
        total_files += len(names)
        for name in names:
            stem = os.path.splitext(name)[0].lower()
            in_lang_dir = os.path.basename(directory).lower() in _LANG_DIRS
            if hebrew_file is None and (stem in ("hebrew", "he-il", "he_il", "heb") or (in_lang_dir and stem in ("he", "iw"))):
                hebrew_file = os.path.relpath(os.path.join(directory, name), root)
        if os.path.basename(directory).lower() in _LANG_DIRS:
            lang_dir = lang_dir or os.path.relpath(directory, root)
            lang_files += len(names)
        for name in names:
            extension = os.path.splitext(name)[1].lower()
            lang_files += extension in _LANG_EXTENSIONS and os.path.basename(directory).lower() not in _LANG_DIRS
            electron |= extension == ".asar"
    if electron:
        tech.add("electron")

    main = main_part[1] if main_part else None
    return {
        "file": {
            "name": os.path.basename(root), "path": root, "folder": True,
            "size": sum(p["size"] for p in parts),
            "machine": main["file"]["machine"] if main else "",
            "signed": any(p.get("signed") for p in parts if p["texts"]),
            "appended": 0,
            "head": main["file"]["head"] if main else "",
        },
        "sections": main["sections"] if main else [],
        "parts": parts,
        "hints": {"langFiles": lang_files, "langDir": lang_dir, "totalFiles": total_files, "hebrewFile": hebrew_file},
        "tech": sorted(tech),
        "unparsed": unparsed,
        **merged,
    }


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
            "tech": detect_tech(pe, bool(forms)),
            "siblings": sum(os.path.splitext(n)[1].lower() in PE_EXTENSIONS for n in os.listdir(os.path.dirname(os.path.abspath(path)))) - 1,
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
    parser.add_argument("exe", help="a .exe / .dll / .mui, or a whole program folder, that you are allowed to modify")
    parser.add_argument("--out", help="write a translated copy here (a file, or a new folder for a program folder)")
    parser.add_argument("--ai", choices=["none", "cli", "api"], default="none",
                        help="translate texts the glossary does not know: 'cli' uses the Claude Code "
                             "command line, 'api' uses the Anthropic API (credentials from the environment)")
    parser.add_argument("--no-mirror", action="store_true", help="do not mirror dialogs")
    parser.add_argument("--mirror-windows", action="store_true",
                        help="also mirror the program's main windows (experimental)")
    parser.add_argument("--json", action="store_true", help="print the analysis as JSON and stop")
    args = parser.parse_args()

    is_folder = os.path.isdir(args.exe)
    analysis = scan_program(args.exe) if is_folder else analyze(args.exe)
    if is_folder:
        for part in analysis["parts"]:
            if part["texts"] or part["tech"] or part.get("error"):
                print(f"[part]      {part['file']:<34} {part['texts']:>5} texts  {' '.join(part['tech'])} {part.get('error', '')}")
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
    report = (build_program if is_folder else build_translated)(
        args.exe, args.out, translations, mirror_dialogs=not args.no_mirror, mirror_process=args.mirror_windows)
    if is_folder:
        print(f"[repack]    {len(report['partsChanged'])} of {report['partsTotal']} files rebuilt: "
              f"{', '.join(report['partsChanged'][:8])}")
        for failed in report["partsFailed"]:
            print(f"            left unchanged: {failed['file']} ({failed['error']})")
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

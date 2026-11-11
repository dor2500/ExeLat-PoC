"""
ExeLat (Executable Translator) -- conceptual mock backend.

This file is a *proof of concept sketch*, not a finished tool. It shows, with
heavily commented Python, how a Windows executable could be localised from an
LTR language (Russian, English) to an RTL language (Hebrew) without its source:

    1. UNPACK        parse the PE file and walk its resource tree (.rsrc)
    2. AI TRANSLATE  send the extracted strings to an LLM
    3. UI RTL FIX    mirror dialog layouts and set RTL window styles
    4. REPACK        rebuild the resource section and patch the PE headers

Steps 1 and 3 contain real, working logic for the simple cases. Step 2 uses a
stub in place of a real LLM call, and step 4 is pseudo-code that explains what
has to happen but stops short of writing a binary.

Only use this on software you own or are licensed to modify.

Usage:
    pip install pefile
    python main.py path/to/app.exe            # inspect + mock-translate
    python main.py path/to/app.exe --json     # dump the translation plan as JSON
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass, asdict

try:
    import pefile
except ImportError:  # keep the file importable so the theory is still readable
    pefile = None


# ---------------------------------------------------------------------------
# Background: where does the text of a Win32 program live?
# ---------------------------------------------------------------------------
# A PE (Portable Executable) file is a set of headers followed by sections.
# The data directory entry IMAGE_DIRECTORY_ENTRY_RESOURCE points at the
# resource section (usually ".rsrc"), which holds a three-level tree:
#
#       level 1: resource TYPE   (RT_STRING, RT_DIALOG, RT_MENU, ...)
#       level 2: resource NAME   (an integer ID or a string name)
#       level 3: LANGUAGE        (LANGID, e.g. 0x0419 Russian, 0x040D Hebrew)
#
# Well-behaved programs keep their UI text there, which makes them the easy
# case ("static translation"). Programs that build strings in code, draw text
# themselves, or ship packed/encrypted need the other two strategies described
# in the README (dynamic API hooking, OCR overlay).

RT_MENU = 4
RT_DIALOG = 5
RT_STRING = 6

LANG_RUSSIAN = 0x0419
LANG_HEBREW = 0x040D

# Extended window styles that make a window behave right-to-left.
WS_EX_RIGHT = 0x00001000        # right-aligned text
WS_EX_RTLREADING = 0x00002000   # RTL reading order for the window text
WS_EX_LAYOUTRTL = 0x00400000    # mirror the whole window: origin moves to the right edge
RTL_EX_STYLES = WS_EX_RIGHT | WS_EX_RTLREADING | WS_EX_LAYOUTRTL


@dataclass
class StringResource:
    """One translatable string and where it came from."""
    string_id: int
    lang_id: int
    source: str
    translated: str | None = None


@dataclass
class ControlRect:
    """Position of a dialog control, in dialog units."""
    x: int
    y: int
    cx: int
    cy: int


# ---------------------------------------------------------------------------
# Phase 1 -- UNPACK
# ---------------------------------------------------------------------------
def load_pe(path: str):
    """Parse the executable. `pefile` reads the DOS header, the NT headers,
    the section table and every data directory, including resources."""
    if pefile is None:
        sys.exit("pefile is not installed. Run: pip install pefile")
    return pefile.PE(path)


def extract_string_tables(pe) -> list[StringResource]:
    """Pull every string out of the RT_STRING resources.

    String tables have an odd on-disk format. Strings are not stored one per
    resource; they are grouped into *blocks of 16*. A block with resource ID N
    holds string IDs (N - 1) * 16 .. (N - 1) * 16 + 15. Inside the block each
    string is a UTF-16LE Pascal string: a 16-bit character count followed by
    that many characters, with no terminator. A count of 0 means "no string
    with this ID".
    """
    results: list[StringResource] = []
    if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        return results  # no resource section at all

    for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if type_entry.id != RT_STRING:
            continue
        for block_entry in type_entry.directory.entries:          # level 2: block ID
            block_id = block_entry.id
            for lang_entry in block_entry.directory.entries:      # level 3: language
                rva = lang_entry.data.struct.OffsetToData
                size = lang_entry.data.struct.Size
                blob = pe.get_data(rva, size)

                offset = 0
                for index in range(16):
                    if offset + 2 > len(blob):
                        break
                    (length,) = struct.unpack_from("<H", blob, offset)
                    offset += 2
                    if length:
                        raw = blob[offset: offset + length * 2]
                        offset += length * 2
                        results.append(StringResource(
                            string_id=(block_id - 1) * 16 + index,
                            lang_id=lang_entry.id,
                            source=raw.decode("utf-16-le", errors="replace"),
                        ))
    return results


def extract_dialog_templates(pe) -> list[bytes]:
    """Return the raw bytes of every RT_DIALOG resource.

    PSEUDO-CODE for the parsing that would follow. A dialog is a DLGTEMPLATE
    (or the newer DLGTEMPLATEEX, recognisable by signature 0xFFFF) followed by
    one DLGITEMTEMPLATE per control:

        header:  style, exStyle, control count, x, y, cx, cy,
                 menu, window class, title (UTF-16), [font size + face name]
        item:    style, exStyle, x, y, cx, cy, id,
                 class (button / edit / static ...), text (UTF-16), extra data

    Everything is DWORD-aligned and variable length, so a real implementation
    needs a careful reader/writer pair that round-trips a template unchanged
    before it is trusted to modify one.
    """
    templates: list[bytes] = []
    if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        return templates
    for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if type_entry.id != RT_DIALOG:
            continue
        for name_entry in type_entry.directory.entries:
            for lang_entry in name_entry.directory.entries:
                data = lang_entry.data.struct
                templates.append(pe.get_data(data.OffsetToData, data.Size))
    return templates


# ---------------------------------------------------------------------------
# Phase 2 -- AI TRANSLATE
# ---------------------------------------------------------------------------
TRANSLATION_PROMPT = """You are localising the user interface of a Windows \
desktop application from {source_lang} to Hebrew.

Rules:
- Translate UI text the way a Hebrew edition of Windows would phrase it.
- Keep printf-style placeholders (%s, %d, %1) and escape sequences (\\n, \\t) exactly.
- Keep the '&' accelerator marker, moved onto a sensible Hebrew letter.
- Do not translate file paths, registry keys, URLs or product names.
- Return a JSON object mapping each string ID to its translation, nothing else.

Strings:
{payload}
"""


def call_llm(prompt: str) -> str:
    """STUB. A real implementation sends `prompt` to an LLM API and returns
    the model's text, for example:

        client = SomeLLMClient(api_key=os.environ["LLM_API_KEY"])
        response = client.generate(model="...", prompt=prompt)
        return response.text

    Here we return an empty JSON object so the PoC runs offline and never
    uploads anything.
    """
    return "{}"


def translate_strings(strings: list[StringResource], source_lang: str = "Russian",
                      batch_size: int = 40) -> None:
    """Fill in `translated` for every string, in batches.

    Why batches: UI strings are short and context-poor ("Open", "Back"). Sending
    neighbouring strings together lets the model see they belong to the same
    dialog and keep terminology consistent, and it costs far fewer requests.

    Why JSON keyed by ID: the model may reorder or merge lines; IDs make the
    mapping back to resources unambiguous and let us detect missing entries.
    """
    for start in range(0, len(strings), batch_size):
        batch = strings[start: start + batch_size]
        payload = json.dumps({s.string_id: s.source for s in batch}, ensure_ascii=False, indent=2)
        reply = call_llm(TRANSLATION_PROMPT.format(source_lang=source_lang, payload=payload))

        try:
            mapping = json.loads(reply)
        except json.JSONDecodeError:
            mapping = {}  # a real tool would retry or fall back to per-string calls

        for s in batch:
            # The stub returns nothing, so mark the string instead of translating it.
            s.translated = mapping.get(str(s.string_id), f"[he] {s.source}")

            # Validation a real tool should do before accepting a translation:
            #   - same set of placeholders as the source (%s, %d, %1 ...)
            #   - length within what the control can display, or flag for resize
            #   - no leftover source-language characters


# ---------------------------------------------------------------------------
# Phase 3 -- UI RTL FIX
# ---------------------------------------------------------------------------
def mirror_control(rect: ControlRect, dialog_width: int) -> ControlRect:
    """Mirror one control horizontally inside its dialog.

    A control that starts `x` units from the left edge must end up `x` units
    from the right edge. Its width does not change, so:

        new_x = dialog_width - (x + cx)
    """
    return ControlRect(dialog_width - (rect.x + rect.cx), rect.y, rect.cx, rect.cy)


def apply_rtl_ex_style(ex_style: int) -> int:
    """Turn on the RTL extended styles for a dialog.

    There are two ways to get a mirrored dialog, and they must not be combined:

      a) set WS_EX_LAYOUTRTL on the dialog and let Windows mirror the
         coordinate system at runtime (cheap, handles most standard controls);
      b) leave the style alone and rewrite every control's x with
         mirror_control() (needed for owner-drawn or oddly behaved controls).

    Doing both mirrors the layout twice and puts everything back where it was.
    """
    return ex_style | RTL_EX_STYLES


def fix_dialog_for_rtl(template: bytes) -> bytes:
    """PSEUDO-CODE: rewrite one dialog template for Hebrew.

        dialog = parse_dlgtemplate(template)
        dialog.ex_style = apply_rtl_ex_style(dialog.ex_style)
        dialog.title    = translations[dialog.title]
        dialog.font     = pick_font_with_hebrew_glyphs(dialog.font)   # e.g. Tahoma / Segoe UI
        for item in dialog.items:
            item.text = translations[item.text]
            if text_overflows(item):          # Hebrew is often shorter than Russian,
                grow_or_flag(item)            # but longer than terse English labels
        return serialize_dlgtemplate(dialog)

    The parse/serialize pair is the hard part and is not implemented here.
    """
    return template


# ---------------------------------------------------------------------------
# Phase 4 -- REPACK
# ---------------------------------------------------------------------------
def repack(pe, strings: list[StringResource], dialogs: list[bytes], out_path: str) -> None:
    """PSEUDO-CODE: write the translated resources back into a new executable.

    Translated resources almost never fit in the bytes the originals occupied,
    so patching in place does not work. The robust approach is to rebuild the
    whole resource tree and place it in a new section at the end of the file:

        1. Build a new resource directory in memory: every original resource,
           with RT_STRING blocks re-encoded from `strings` and RT_DIALOG
           entries replaced by `dialogs`. Either retag the language level as
           LANG_HEBREW or add Hebrew alongside the original language.

        2. Append a section (say ".rsrc2"), respecting both alignments:
               raw size      rounded up to OptionalHeader.FileAlignment
               virtual size  rounded up to OptionalHeader.SectionAlignment
           This needs a free slot in the section table; if the headers have no
           slack, the first section's raw data has to be shifted.

        3. Patch the headers:
               FileHeader.NumberOfSections                     += 1
               OptionalHeader.SizeOfImage                       = end of new section
               OptionalHeader.SizeOfInitializedData            += new raw size
               DataDirectory[IMAGE_DIRECTORY_ENTRY_RESOURCE]    = new RVA and size
           Resource data entries store RVAs, so they are computed for the new
           section's address, not copied from the old one.

        4. Recompute OptionalHeader.CheckSum (pefile: pe.generate_checksum()).

        5. Write the file (pefile: pe.write(out_path)).

    Consequences to tell the user about:
      - Any Authenticode signature is now invalid. The security directory
        should be stripped and the file re-signed with the user's own key.
      - Installers with an appended payload (NSIS, Inno Setup) keep data after
        the last section; it has to be moved along and its offsets respected.
      - Packed or self-checking executables will refuse to run. Those need the
        runtime strategies instead of static patching.
    """
    raise NotImplementedError("Repacking is described above but not implemented in this PoC.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="ExeLat PoC: inspect and mock-translate a Windows executable.")
    parser.add_argument("exe", help="path to a .exe or .dll you are allowed to modify")
    parser.add_argument("--source-lang", default="Russian")
    parser.add_argument("--json", action="store_true", help="print the translation plan as JSON")
    args = parser.parse_args()

    pe = load_pe(args.exe)
    strings = extract_string_tables(pe)
    dialogs = extract_dialog_templates(pe)
    translate_strings(strings, source_lang=args.source_lang)
    dialogs = [fix_dialog_for_rtl(d) for d in dialogs]

    if args.json:
        print(json.dumps([asdict(s) for s in strings], ensure_ascii=False, indent=2))
    else:
        print(f"[unpack]    {len(pe.sections)} sections, {len(strings)} strings, {len(dialogs)} dialogs")
        for s in strings[:10]:
            print(f"  #{s.string_id:<6} lang=0x{s.lang_id:04X}  {s.source!r} -> {s.translated!r}")
        if len(strings) > 10:
            print(f"  ... and {len(strings) - 10} more")
        print("[translate] stub LLM used; strings are tagged, not translated")
        print("[rtl fix]   dialog templates passed through unchanged (see fix_dialog_for_rtl)")
        print("[repack]    skipped: conceptual only (see repack)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

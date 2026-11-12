"""
ExeLat desktop app.

Wraps the dashboard (index.html) in a native window using pywebview and
connects it to the real code: analysis (main.py), translation (translator.py)
and writing the translated executable (patcher.py).

    python app.py                                  # open the app
    python app.py --analyze FILE --out J.json      # headless: analysis as JSON
    python app.py --analyze FILE --build OUT.exe   # headless: glossary-only translated copy
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from main import analyze
from patcher import build_translated

APP_NAME = "ExeLat"
APP_VERSION = "1.1.0"

FILE_TYPES = ("Windows binaries (*.exe;*.dll;*.mui;*.ocx;*.cpl;*.scr)", "All files (*.*)")

_window = None  # set in run_gui(); kept out of the Api class so it is not exposed to JavaScript


def resource_path(relative: str) -> str:
    """Locate a bundled file, both from source and from a PyInstaller build
    (which unpacks data files into a temp folder exposed as sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def safe_analyze(path: str) -> dict:
    """analyze() with errors turned into data the UI can show."""
    try:
        return {"ok": True, **analyze(path)}
    except Exception as error:  # pefile raises PEFormatError for non-PE files
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


class Api:
    """Methods callable from the page as window.pywebview.api.<name>()."""

    def version(self) -> str:
        return APP_VERSION

    def providers(self) -> dict:
        """Which AI translation routes are usable on this machine."""
        from translator import cli_path
        return {"cli": bool(cli_path()), "apiEnv": bool(os.environ.get("ANTHROPIC_API_KEY"))}

    def pick_file(self):
        import webview
        chosen = _window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=FILE_TYPES)
        return safe_analyze(chosen[0]) if chosen else None

    def translate(self, texts: list, provider: str, api_key: str = "") -> dict:
        """Translate texts with Claude. The key, if any, is used for this call only."""
        from translator import TranslationError, translate

        def progress(done: int, total: int) -> None:
            _window.evaluate_js(f"window.exelatProgress({int(done)}, {int(total)})")

        try:
            return translate([str(t) for t in texts], provider=provider, api_key=api_key or None, on_progress=progress)
        except TranslationError as error:
            return {"translations": {}, "rejected": [], "error": str(error)}
        except Exception as error:
            return {"translations": {}, "rejected": [], "error": f"{type(error).__name__}: {error}"}

    def build(self, source: str, translations: dict, mirror_dialogs: bool = True, mirror_windows: bool = False):
        """Ask where to save, then write the translated executable."""
        import webview
        stem, extension = os.path.splitext(os.path.basename(source))
        target = _window.create_file_dialog(
            webview.FileDialog.SAVE, directory=os.path.dirname(source),
            save_filename=f"{stem}.he{extension}", file_types=("Windows binaries (*.exe;*.dll;*.mui)", "All files (*.*)"))
        if not target:
            return None
        path = target if isinstance(target, str) else target[0]
        try:
            return {"ok": True, **build_translated(source, path, dict(translations),
                                                   mirror_dialogs=bool(mirror_dialogs), mirror_process=bool(mirror_windows))}
        except PermissionError:
            return {"ok": False, "error": "No permission to write there. Save to another folder (for example Documents) "
                                          "and copy the file next to the original afterwards."}
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def reveal(self, path: str) -> None:
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def export_plan(self, suggested_name: str, content: str):
        import webview
        target = _window.create_file_dialog(webview.FileDialog.SAVE, save_filename=suggested_name,
                                            file_types=("JSON (*.json)",))
        if not target:
            return None
        path = target if isinstance(target, str) else target[0]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path


def _bind_file_drop(window) -> None:
    """Browsers hide the real path of a dropped file from page scripts, so the
    drop is handled here, where pywebview exposes it, and pushed to the page."""
    from webview.dom import DOMEventHandler

    def on_drop(event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        path = files[0].get("pywebviewFullPath") if files else None
        if path:
            window.evaluate_js(f"window.exelatLoad({json.dumps(safe_analyze(path))})")

    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)


def run_gui() -> None:
    import webview

    global _window
    _window = webview.create_window(
        f"{APP_NAME} {APP_VERSION}",
        url=resource_path("index.html"),
        js_api=Api(),
        width=1320, height=900, min_size=(980, 640),
        background_color="#070b14",
    )
    webview.start(_bind_file_drop, _window)


def main() -> int:
    parser = argparse.ArgumentParser(description="ExeLat desktop app")
    parser.add_argument("--analyze", metavar="FILE", help="analyse FILE without opening a window")
    parser.add_argument("--out", metavar="JSON", help="where --analyze writes its result")
    parser.add_argument("--build", metavar="EXE", help="with --analyze: also write a glossary-translated copy")
    args = parser.parse_args()

    if args.analyze:
        result = safe_analyze(args.analyze)
        if args.build and result["ok"]:
            try:
                result["build"] = build_translated(args.analyze, args.build, result["translations"], mirror_process=True)
            except Exception as error:
                result["build"] = {"error": f"{type(error).__name__}: {error}"}
        # Confirms that a packaged build carries everything the GUI path needs.
        modules = {}
        for name in ("webview", "anthropic", "translator"):
            try:
                modules[name] = getattr(__import__(name), "__version__", "ok")
            except Exception as error:
                modules[name] = f"missing: {error}"
        result["modules"] = modules
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
        elif sys.stdout:            # a windowed build has no console to print to
            print(text)
        return 0

    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())

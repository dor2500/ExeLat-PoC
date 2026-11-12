"""
ExeLat desktop app.

Wraps the dashboard (index.html) in a native window using pywebview and
connects it to the real code: analysis (main.py), translation (translator.py)
and writing the translated program (patcher.py).

    python app.py                                  # open the app
    python app.py --analyze PATH --out J.json      # headless: analysis of a file or program folder
    python app.py --analyze PATH --build OUT       # headless: glossary-only translated copy
    python app.py --selftest PATH --build OUT --out J.json
                                                   # open the real window, load PATH, press Build, report
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

from main import analyze, scan_program
from patcher import build_program, build_translated

APP_NAME = "ExeLat"
APP_VERSION = "1.2.0"

FILE_TYPES = ("Windows binaries (*.exe;*.dll;*.mui;*.ocx;*.cpl;*.scr)", "All files (*.*)")

_window = None  # set in run_gui(); kept out of the Api class so it is not exposed to JavaScript


def resource_path(relative: str) -> str:
    """Locate a bundled file, both from source and from a PyInstaller build
    (which unpacks data files into a temp folder exposed as sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def _progress(kind: str):
    def report(done: int, total: int) -> None:
        if _window is not None:
            _window.evaluate_js(f"window.exelatProgress({int(done)}, {int(total)}, '{kind}')")
    return report


def safe_analyze(path: str) -> dict:
    """Analyse a file or a whole program folder, with errors turned into data the UI can show."""
    try:
        if os.path.isdir(path):
            return {"ok": True, **scan_program(path, on_progress=_progress("scan"))}
        return {"ok": True, **analyze(path)}
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    except Exception as error:  # pefile raises PEFormatError for non-PE files
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def _free_name(path: str) -> str:
    candidate, counter = path, 2
    while os.path.exists(candidate):
        candidate, counter = f"{path}-{counter}", counter + 1
    return candidate


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

    def pick_folder(self):
        import webview
        chosen = _window.create_file_dialog(webview.FileDialog.FOLDER)
        return safe_analyze(chosen[0]) if chosen else None

    def scan(self, path: str):
        """Analyse a path the page already knows (the folder of the loaded file)."""
        return safe_analyze(path)

    def translate(self, texts: list, provider: str, api_key: str = "") -> dict:
        """Translate texts with Claude. The key, if any, is used for this call only."""
        from translator import TranslationError, translate
        try:
            return translate([str(t) for t in texts], provider=provider, api_key=api_key or None,
                             on_progress=_progress("ai"))
        except TranslationError as error:
            return {"translations": {}, "rejected": [], "error": str(error)}
        except Exception as error:
            return {"translations": {}, "rejected": [], "error": f"{type(error).__name__}: {error}"}

    def build(self, source: str, translations: dict, mirror_dialogs: bool = True, mirror_windows: bool = False,
              target: str = ""):
        """Write the translated copy. A file becomes one new file next to the
        original; a program folder becomes a complete new folder."""
        import webview
        try:
            if os.path.isdir(source):
                if not target:
                    parent = _window.create_file_dialog(webview.FileDialog.FOLDER, directory=os.path.dirname(source))
                    if not parent:
                        return None
                    target = _free_name(os.path.join(parent[0], os.path.basename(source) + "-he"))
                return {"ok": True, **build_program(source, target, dict(translations), mirror_dialogs=bool(mirror_dialogs),
                                                    mirror_process=bool(mirror_windows), on_progress=_progress("build"))}
            if not target:
                stem, extension = os.path.splitext(os.path.basename(source))
                chosen = _window.create_file_dialog(
                    webview.FileDialog.SAVE, directory=os.path.dirname(source), save_filename=f"{stem}.he{extension}",
                    file_types=("Windows binaries (*.exe;*.dll;*.mui)", "All files (*.*)"))
                if not chosen:
                    return None
                target = chosen if isinstance(chosen, str) else chosen[0]
            return {"ok": True, **build_translated(source, target, dict(translations), mirror_dialogs=bool(mirror_dialogs),
                                                   mirror_process=bool(mirror_windows))}
        except PermissionError:
            return {"ok": False, "error": "No permission to write there. Choose another folder, for example Documents."}
        except Exception as error:
            return {"ok": False, "error": str(error) if isinstance(error, ValueError) else f"{type(error).__name__}: {error}"}

    def reveal(self, path: str) -> None:
        if os.path.isdir(path):
            os.startfile(path)
        elif os.path.exists(path):
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
            window.evaluate_js("window.exelatBusy && window.exelatBusy()")
            window.evaluate_js(f"window.exelatLoad({json.dumps(safe_analyze(path))})")

    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)


def _selftest(window, source: str, target: str, report_path: str) -> None:
    """Drive the real window the way a user would: load `source`, wait for the
    pipeline, press Build, and write what the page shows to `report_path`."""
    result: dict = {"stage": "start"}
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not window.evaluate_js("!!(window.pywebview && window.pywebview.api && window.exelatLoad)"):
            time.sleep(0.3)
        window.evaluate_js(f"window.exelatLoad({json.dumps(safe_analyze(source))})")
        result["stage"] = "loaded"
        deadline = time.time() + 60
        while time.time() < deadline and not window.evaluate_js("window.exelatState().ready"):
            time.sleep(0.3)
        result["before"] = window.evaluate_js("window.exelatState()")
        window.evaluate_js(f"window.exelatBuildTarget = {json.dumps(target)}; document.getElementById('buildBtn').click();")
        result["stage"] = "build clicked"
        deadline = time.time() + 300
        while time.time() < deadline and not window.evaluate_js("window.exelatState().finished"):
            time.sleep(0.5)
        result["after"] = window.evaluate_js("window.exelatState()")
        result["stage"] = "done"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    window.destroy()


def run_gui(selftest: tuple | None = None) -> None:
    import webview

    global _window
    _window = webview.create_window(
        f"{APP_NAME} {APP_VERSION}",
        url=resource_path("index.html"),
        js_api=Api(),
        width=1320, height=900, min_size=(980, 640),
        background_color="#070b14",
    )

    def on_start(window):
        _bind_file_drop(window)
        if selftest:
            threading.Thread(target=_selftest, args=(window, *selftest), daemon=True).start()

    webview.start(on_start, _window)


def main() -> int:
    parser = argparse.ArgumentParser(description="ExeLat desktop app")
    parser.add_argument("--analyze", metavar="PATH", help="analyse a file or program folder without opening a window")
    parser.add_argument("--selftest", metavar="PATH", help="open the window, load PATH and press Build (needs --build, --out)")
    parser.add_argument("--out", metavar="JSON", help="where --analyze / --selftest write their result")
    parser.add_argument("--build", metavar="TARGET", help="write a glossary-translated copy here")
    args = parser.parse_args()

    if args.selftest:
        run_gui(selftest=(args.selftest, args.build, args.out))
        return 0

    if args.analyze:
        result = safe_analyze(args.analyze)
        if args.build and result["ok"]:
            try:
                builder = build_program if os.path.isdir(args.analyze) else build_translated
                result["build"] = builder(args.analyze, args.build, result["translations"], mirror_process=True)
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

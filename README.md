<p align="center">
  <img src="assets/logo.png" alt="ExeLat logo" width="220" />
</p>

<h1 align="center">ExeLat — Executable Translator</h1>

<p align="center">
  <b>AI-assisted localisation of compiled Windows software from LTR languages to Hebrew (RTL).</b>
</p>

<p align="center">
  <img alt="Status" src="https://img.shields.io/badge/status-proof%20of%20concept-f5b941" />
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" />
  <img alt="Platform" src="https://img.shields.io/badge/target-Win32%20PE-0078D6" />
  <img alt="UI" src="https://img.shields.io/badge/UI-Tailwind%20CSS-38BDF8?logo=tailwindcss&logoColor=white" />
  <img alt="Direction" src="https://img.shields.io/badge/LTR%20%E2%86%92%20RTL-RU%20%2F%20EN%20%E2%86%92%20HE-4fd8e0" />
</p>

---

## 📖 Project Overview

Plenty of useful Windows software — installers, utilities, older Win32 apps — was never released in Hebrew, and its source code is long gone. **ExeLat** explores whether that software can be localised anyway, working directly on the compiled binary.

Translating to Hebrew is more than swapping strings. Hebrew reads right-to-left, so a correct result also has to **mirror the user interface**: labels move to the right of their fields, button order flips, text aligns right. ExeLat treats both as one problem:

1. Pull the text and dialog layouts out of the executable.
2. Translate the text with a large language model.
3. Flip the layout for RTL.
4. Put everything back.

![ExeLat pipeline](assets/architecture.png)

> [!IMPORTANT]
> **This repository is a proof of concept.** It contains a UI mockup and a commented conceptual backend. It does not yet produce a working translated executable. See [Project Status](#-project-status) for exactly what runs today.

## ✨ Features

ExeLat is designed around three strategies, tried from cheapest to most invasive. Which one applies depends on where a program keeps its text.

### 📦 Static Translation
For programs that store their UI in PE resources (string tables, dialogs, menus).

- Parses the PE file and walks the `.rsrc` resource tree
- Extracts `RT_STRING`, `RT_DIALOG` and `RT_MENU` entries
- Sends strings to an LLM in batches, keyed by resource ID, preserving placeholders (`%s`, `%1`) and `&` accelerators
- Mirrors dialog templates and sets `WS_EX_LAYOUTRTL | WS_EX_RTLREADING`
- Rebuilds the resource section and patches the PE headers

### 🪝 Dynamic API Hooking
For programs that build their text in code, so there is nothing in the resources to patch.

- Injects a helper DLL into the running process
- Hooks text-carrying Win32 calls such as `CreateWindowExW`, `SetWindowTextW`, `DrawTextW` and `TextOutW`
- Looks each string up in a translation cache (falling back to the LLM for unseen strings) and substitutes it before the original call runs
- Applies RTL extended styles to windows as they are created

### 🔍 OCR Overlay
The last resort, for programs that draw their own UI as pixels (custom skins, games, some installers).

- Captures the application window
- Runs OCR to find text regions and read the source text
- Translates and draws the Hebrew text in a transparent, click-through overlay window positioned over the original

| Strategy | Works when | Modifies the file | Mirrors layout |
|---|---|---|---|
| Static Translation | text lives in `.rsrc` | ✅ yes | ✅ fully |
| Dynamic API Hooking | text is created at runtime | ❌ no (in memory) | ✅ mostly |
| OCR Overlay | text is rendered as pixels | ❌ no | ❌ text only |

## 🖥️ Dashboard Mockup

`index.html` is a self-contained mockup of the ExeLat dashboard: a drag-and-drop zone for `.exe` files, an animated pipeline, and a live side-by-side preview of an installer dialog going from Russian (LTR) to Hebrew (RTL).

![ExeLat dashboard](assets/mockup_preview.png)

## 🧰 Tech Stack

| Layer | Technology | Role |
|---|---|---|
| Binary parsing | [`pefile`](https://github.com/erocarrera/pefile) | Read PE headers, sections and resources |
| Translation | Any LLM API | Context-aware UI string translation |
| Backend | Python 3.10+ | Pipeline orchestration (`main.py`) |
| Frontend | HTML, [Tailwind CSS](https://tailwindcss.com) (CDN), vanilla JS | Dashboard mockup (`index.html`) |
| Planned: hooking | C/C++ DLL, Microsoft Detours or MinHook | Runtime API interception |
| Planned: OCR | Tesseract or Windows.Media.Ocr | Reading rendered text |

## 🚀 How to Run the PoC

### 1. Clone

```bash
git clone https://github.com/dor2500/ExeLat-PoC.git
cd ExeLat-PoC
```

### 2. Open the dashboard mockup

Open `index.html` in any modern browser — no build step and no server. An internet connection is needed the first time, to load Tailwind and fonts from their CDNs.

- Click **Run demo translation** to watch the simulated pipeline, or drop any `.exe` on the drop zone.
- Append `?demo=done` to the URL to jump straight to the finished state.

The mockup never reads or uploads the file you drop; it only uses its name and size.

### 3. Run the conceptual backend

```bash
pip install pefile
python main.py path/to/some-app.exe
```

This parses the executable, lists the strings found in its string tables, and runs them through a **stubbed** translator (each string is tagged `[he]` rather than translated). Add `--json` for machine-readable output. No network calls are made.

## 🧭 Project Status

| Component | State |
|---|---|
| Dashboard mockup (`index.html`) | ✅ Working — the pipeline is simulated in the browser |
| PE parsing and string-table extraction (`main.py`) | ✅ Working |
| LLM translation | 🟡 Stub — prompt and batching are written, the API call is not |
| Dialog template rewrite (RTL fix) | 🟡 Mirroring maths implemented; template parser is pseudo-code |
| Repacking the executable | 🔴 Pseudo-code only, documented step by step |
| Dynamic API hooking | 🔴 Design only |
| OCR overlay | 🔴 Design only |

## 📁 Repository Layout

```
ExeLat-PoC/
├── README.md
├── index.html              # dashboard mockup
├── main.py                 # conceptual backend
└── assets/
    ├── logo.png
    ├── architecture.png
    ├── mockup_preview.png
    └── *.svg               # sources for the logo and diagram
```

## ⚠️ Known Limits

- **Signatures break.** Any change to a signed executable invalidates its Authenticode signature; the output must be re-signed with your own certificate.
- **Packed or self-checking binaries** will not survive static patching.
- **Installers with appended payloads** (NSIS, Inno Setup) need their trailing data preserved.
- **Bidirectional text** — paths, numbers and product names inside Hebrew sentences — needs care that a simple mirror does not provide.

## ⚖️ Responsible Use

Modifying software can be restricted by its licence and by local law. Use ExeLat only on software you own or are permitted to modify, and do not redistribute modified binaries without the right to do so.

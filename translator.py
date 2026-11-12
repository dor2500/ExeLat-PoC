"""
LLM translation of UI strings to Hebrew.

Two ways to reach Claude:

    provider="api"   the Anthropic API through the official SDK. Uses the key
                     passed in, or whatever credentials the environment has
                     (ANTHROPIC_API_KEY, or an `ant auth login` profile).
    provider="cli"   the Claude Code command line (`claude -p`), if it is
                     installed. Uses that tool's own sign-in; no API key.

Strings are sent in batches and come back as JSON keyed by a numeric id, so a
reordered or partial answer can never attach a translation to the wrong string.
Every translation is checked before it is accepted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

API_MODEL = "claude-opus-5-5"
CLI_MODEL = "sonnet"
BATCH_SIZE = 60
WORKERS = 3
RETRIES = 2

SYSTEM_PROMPT = """You localise the user interface of Windows desktop programs into Hebrew.

You receive a JSON array of {"id", "text"} objects. The texts are UI strings from one program: \
menu items, button labels, dialog titles, messages. The source language is usually English or Russian.

Translate each text the way the Hebrew edition of Windows would phrase it: short, standard, \
imperative for commands (Save -> שמור, Cancel -> ביטול, Browse... -> עיון...).

Keep these exactly as they are, in the same order:
- placeholders such as %s, %d, %1, %ls, {0}
- escape characters and line breaks
- a trailing "..." or ":" (the colon stays at the end of the Hebrew text)
- anything after a tab character (it is a keyboard shortcut such as Ctrl+O)
- file paths, URLs, registry keys, file extensions, version numbers

An "&" marks the next letter as a keyboard accelerator. Keep exactly one "&" if the source has one, \
placed before a Hebrew letter of your translation.

Do not translate product names, company names, or strings that are identifiers rather than \
prose (for example "MS Shell Dlg", "msctls_progress32", "http"). Return those unchanged.

Return every id you were given."""

SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "he": {"type": "string"}},
                "required": ["id", "he"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["translations"],
    "additionalProperties": False,
}

_PLACEHOLDER = re.compile(r"%(?:\d+!?[^!\s]*!?|[-+0 #]*\d*(?:\.\d+)?(?:hh|h|ll|l|I64|I32|w)?[sdiuxXcCSfgGeEp%])|\{\d+\}")
_HEBREW = re.compile(r"[֐-׿]")


class TranslationError(Exception):
    """A problem the user can act on (bad key, CLI missing, rate limit...)."""


def cli_path() -> str | None:
    return shutil.which("claude")


def is_acceptable(source: str, hebrew: str) -> bool:
    """Reject translations that would break the program or lose information."""
    if not hebrew.strip():
        return False
    if sorted(_PLACEHOLDER.findall(source)) != sorted(_PLACEHOLDER.findall(hebrew)):
        return False                      # a missing %s can crash a printf-style call
    if "\t" in source and source.split("\t", 1)[1] != hebrew.split("\t", 1)[-1]:
        return False                      # the shortcut hint was altered
    if hebrew.count("&") - 2 * hebrew.count("&&") > 1:
        return False                      # more than one accelerator
    return True


def _user_message(batch: list[tuple[int, str]]) -> str:
    return json.dumps([{"id": i, "text": text} for i, text in batch], ensure_ascii=False)


def _parse_reply(reply: str) -> dict[int, str]:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < 0:
        raise TranslationError("The model did not return JSON.")
    try:
        data = json.loads(reply[start: end + 1])
        return {int(entry["id"]): str(entry["he"]) for entry in data["translations"]}
    except (ValueError, KeyError, TypeError) as error:
        raise TranslationError(f"Could not read the model's answer: {error}") from error


def _call_api(client, batch: list[tuple[int, str]]) -> dict[int, str]:
    import anthropic

    try:
        # Server-side fallback: if the request is declined on policy grounds
        # the API re-runs it on a fallback model inside the same call.
        response = client.beta.messages.create(
            model=API_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_message(batch)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.AuthenticationError as error:
        raise TranslationError("The API key was rejected. Check the key and try again.") from error
    except anthropic.PermissionDeniedError as error:
        raise TranslationError(f"The API refused this request: {error.message}") from error
    except anthropic.RateLimitError as error:
        raise TranslationError("Rate limit reached. Wait a minute and press Translate again.") from error
    except anthropic.APIStatusError as error:
        raise TranslationError(f"API error {error.status_code}: {error.message}") from error
    except anthropic.APIConnectionError as error:
        raise TranslationError("Could not reach the Anthropic API. Check the internet connection.") from error

    if response.stop_reason == "refusal":
        raise TranslationError("The model declined to translate this batch.")
    if response.stop_reason == "max_tokens":
        raise TranslationError("The answer was cut off; try again.")
    return _parse_reply(next(block.text for block in response.content if block.type == "text"))


def _call_cli(executable: str, batch: list[tuple[int, str]]) -> dict[int, str]:
    prompt = (SYSTEM_PROMPT + '\n\nAnswer with one JSON object and nothing else, shaped like '
              '{"translations": [{"id": 0, "he": "..."}]}.\n\nInput:\n' + _user_message(batch))
    try:
        # An empty working directory keeps unrelated project context out of the request.
        with tempfile.TemporaryDirectory() as workdir:
            completed = subprocess.run(
                [executable, "-p", "--output-format", "json", "--model", CLI_MODEL],
                input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600, cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except subprocess.TimeoutExpired as error:
        raise TranslationError("Claude Code took too long to answer.") from error
    except OSError as error:
        raise TranslationError(f"Could not start Claude Code: {error}") from error
    try:
        envelope = json.loads(completed.stdout)
    except ValueError:
        envelope = None
    if completed.returncode != 0 or envelope is None or envelope.get("is_error"):
        # On failure the CLI still prints its JSON envelope; the readable part is "result".
        detail = str(envelope.get("result") or envelope.get("subtype") or "") if envelope else ""
        detail = detail or (completed.stderr or completed.stdout or "").strip()
        raise TranslationError(f"Claude Code failed: {detail[:200] or 'no output'}")
    return _parse_reply(str(envelope.get("result", "")))


def translate(texts: list[str], provider: str = "cli", api_key: str | None = None, on_progress=None) -> dict:
    """Translate distinct UI strings to Hebrew.

    Returns {"translations": {source: hebrew}, "rejected": [source, ...], "error": str | None}.
    A failure part-way through keeps what was already translated.
    """
    texts = list(dict.fromkeys(t for t in texts if t and t.strip()))
    result = {"translations": {}, "rejected": [], "error": None}
    if not texts:
        return result

    if provider == "api":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        call = lambda batch: _call_api(client, batch)
    elif provider == "cli":
        executable = cli_path()
        if not executable:
            raise TranslationError("Claude Code is not installed (the `claude` command was not found).")
        call = lambda batch: _call_cli(executable, batch)
    else:
        raise TranslationError(f"Unknown provider: {provider}")

    indexed = list(enumerate(texts))
    batches = [indexed[i: i + BATCH_SIZE] for i in range(0, len(indexed), BATCH_SIZE)]
    done = 0
    if on_progress:
        on_progress(0, len(texts))

    stop = threading.Event()

    def call_with_retry(batch):
        """A batch can fail for passing reasons (a busy service, one malformed
        answer). Try again before giving up on its strings."""
        for attempt in range(RETRIES + 1):
            if stop.is_set():
                raise TranslationError("Stopped after an earlier error.")
            try:
                return call(batch)
            except TranslationError as error:
                message = str(error)
                permanent = ("key was rejected", "not installed", "limit", "refused", "declined")
                if attempt == RETRIES or stop.is_set() or any(word in message for word in permanent):
                    stop.set()            # an exhausted quota fails every batch: do not keep spending calls
                    raise
                time.sleep(2 + 3 * attempt)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for batch, future in [(b, pool.submit(call_with_retry, b)) for b in batches]:
            try:
                answers = future.result()
            except TranslationError as error:
                result["error"] = result["error"] or str(error)
                answers = {}
            for index, source in batch:
                hebrew = answers.get(index)
                if hebrew is None:
                    continue
                if hebrew.strip() == source.strip() and not _HEBREW.search(hebrew):
                    continue                              # deliberately left untranslated
                if is_acceptable(source, hebrew):
                    result["translations"][source] = hebrew
                else:
                    result["rejected"].append(source)
            done += len(batch)
            if on_progress:
                on_progress(done, len(texts))
    return result

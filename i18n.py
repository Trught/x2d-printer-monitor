"""Small editable JSON translation catalogs; protocol keys always stay intact."""

from __future__ import annotations

import json
from pathlib import Path
from string import Formatter

LOCALE_DIR = Path(__file__).resolve().parent / "translations"
_language = "en"
_messages: dict[str, str] = {}


def available_languages() -> dict[str, str]:
    result = {"en": "English"}
    for path in sorted(LOCALE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result[path.stem] = data.get("_name", path.stem)
        except (OSError, ValueError, AttributeError):
            continue
    return result


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name is not None}


def set_language(language: str) -> None:
    global _language, _messages
    data = {}
    if language != "en":
        if language not in available_languages():
            language = "en"
        else:
            try:
                data = json.loads(
                    (LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                language = "en"
    messages = {}
    for source, translated in data.items():
        if source.startswith("_") or not isinstance(translated, str):
            continue
        try:
            if _fields(source) == _fields(translated):
                messages[source] = translated
        except ValueError:
            continue
    _language, _messages = language, messages


def get_language() -> str:
    return _language


def tr(text_id: str, **values) -> str:
    text = _messages.get(text_id, text_id)
    return text.format(**values) if values else text


def bind_static_text(root, exclude=()):
    """Remember original UI labels so language changes preserve live app state."""
    bindings = []

    def visit(widget):
        if widget in exclude:
            return
        if widget not in exclude:
            try:
                text = widget.cget("text")
                if text:
                    bindings.append(
                        lambda w=widget, s=text: w.configure(text=tr(s))
                        if w.winfo_exists()
                        else None
                    )
            except Exception:  # widgets without a text option
                pass
        if widget.winfo_class() == "TNotebook":
            for tab in widget.tabs():
                text = widget.tab(tab, "text")
                bindings.append(lambda w=widget, t=tab, s=text: w.tab(t, text=tr(s)))
        if widget.winfo_class() == "Treeview":
            for column in widget.cget("columns"):
                text = widget.heading(column, "text")
                bindings.append(
                    lambda w=widget, c=column, s=text: w.heading(c, text=tr(s))
                )
        for child in widget.winfo_children():
            visit(child)

    visit(root)
    for apply in bindings:
        apply()
    return bindings

"""
Offline glossary of very common UI terms -> Hebrew.

This is what ExeLat can translate with no LLM at all: the short, standard
labels that appear in almost every Windows program. Anything not found here is
left for the LLM stage (or for manual editing in the dashboard).

Keys are normalised: lower case, no '&' accelerator, no trailing "..." or ":".
"""

from __future__ import annotations

import re

GLOSSARY: dict[str, str] = {
    # --- English ---
    "ok": "אישור", "cancel": "ביטול", "yes": "כן", "no": "לא", "close": "סגור",
    "apply": "החל", "help": "עזרה", "back": "הקודם", "next": "הבא", "finish": "סיום",
    "browse": "עיון", "install": "התקן", "uninstall": "הסר התקנה", "exit": "יציאה",
    "open": "פתח", "save": "שמור", "save as": "שמירה בשם", "print": "הדפס",
    "file": "קובץ", "edit": "עריכה", "view": "תצוגה", "tools": "כלים", "options": "אפשרויות",
    "settings": "הגדרות", "properties": "מאפיינים", "about": "אודות", "new": "חדש",
    "delete": "מחק", "remove": "הסר", "add": "הוסף", "copy": "העתק", "cut": "גזור",
    "paste": "הדבק", "undo": "בטל", "redo": "בצע שוב", "select all": "בחר הכל",
    "find": "חיפוש", "find next": "חפש את הבא", "replace": "החלף", "replace all": "החלף הכל",
    "search": "חיפוש", "retry": "נסה שוב", "ignore": "התעלם", "abort": "בטל",
    "continue": "המשך", "skip": "דלג", "stop": "עצור", "start": "התחל", "run": "הפעל",
    "name": "שם", "type": "סוג", "size": "גודל", "date": "תאריך", "folder": "תיקייה",
    "file name": "שם קובץ", "files of type": "קבצים מסוג", "look in": "חפש ב",
    "error": "שגיאה", "warning": "אזהרה", "information": "מידע", "question": "שאלה",
    "font": "גופן", "color": "צבע", "style": "סגנון", "language": "שפה",
    "username": "שם משתמש", "user name": "שם משתמש", "password": "סיסמה",
    "i agree": "אני מסכים", "i accept the agreement": "אני מקבל את ההסכם",
    "i do not accept the agreement": "אני לא מקבל את ההסכם",
    "license agreement": "הסכם רישיון", "destination folder": "תיקיית יעד",
    "default": "ברירת מחדל", "advanced": "מתקדם", "general": "כללי", "details": "פרטים",
    "all files": "כל הקבצים", "read-only": "לקריאה בלבד", "up": "למעלה", "down": "למטה",
    "match case": "התאם רישיות", "direction": "כיוון", "printer": "מדפסת", "copies": "עותקים",
    "all": "הכל", "pages": "עמודים", "selection": "בחירה", "preview": "תצוגה מקדימה",
    "refresh": "רענן", "rename": "שנה שם", "update": "עדכן", "download": "הורד",
    "ready": "מוכן", "loading": "טוען", "please wait": "נא להמתין", "done": "בוצע",

    # --- Russian ---
    "ок": "אישור", "отмена": "ביטול", "да": "כן", "нет": "לא", "закрыть": "סגור",
    "применить": "החל", "справка": "עזרה", "помощь": "עזרה", "назад": "הקודם",
    "далее": "הבא", "готово": "סיום", "завершить": "סיום", "обзор": "עיון",
    "установить": "התקן", "удалить": "הסר", "выход": "יציאה", "открыть": "פתח",
    "сохранить": "שמור", "сохранить как": "שמירה בשם", "печать": "הדפס",
    "файл": "קובץ", "правка": "עריכה", "вид": "תצוגה", "сервис": "כלים",
    "параметры": "אפשרויות", "настройки": "הגדרות", "свойства": "מאפיינים",
    "о программе": "אודות", "создать": "חדש", "добавить": "הוסף", "копировать": "העתק",
    "вырезать": "גזור", "вставить": "הדבק", "отменить": "בטל", "выделить все": "בחר הכל",
    "найти": "חיפוש", "найти далее": "חפש את הבא", "заменить": "החלף", "поиск": "חיפוש",
    "повтор": "נסה שוב", "повторить": "נסה שוב", "пропустить": "דלג", "продолжить": "המשך",
    "стоп": "עצור", "пуск": "התחל", "имя": "שם", "тип": "סוג", "размер": "גודל",
    "дата": "תאריך", "папка": "תיקייה", "имя файла": "שם קובץ", "тип файлов": "קבצים מסוג",
    "ошибка": "שגיאה", "предупреждение": "אזהרה", "внимание": "אזהרה", "информация": "מידע",
    "шрифт": "גופן", "цвет": "צבע", "язык": "שפה", "пароль": "סיסמה",
    "имя пользователя": "שם משתמש", "папка установки": "תיקיית התקנה",
    "лицензионное соглашение": "הסכם רישיון", "я принимаю условия соглашения": "אני מקבל את ההסכם",
    "по умолчанию": "ברירת מחדל", "дополнительно": "מתקדם", "общие": "כללי",
    "все файлы": "כל הקבצים", "обновить": "רענן", "переименовать": "שנה שם",
    "подождите": "נא להמתין", "загрузка": "טוען",
}

_ACCEL = re.compile(r"&(?!&)")
_WRAP = re.compile(r"^(?P<lead>[<\s]*)(?P<core>.*?)(?P<tail>\s*(?:\.\.\.|…|:|>)*\s*)$", re.S)


def lookup(text: str) -> str | None:
    """Translate one UI label if it is a known standard term.

    Decoration that carries no meaning is peeled off, the core term is looked
    up, and the decoration is put back: "&Browse..." -> "עיון...".
    Wizard arrows ("< Back", "Next >") are dropped, since their direction is
    wrong once the dialog is mirrored.
    """
    if not text or len(text) > 60:
        return None
    plain = _ACCEL.sub("", text).split("\t")[0].strip()
    match = _WRAP.match(plain)
    if not match:
        return None
    core = match.group("core").strip().lower()
    hebrew = GLOSSARY.get(core)
    if hebrew is None:
        return None
    tail = match.group("tail").replace(">", "").strip()
    return hebrew + tail

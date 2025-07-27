import json
from pathlib import Path

def load_translations():
    """Загружает все файлы переводов из папки locales в память."""
    translations = {}
    locales_dir = Path(__file__).parent.parent / "locales"
    for file in locales_dir.glob("*.json"):
        lang_code = file.stem
        with open(file, "r", encoding="utf-8") as f:
            translations[lang_code] = json.load(f)
    return translations

TRANSLATIONS = load_translations()
DEFAULT_LANG = "ru"
SUPPORTED_LANGUAGES = list(TRANSLATIONS.keys())

def get_string(key: str, lang: str = DEFAULT_LANG) -> str:
    """Получает строку перевода по ключу и языку."""
    return TRANSLATIONS.get(lang, {}).get(key, TRANSLATIONS.get(DEFAULT_LANG, {}).get(key, f"_{key}_"))

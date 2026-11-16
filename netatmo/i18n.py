"""Translation loader for the Netatmo plugin."""

import json
import logging
import os

logger = logging.getLogger(__name__)

I18N_DIR = os.path.join(os.path.dirname(__file__), "i18n")

LANGUAGES = {
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "de": "Deutsch",
}

_CACHE = {}


def _read(lang):
    try:
        with open(os.path.join(I18N_DIR, f"{lang}.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Translation '%s' unreadable: %s", lang, e)
        return {}


def load_translations(lang="en"):
    """Translations for `lang`, falling back to English for any missing keys."""
    if lang not in LANGUAGES:
        lang = "en"
    if lang in _CACHE:
        return _CACHE[lang]

    data = _read("en")
    if lang != "en":
        local = _read(lang)
        data = {**data, **{k: v for k, v in local.items() if k != "labels"}}
        data["labels"] = {**data.get("labels", {}), **local.get("labels", {})}
    _CACHE[lang] = data
    return data

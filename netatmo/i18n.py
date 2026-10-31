"""Chargement des traductions du plugin Netatmo."""

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
        logger.warning("Traduction '%s' illisible : %s", lang, e)
        return {}


def load_translations(lang="en"):
    """Traductions de `lang`, complétées par l'anglais pour les clés manquantes."""
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

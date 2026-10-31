"""Charge le dossier `netatmo/` comme un paquet autonome (sans InkyPi)."""

import importlib.util
import os
import sys

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "netatmo"))


def _load_package(name="netatmo_plugin"):
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = [PLUGIN_DIR]
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [PLUGIN_DIR]
    sys.modules[name] = module


import importlib.machinery  # noqa: E402

_load_package()

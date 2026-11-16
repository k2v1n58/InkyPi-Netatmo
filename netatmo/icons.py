"""Mapping of weather codes and moon phases to PNG icon filenames.

Icons in icons/ match InkyPi's built-in weather plugin for consistent
rendering. Emojis are avoided because they depend on Raspberry Pi fonts
and often render as empty squares.
"""

_CODE_TO_ICON = {
    0: "01d",
    1: "022d",
    2: "02d",
    3: "04d",
    45: "50d",
    48: "48d",
    51: "51d", 61: "51d", 80: "51d",
    53: "53d", 63: "53d", 81: "53d",
    55: "09d", 65: "09d", 82: "09d",
    56: "56d", 66: "56d",
    57: "57d", 67: "57d",
    71: "71d", 85: "71d",
    73: "73d",
    75: "13d", 86: "13d",
    77: "77d",
    95: "11d", 96: "11d", 99: "11d",
}

# Only these icons have night variants.
_NIGHT_VARIANTS = {"01d": "01n", "022d": "022n", "02d": "02n"}

_MOON_THRESHOLDS = [
    (1.0, "newmoon"),
    (7.0, "waxingcrescent"),
    (8.5, "firstquarter"),
    (14.0, "waxinggibbous"),
    (15.5, "fullmoon"),
    (22.0, "waninggibbous"),
    (23.5, "lastquarter"),
    (29.0, "waningcrescent"),
]

_SOUTHERN_SWAP = {
    "waxingcrescent": "waningcrescent",
    "waningcrescent": "waxingcrescent",
    "waxinggibbous": "waninggibbous",
    "waninggibbous": "waxinggibbous",
    "firstquarter": "lastquarter",
    "lastquarter": "firstquarter",
}


def weather_icon_name(wmo_code, is_day=True):
    """WMO code (Open-Meteo) -> icon filename (without extension)."""
    try:
        icon = _CODE_TO_ICON.get(int(wmo_code), "01d")
    except (ValueError, TypeError):
        icon = "01d"
    if not is_day:
        icon = _NIGHT_VARIANTS.get(icon, icon)
    return icon


def moon_icon_name(age_days, latitude=None):
    """Moon age (days) -> icon name, inverted in the Southern hemisphere."""
    name = "newmoon"
    for threshold, phase in _MOON_THRESHOLDS:
        if age_days <= threshold:
            name = phase
            break
    if latitude is not None and latitude < 0:
        name = _SOUTHERN_SWAP.get(name, name)
    return name

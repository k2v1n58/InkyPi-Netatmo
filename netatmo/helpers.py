"""Fonctions utilitaires pures (formatage, unités, lune, point de rosée, Open-Meteo, graphique).

Aucune dépendance à InkyPi ici : ce module est testable de façon isolée.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

import requests

from .icons import weather_icon_name
from .moon import moon_illumination

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

TREND_SYMBOLS = {"up": "↑", "down": "↓", "stable": "→"}

# Unités d'affichage. L'API Netatmo renvoie toujours des valeurs métriques,
# la conversion est faite ici.
UNIT_LABELS = {
    "metric": {"temp": "°C", "wind": "km/h", "pressure": "hPa", "rain": "mm"},
    "imperial": {"temp": "°F", "wind": "mph", "pressure": "inHg", "rain": "in"},
}


# ---------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------

def is_enabled(settings, key, default=False):
    """Interprète une case à cocher du formulaire ('true'/'false', bool, absente)."""
    val = settings.get(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "yes", "on")


def setting_str(settings, key):
    """Valeur texte d'un paramètre, jamais None."""
    val = settings.get(key)
    return str(val).strip() if val is not None else ""


def parse_coordinates(lat, lon):
    """Retourne (lat, lon) en float ou None si invalides / hors limites."""
    try:
        lat_f = float(str(lat).strip().replace(",", "."))
        lon_f = float(str(lon).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return lat_f, lon_f


def station_coordinates(device):
    """Coordonnées (lat, lon) de la station Netatmo, ou None.

    L'API renvoie `place.location` au format GeoJSON : [longitude, latitude].
    """
    location = ((device or {}).get("place") or {}).get("location")
    if isinstance(location, (list, tuple)) and len(location) >= 2:
        return parse_coordinates(location[1], location[0])
    return None


# ---------------------------------------------------------------------------
# Formatage et conversions
# ---------------------------------------------------------------------------

def to_float(val):
    """float(val) ou None."""
    if val is None:
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def fmt_float(val, precision=1, fallback="--"):
    f = to_float(val)
    return fallback if f is None else f"{f:.{precision}f}"


def fmt_int(val, fallback="--"):
    f = to_float(val)
    return fallback if f is None else str(int(round(f)))


def resolve_units(setting, device=None):
    """'metric' ou 'imperial'. 'auto' suit les préférences du compte Netatmo."""
    if setting in ("metric", "imperial"):
        return setting
    admin = (device or {}).get("administrative") or {}
    return "imperial" if admin.get("unit") == 1 else "metric"


def conv_temp(celsius, units):
    c = to_float(celsius)
    if c is None:
        return None
    return c * 9 / 5 + 32 if units == "imperial" else c


def conv_wind(kmh, units):
    v = to_float(kmh)
    if v is None:
        return None
    return v * 0.621371 if units == "imperial" else v


def conv_pressure(hpa, units):
    v = to_float(hpa)
    if v is None:
        return None
    return v * 0.02953 if units == "imperial" else v


def conv_rain(mm, units):
    v = to_float(mm)
    if v is None:
        return None
    return v / 25.4 if units == "imperial" else v


def fmt_pressure(hpa, units):
    v = conv_pressure(hpa, units)
    if v is None:
        return "--"
    return f"{v:.2f}" if units == "imperial" else str(int(round(v)))


def fmt_rain(mm, units):
    v = conv_rain(mm, units)
    if v is None:
        return "--"
    return f"{v:.2f}" if units == "imperial" else f"{v:.1f}"


def co2_status(co2):
    """good / fair / poor / unknown selon la concentration en ppm."""
    c = to_float(co2)
    if c is None:
        return "unknown"
    if c < 800:
        return "good"
    if c <= 1100:
        return "fair"
    return "poor"


def calculate_dew_point(temp_c, humidity):
    """Point de rosée en °C (Magnus-Tetens), None si non calculable."""
    t = to_float(temp_c)
    rh = to_float(humidity)
    if t is None or rh is None or rh <= 0 or rh > 100:
        return None
    a, b = 17.27, 237.7
    alpha = (a * t) / (b + t) + math.log(rh / 100.0)
    return round((b * alpha) / (a - alpha), 1)


def get_cardinal(degrees, cardinals):
    """Angle en degrés -> direction cardinale (16 points)."""
    deg = to_float(degrees)
    if deg is None or deg < 0 or not cardinals or len(cardinals) < 16:  # -1 = vent calme
        return ""
    return cardinals[int((deg % 360 + 11.25) / 22.5) % 16]


# ---------------------------------------------------------------------------
# Lune
# ---------------------------------------------------------------------------

SYNODIC_MONTH = 29.53058770576

def get_moon_phase(dt=None, moon_phases=None):
    """Phase lunaire : nom, illumination (%) et âge en jours."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    illum, elongation, _ = moon_illumination(dt)
    age = elongation / 360.0 * SYNODIC_MONTH

    name = "Moon"
    for item in moon_phases or []:
        name = item.get("name", name)
        if age <= item.get("threshold", 0):
            break
    return {"name": name, "illumination": int(round(illum * 100)), "age": age}


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

def fetch_open_meteo(lat, lon, units="metric", timeout=10):
    """Interroge Open-Meteo. Retourne le JSON brut ou None en cas d'échec."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "hourly": "temperature_2m,precipitation",
        "timezone": "auto",
        "forecast_days": 4,
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
    try:
        res = requests.get(OPEN_METEO_URL, params=params, timeout=timeout)
        res.raise_for_status()
        return res.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.warning("Open-Meteo indisponible : %s", e)
        return None


def parse_forecast(data, days_abbr, weather_codes, today_label="Today"):
    """Transforme le JSON Open-Meteo en jours de prévision.

    Les températures restent dans l'unité demandée à l'API.
    Retourne (liste_de_jours, icône_actuelle_ou_None).
    """
    daily = (data or {}).get("daily") or {}
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    maxs = daily.get("temperature_2m_max") or []
    mins = daily.get("temperature_2m_min") or []
    probs = daily.get("precipitation_probability_max") or []

    def at(seq, i):
        return seq[i] if i < len(seq) else None

    days = []
    for i, date_str in enumerate(times):
        if i == 0:
            label = today_label
        else:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                abbr = days_abbr[dt.weekday()] if days_abbr and dt.weekday() < len(days_abbr) else ""
                label = abbr or date_str
            except ValueError:
                label = date_str
        code = at(codes, i)
        code_str = str(int(code)) if to_float(code) is not None else "0"
        prob = to_float(at(probs, i))
        days.append({
            "date_label": label,
            "weather": weather_codes.get(code_str, ""),
            "icon": weather_icon_name(code_str),
            "temp_min": fmt_int(at(mins, i)),
            "temp_max": fmt_int(at(maxs, i)),
            "rain_prob": int(round(prob)) if prob is not None else None,
        })

    current = (data or {}).get("current") or {}
    code = current.get("weather_code")
    current_icon = None
    if to_float(code) is not None:
        current_icon = weather_icon_name(code, is_day=bool(current.get("is_day", 1)))
    return days, current_icon


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name) if name else None
    except Exception:  # fuseau inconnu ou base tzdata absente
        return None


def _local_to_utc_ts(local, zone, offset):
    """Heure locale naïve -> timestamp UTC, en tenant compte des changements d'heure."""
    if zone is not None:
        return int(local.replace(tzinfo=zone).timestamp())
    return int((local.replace(tzinfo=timezone.utc) - timedelta(seconds=offset)).timestamp())


def upcoming_hourly(data, hours=24):
    """Prévisions horaires à partir de l'heure courante (et non de minuit).

    Retourne (liste de (timestamp_utc, temp), liste de (timestamp_utc, mm)).
    """
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    precs = hourly.get("precipitation") or []
    offset = int((data or {}).get("utc_offset_seconds") or 0)

    now_local = datetime.now(timezone.utc) + timedelta(seconds=offset)
    start = now_local.strftime("%Y-%m-%dT%H:00")

    first = next((i for i, t in enumerate(times) if t >= start), None)
    if first is None:
        return [], []
    zone = _zone((data or {}).get("timezone"))

    temp_pts, precip = [], []
    for i in range(first, min(first + hours, len(times))):
        try:
            local = datetime.strptime(times[i], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        ts = _local_to_utc_ts(local, zone, offset)
        t = to_float(temps[i]) if i < len(temps) else None
        if t is not None:
            temp_pts.append((ts, t))
        p = to_float(precs[i]) if i < len(precs) else None
        if p is not None:
            precip.append((ts, p))
    return temp_pts, precip


# ---------------------------------------------------------------------------
# Graphique
# ---------------------------------------------------------------------------

def merge_current_reading(history, now_ts, current, guard=1800):
    """Raccorde l'historique horaire à la mesure instantanée, placée sur « Now ».

    `getmeasure` (échelle 1hour) renvoie des moyennes horaires : la dernière
    date de 0 à 60 min et diffère de la température affichée sur la carte.
    On retire donc les moyennes des `guard` dernières secondes et on ajoute la
    valeur courante du module à l'instant présent : le point du graphique est
    alors identique à la valeur de la carte et tombe exactement sur « Now ».
    """
    merged = [(ts, v) for ts, v in history if v is not None and ts <= now_ts - guard]
    if current is not None:
        merged.append((now_ts, current))
    return merged


COLOR_PAST = "#d9480f"    # orange : mesures Netatmo
COLOR_FUTURE = "#1c5fd4"  # bleu : prévisions


def build_sparkline(past, future, now_ts, precip=None, width=240, height=90, span_hours=24):
    """Construit le graphique -24h -> maintenant -> +24h.

    past / future : listes de (timestamp_utc, valeur) déjà converties.
    precip        : liste de (timestamp_utc, mm) des pluies prévues (barres).
    Les positions X sont proportionnelles au temps réel. Le SVG est étiré
    (preserveAspectRatio=none) : il ne contient que des traits ; les points et
    les valeurs sont renvoyés en pourcentages (`markers`) pour être posés en
    HTML sans déformation.

    Retourne (svg, markers, valeur_min, valeur_max) ou ("", [], None, None).
    """
    past = [p for p in past if p[1] is not None]
    future = [p for p in future if p[1] is not None and p[0] > now_ts]
    if len(past) + len(future) < 2:
        return "", [], None, None

    values = [v for _, v in past + future]
    vmin, vmax = min(values), max(values)
    vrange = max(vmax - vmin, 1.0)
    pad_x, pad_top, pad_bottom = 4, 16, 16
    span = span_hours * 3600.0
    inner_w, inner_h = width - 2 * pad_x, height - pad_top - pad_bottom

    def ratio(ts):
        return min(max((ts - (now_ts - span)) / (2 * span), 0.0), 1.0)

    def xy(ts, val):
        x = pad_x + ratio(ts) * inner_w
        y = pad_top + (1 - (val - vmin) / vrange) * inner_h
        return round(x, 1), round(y, 1)

    stroke = 'vector-effect="non-scaling-stroke"'
    parts = []

    # quadrillage : -12h / +12h, et repères min / max
    for hours in (-12, 12):
        gx = round(pad_x + ratio(now_ts + hours * 3600) * inner_w, 1)
        parts.append(
            f'<line x1="{gx}" y1="0" x2="{gx}" y2="{height}" stroke="#d4d4d4" stroke-width="1" {stroke}/>'
        )
    for val in (vmin, vmax):
        _, gy = xy(now_ts, val)
        parts.append(
            f'<line x1="0" y1="{gy}" x2="{width}" y2="{gy}" stroke="#bbb" stroke-width="1" '
            f'stroke-dasharray="1,3" {stroke}/>'
        )

    # pluie prévue : barres bleues le long du bas
    bars = [(ts, mm) for ts, mm in (precip or []) if mm is not None and mm >= 0.2]
    if bars:
        scale = max(max(mm for _, mm in bars), 2.0)
        bar_w = round(inner_w / (2 * span_hours) * 0.8, 2)
        for ts, mm in bars:
            bx, _ = xy(ts, vmin)
            bh = max(round(mm / scale * pad_top * 2.2, 1), 1.5)
            parts.append(
                f'<rect x="{round(bx - bar_w / 2, 2)}" y="{round(height - bh, 1)}" width="{bar_w}" '
                f'height="{bh}" fill="{COLOR_FUTURE}" fill-opacity="0.45"/>'
            )

    now_x = round(pad_x + 0.5 * inner_w, 1)
    parts.append(
        f'<line x1="{now_x}" y1="0" x2="{now_x}" y2="{height}" stroke="#000" '
        f'stroke-width="1" stroke-dasharray="3,2" {stroke}/>'
    )

    past_xy = [xy(ts, v) for ts, v in past]
    future_xy = [xy(ts, v) for ts, v in future]

    if len(past_xy) >= 2:
        line = "M " + " L ".join(f"{x},{y}" for x, y in past_xy)
        area = f"{line} L {past_xy[-1][0]},{height} L {past_xy[0][0]},{height} Z"
        parts.append(f'<path d="{area}" fill="{COLOR_PAST}" fill-opacity="0.16" stroke="none"/>')
        parts.append(
            f'<path d="{line}" fill="none" stroke="{COLOR_PAST}" stroke-width="2.6" '
            f'stroke-linejoin="round" stroke-linecap="round" {stroke}/>'
        )

    if future_xy:
        # la prévision démarre au dernier point mesuré : courbe continue
        seq = ([past_xy[-1]] if past_xy else []) + future_xy
        if len(seq) >= 2:
            line = "M " + " L ".join(f"{x},{y}" for x, y in seq)
            parts.append(
                f'<path d="{line}" fill="none" stroke="{COLOR_FUTURE}" stroke-width="2.4" '
                f'stroke-dasharray="5,4" stroke-linejoin="round" {stroke}/>'
            )

    svg = (
        f'<svg class="sparkline-svg" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )

    # ---- repères HTML : valeur actuelle, maximum et minimum
    def marker(kind, ts, val, decimals):
        x, y = xy(ts, val)
        return {
            "kind": kind,
            "x": round(x / width * 100, 1),
            "y": round(y / height * 100, 1),
            "label": f"{fmt_float(val, decimals)}°",
        }

    markers = []
    now_marker = None
    if past:
        now_marker = marker("now", past[-1][0], past[-1][1], 1)
        # étiquette du côté opposé à la courbe qui arrive sur le point
        dy = past_xy[-1][1] - past_xy[-2][1] if len(past_xy) >= 2 else 0
        low_in_plot = now_marker["y"] > 62  # point près du bas : pas de place dessous
        if dy < -1.5:
            now_marker["pos"] = "upleft"
        elif dy > 1.5:
            now_marker["pos"] = "upright" if low_in_plot else "downleft"
        else:
            now_marker["pos"] = "up"
        markers.append(now_marker)

    for kind, pick in (("max", max), ("min", min)):
        ts, val = pick(reversed(past + future), key=lambda p: p[1])  # égalité : point le plus récent
        m = marker(kind, ts, val, 0)
        if now_marker and abs(m["x"] - now_marker["x"]) < 14 and abs(m["y"] - now_marker["y"]) < 22:
            continue  # trop proche de la valeur actuelle
        markers.append(m)

    return svg, markers, vmin, vmax

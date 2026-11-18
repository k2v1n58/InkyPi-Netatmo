"""Pure utility functions (formatting, units, moon, dew point, Open-Meteo, charts).

No dependencies on InkyPi here: this module is isolated and unit-testable.
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

# Display units. Netatmo API always returns metric values;
# unit conversion is handled here.
UNIT_LABELS = {
    "metric": {"temp": "°C", "wind": "km/h", "pressure": "hPa", "rain": "mm"},
    "imperial": {"temp": "°F", "wind": "mph", "pressure": "inHg", "rain": "in"},
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def is_enabled(settings, key, default=False):
    """Interprets a form checkbox ('true'/'false', bool, missing)."""
    val = settings.get(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "yes", "on")


def setting_str(settings, key):
    """String value of a setting, never None."""
    val = settings.get(key)
    return str(val).strip() if val is not None else ""


def parse_coordinates(lat, lon):
    """Returns (lat, lon) as floats or None if invalid / out of bounds."""
    try:
        lat_f = float(str(lat).strip().replace(",", "."))
        lon_f = float(str(lon).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return lat_f, lon_f


def station_coordinates(device):
    """Coordinates (lat, lon) of the Netatmo station, or None.

    The API returns `place.location` in GeoJSON format: [longitude, latitude].
    """
    location = ((device or {}).get("place") or {}).get("location")
    if isinstance(location, (list, tuple)) and len(location) >= 2:
        return parse_coordinates(location[1], location[0])
    return None


# ---------------------------------------------------------------------------
# Formatting and conversions
# ---------------------------------------------------------------------------

def to_float(val):
    """float(val) or None."""
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
    """'metric' or 'imperial'. 'auto' follows Netatmo account preferences."""
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
    """good / fair / poor / unknown depending on CO2 ppm level."""
    c = to_float(co2)
    if c is None:
        return "unknown"
    if c < 800:
        return "good"
    if c <= 1100:
        return "fair"
    return "poor"


def calculate_dew_point(temp_c, humidity):
    """Dew point in °C (Magnus-Tetens formula), None if calculation fails."""
    t = to_float(temp_c)
    rh = to_float(humidity)
    if t is None or rh is None or rh <= 0 or rh > 100:
        return None
    a, b = 17.27, 237.7
    alpha = (a * t) / (b + t) + math.log(rh / 100.0)
    return round((b * alpha) / (a - alpha), 1)


def feels_like(temp_c, humidity=None, wind_kmh=None):
    """Apparent temperature in °C (wind chill or heat index).

    - Cold and windy (T <= 10 °C, wind > 4.8 km/h): wind chill formula
      used by Environment Canada and NOAA.
    - Hot and humid (T >= 27 °C, RH >= 40%): Rothfusz heat index (NOAA).
    - Otherwise: measured temperature.
    """
    t = to_float(temp_c)
    if t is None:
        return None
    v = to_float(wind_kmh)
    rh = to_float(humidity)
    if v is not None and t <= 10 and v > 4.8:
        p = v ** 0.16
        return 13.12 + 0.6215 * t - 11.37 * p + 0.3965 * t * p
    if rh is not None and t >= 27 and rh >= 40:
        f = t * 9 / 5 + 32
        hi = (-42.379 + 2.04901523 * f + 10.14333127 * rh - 0.22475541 * f * rh
              - 0.00683783 * f * f - 0.05481717 * rh * rh + 0.00122874 * f * f * rh
              + 0.00085282 * f * rh * rh - 0.00000199 * f * f * rh * rh)
        return (hi - 32) * 5 / 9
    return t


def get_cardinal(degrees, cardinals):
    """Angle in degrees -> cardinal wind direction (16 points)."""
    deg = to_float(degrees)
    if deg is None or deg < 0 or not cardinals or len(cardinals) < 16:  # -1 = calm wind
        return ""
    return cardinals[int((deg % 360 + 11.25) / 22.5) % 16]


# ---------------------------------------------------------------------------
# Moon
# ---------------------------------------------------------------------------

SYNODIC_MONTH = 29.53058770576

def get_moon_phase(dt=None, moon_phases=None):
    """Lunar phase: name, illumination (%), and age in days."""
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
    """Queries Open-Meteo. Returns raw JSON or None on failure."""
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
        logger.warning("Open-Meteo unavailable: %s", e)
        return None


def parse_forecast(data, days_abbr, weather_codes, today_label="Today"):
    """Transforms Open-Meteo JSON into forecast day structures.

    Temperatures remain in the units requested from the API.
    Returns (days_list, current_icon_or_None).
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
    except Exception:  # Unknown timezone or tzdata missing
        return None


def _local_to_utc_ts(local, zone, offset):
    """Naive local datetime -> UTC timestamp, taking DST into account."""
    if zone is not None:
        return int(local.replace(tzinfo=zone).timestamp())
    return int((local.replace(tzinfo=timezone.utc) - timedelta(seconds=offset)).timestamp())


def upcoming_hourly(data, hours=24):
    """Hourly forecast starting from current hour (not midnight).

    Returns (list of (timestamp_utc, temp), list of (timestamp_utc, mm)).
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
# Chart / Sparkline
# ---------------------------------------------------------------------------

def merge_current_reading(history, now_ts, current, guard=1800):
    """Bridges hourly history with live instantaneous reading placed at 'Now'.

    `getmeasure` (1hour scale) returns hourly averages: the latest is 0 to 60 min old
    and differs from the real-time temperature shown on cards. We strip averages
    from the last `guard` seconds and append the live module value at the current
    timestamp, ensuring the chart point aligns with the card reading right at 'Now'.
    """
    merged = [(ts, v) for ts, v in history if v is not None and ts <= now_ts - guard]
    if current is not None:
        merged.append((now_ts, current))
    return merged


COLOR_PAST = "#d9480f"    # Orange: Netatmo measured readings
COLOR_FUTURE = "#1c5fd4"  # Blue: Open-Meteo forecasts


def build_sparkline(past, future, now_ts, precip=None, width=240, height=90, span_hours=24):
    """Builds the -24h -> now -> +24h temperature and precipitation sparkline.

    past / future : lists of (timestamp_utc, value) already converted.
    precip        : list of (timestamp_utc, mm) forecast rain bars.
    X positions are proportional to real time. The SVG is stretched
    (preserveAspectRatio=none): it contains lines only; marker points and labels
    are returned as percentages (`markers`) to be rendered in HTML without distortion.

    Returns (svg, markers, min_value, max_value) or ("", [], None, None).
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

    # Gridlines: -12h / +12h, and min / max baselines
    for hours in (-12, 12):
        gx = round(pad_x + ratio(now_ts + hours * 3600) * inner_w, 1)
        parts.append(
            f'<line x1="{gx}" y1="0" x2="{gx}" y2="{height}" stroke="#b8b8b8" stroke-width="1.2" {stroke}/>'
        )
    for val in (vmin, vmax):
        _, gy = xy(now_ts, val)
        parts.append(
            f'<line x1="0" y1="{gy}" x2="{width}" y2="{gy}" stroke="#888" stroke-width="1.2" '
            f'stroke-dasharray="3,3" {stroke}/>'
        )

    # Forecast rain: blue bars along the bottom
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
        f'stroke-width="1.6" stroke-dasharray="4,3" {stroke}/>'
    )

    past_xy = [xy(ts, v) for ts, v in past]
    future_xy = [xy(ts, v) for ts, v in future]

    if len(past_xy) >= 2:
        line = "M " + " L ".join(f"{x},{y}" for x, y in past_xy)
        area = f"{line} L {past_xy[-1][0]},{height} L {past_xy[0][0]},{height} Z"
        parts.append(f'<path d="{area}" fill="{COLOR_PAST}" fill-opacity="0.16" stroke="none"/>')
        parts.append(
            f'<path d="{line}" fill="none" stroke="{COLOR_PAST}" stroke-width="3.4" '
            f'stroke-linejoin="round" stroke-linecap="round" {stroke}/>'
        )

    if future_xy:
        # Forecast starts at the last measured point for a continuous curve
        seq = ([past_xy[-1]] if past_xy else []) + future_xy
        if len(seq) >= 2:
            line = "M " + " L ".join(f"{x},{y}" for x, y in seq)
            parts.append(
                f'<path d="{line}" fill="none" stroke="{COLOR_FUTURE}" stroke-width="3.0" '
                f'stroke-dasharray="5,4" stroke-linejoin="round" {stroke}/>'
            )

    svg = (
        f'<svg class="sparkline-svg" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )

    # HTML markers: current reading, maximum, and minimum
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
        # Place label opposite to incoming curve direction
        dy = past_xy[-1][1] - past_xy[-2][1] if len(past_xy) >= 2 else 0
        low_in_plot = now_marker["y"] > 62  # Point near bottom: no room underneath
        if dy < -1.5:
            now_marker["pos"] = "upleft"
        elif dy > 1.5:
            now_marker["pos"] = "upright" if low_in_plot else "downleft"
        else:
            now_marker["pos"] = "up"
        markers.append(now_marker)

    for kind, pick in (("max", max), ("min", min)):
        ts, val = pick(reversed(past + future), key=lambda p: p[1])  # Tie-breaker: pick most recent point
        m = marker(kind, ts, val, 0)
        if now_marker and abs(m["x"] - now_marker["x"]) < 14 and abs(m["y"] - now_marker["y"]) < 22:
            continue  # Too close to current value marker
        markers.append(m)

    return svg, markers, vmin, vmax

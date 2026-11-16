"""InkyPi plugin: Netatmo weather station + Open-Meteo forecast."""

import logging
import os
import time
from datetime import datetime

import pytz

from plugins.base_plugin.base_plugin import (
    BASE_PLUGIN_RENDER_DIR,
    STATIC_DIR,
    BasePlugin,
)
from utils.app_utils import get_fonts
from utils.image_utils import take_screenshot_html

from . import helpers as h
from .i18n import LANGUAGES, load_translations
from .icons import moon_icon_name
from .budget import CallBudget, DataCache
from .netatmo_api import NetatmoBudgetError, NetatmoClient, NetatmoError

logger = logging.getLogger(__name__)

# --- Netatmo API calls: maximum 4 per hour (including token refresh).
# Target schedule: station ~2.7/h (every 22 min), token ~1 every 3 h, rain 1 every 3 h,
# temperature history catch-up 1 every 6 h. The hard cap is guaranteed by
# CallBudget; these intervals simply prevent hitting it.
MAX_CALLS_PER_HOUR = 4
STATION_INTERVAL = 22 * 60        # ~2.7 calls/h; station only sends new readings every 10 min
TEMP_HISTORY_INTERVAL = 6 * 3600  # in-between, history is supplemented by live station readings
RAIN_HISTORY_INTERVAL = 3 * 3600
STALE_BANNER_AGE = 45 * 60        # "offline" banner shown beyond this age
STALE_MAX_AGE = 3 * 3600          # beyond this, no more cached data: raise error

TREND = h.TREND_SYMBOLS


_MOON_ORDER = ["newmoon", "waxingcrescent", "firstquarter", "waxinggibbous",
               "fullmoon", "waninggibbous", "lastquarter", "waningcrescent"]


def _moon_name(age, moon_phases):
    """Translated name matching the icon (same thresholds, Northern hemisphere)."""
    try:
        return moon_phases[_MOON_ORDER.index(moon_icon_name(age))]["name"]
    except (ValueError, IndexError, KeyError, TypeError):
        return None


class Netatmo(BasePlugin):
    """Displays Netatmo weather station readings, 4-day forecast, and moon phase."""

    def __init__(self, config, **dependencies):
        super().__init__(config, **dependencies)
        plugin_dir = self.get_plugin_dir()
        self._budget = CallBudget(os.path.join(plugin_dir, "call_budget.json"), max_calls=MAX_CALLS_PER_HOUR)
        self._cache = DataCache(os.path.join(plugin_dir, "data_cache.json"))
        self._client = NetatmoClient(
            os.path.join(plugin_dir, "token_cache.json"),
            env_path_finder=self._find_env_path,
            budget=self._budget,
        )

    # ------------------------------------------------------------ InkyPi API

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "Netatmo",
            "expected_key": "NETATMO_CLIENT_ID, NETATMO_CLIENT_SECRET, NETATMO_REFRESH_TOKEN",
        }
        template_params["languages"] = LANGUAGES
        return template_params

    def _find_env_path(self, device_config):
        base_dir = getattr(device_config, "BASE_DIR", None)
        candidates = []
        if base_dir:
            candidates.append(os.path.join(base_dir, "..", ".env"))
        candidates.append(os.path.join(self.get_plugin_dir(), "..", "..", "..", ".env"))
        for path in candidates:
            path = os.path.abspath(path)
            if os.path.isfile(path):
                return path
        return None

    def render_image(self, dimensions, html_file, css_file=None, template_params=None):
        """Like BasePlugin.render_image, but independent of encoding.

        InkyPi's base template lacks a <meta charset>: depending on browser and
        OS, non-ASCII characters (é, °, ₂...) can render incorrectly. All
        non-ASCII characters are therefore converted to HTML entities.
        """
        params = dict(template_params or {})
        css_files = [os.path.join(BASE_PLUGIN_RENDER_DIR, "plugin.css")]
        if css_file:
            css_files.append(os.path.join(self.render_dir, css_file))
        params.update(
            style_sheets=css_files,
            width=dimensions[0],
            height=dimensions[1],
            font_faces=get_fonts(),
            static_dir=STATIC_DIR,
        )
        html = self.env.get_template(html_file).render(params)
        image = take_screenshot_html(html.encode("ascii", "xmlcharrefreplace").decode("ascii"), dimensions)
        if image is None:
            raise RuntimeError("Failed to render the Netatmo display (screenshot failed, see logs).")
        return image

    # ------------------------------------------------------------- Data fetching

    def _fetch_station(self, device_config, station_name):
        """Station data: at most one request every 22 minutes.

        Returns (device, is_stale). Between two requests, or if Netatmo is
        unreachable / call budget reached, the last cached response is reused;
        it is only marked 'offline' after 45 minutes.
        """
        key = f"station:{station_name.strip().lower()}"
        age = self._cache.age(key)
        if age is not None and age < STATION_INTERVAL:
            return self._cache.get(key)["value"], False
        try:
            device = self._client.get_station(device_config, station_name)
            self._cache.put(key, device)
            self._record_live_temperature(device)
            return device, False
        except NetatmoError as e:
            if age is not None and age < STALE_MAX_AGE:
                level = logging.INFO if isinstance(e, NetatmoBudgetError) else logging.WARNING
                logger.log(level, "Netatmo: using cached data (%s)", e)
                return self._cache.get(key)["value"], age > STALE_BANNER_AGE
            raise

    def _next_station_due(self):
        """Timestamp of the next planned station refresh."""
        entries = [e for k, e in ((k, self._cache.get(k)) for k in self._cache.keys()) if k.startswith("station:") and e]
        last = max((float(e["ts"]) for e in entries), default=time.time())
        return max(last + STATION_INTERVAL, time.time())

    def _record_live_temperature(self, device):
        """Appends the outdoor temperature reading to the local 24h history."""
        module = self._first_module(device.get("modules") or [], "NAModule1")
        dash = (module or {}).get("dashboard_data") or {}
        ts, temp = dash.get("time_utc"), h.to_float(dash.get("Temperature"))
        if ts is None or temp is None:
            return
        entry = self._cache.get("live_temp")
        points = [p for p in (entry["value"] if entry else []) if p[0] > time.time() - 86400 and p[0] != ts]
        self._cache.put("live_temp", points + [[int(ts), temp]])

    def _history(self, key, interval, fetch):
        """Cached history, refreshed at most every `interval` seconds.

        The station has priority: history is only requested if, after this
        call, at least one slot remains available at the station's next
        scheduled refresh time. Otherwise it waits; hard cap is guaranteed by CallBudget.
        """
        age = self._cache.age(key)
        if (age is None or age >= interval) and self._budget.remaining() >= 1 \
                and self._budget.remaining_at(self._next_station_due(), extra=1) >= 1:
            points = fetch()
            if points is not None:
                self._cache.put(key, points)
        entry = self._cache.get(key)
        return [tuple(p) for p in entry["value"]] if entry else []

    def _temperature_history(self, device_config, device, module):
        """24h history: Netatmo records (refreshed every 6h) + live local readings."""
        fetched = self._history(
            f"temp_hist:{module.get('_id')}", TEMP_HISTORY_INTERVAL,
            lambda: self._client.get_temperature_history(device_config, device.get("_id"), module.get("_id")))
        live = self._cache.get("live_temp")
        merged = {int(ts): v for ts, v in fetched}
        for ts, v in (live["value"] if live else []):
            # A local reading does not replace an hourly Netatmo average
            if all(abs(ts - other) > 1800 for other in merged):
                merged[int(ts)] = v
        cutoff = time.time() - 86400
        return sorted((ts, v) for ts, v in merged.items() if ts >= cutoff)

    def _rain_history(self, device_config, device, module):
        cutoff = time.time() - 86400
        points = self._history(
            f"rain_hist:{module.get('_id')}", RAIN_HISTORY_INTERVAL,
            lambda: self._client.get_rain_history(device_config, device.get("_id"), module.get("_id")))
        return [(ts, mm) for ts, mm in points if ts >= cutoff]

    @staticmethod
    def _first_module(modules, module_type):
        return next((m for m in modules if m.get("type") == module_type), None)

    @staticmethod
    def _name(settings, key, module, fallback):
        return h.setting_str(settings, key) or (module or {}).get("module_name") or fallback

    # --------------------------------------------------------------- Rendering

    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        lang = settings.get("language")
        lang = lang if lang in LANGUAGES else "en"
        tr = load_translations(lang)
        t = tr.get("labels", {})

        flag = lambda key, default=True: h.is_enabled(settings, key, default)  # noqa: E731
        show_outdoor, show_wind, show_rain = flag("show_outdoor"), flag("show_wind"), flag("show_rain")
        show_moon, show_forecast, show_graph = flag("show_moon"), flag("show_forecast"), flag("show_graph")

        # Coordinates: manual input takes precedence, otherwise use Netatmo station coordinates.
        manual_coords = h.parse_coordinates(settings.get("latitude"), settings.get("longitude"))
        if (h.setting_str(settings, "latitude") or h.setting_str(settings, "longitude")) and not manual_coords:
            raise RuntimeError("Latitude and longitude must be valid numbers (lat -90..90, lon -180..180).")

        try:
            tz = pytz.timezone(device_config.get_config("timezone", default="UTC"))
        except pytz.UnknownTimeZoneError:
            tz = pytz.utc
        now = datetime.now(tz)
        time_fmt = "%H:%M" if device_config.get_config("time_format", default="24h") == "24h" else "%I:%M %p"
        last_refresh_time = now.strftime(f"%Y-%m-%d {time_fmt}")

        # ---- Netatmo
        try:
            device, stale = self._fetch_station(device_config, h.setting_str(settings, "station_name"))
        except NetatmoError as e:
            raise RuntimeError(str(e)) from e
        modules = device.get("modules") or []
        coords = manual_coords or h.station_coordinates(device)
        units = h.resolve_units(h.setting_str(settings, "units") or "auto", device)
        ulabels = h.UNIT_LABELS[units]

        main_dash = device.get("dashboard_data") or {}

        pressure_data = None
        if main_dash.get("Pressure") is not None:
            pressure_data = {
                "value": h.fmt_pressure(main_dash.get("Pressure"), units),
                "trend": TREND.get(main_dash.get("pressure_trend"), ""),
                "trend_dir": main_dash.get("pressure_trend") or "stable",
            }

        # ---- Open-Meteo forecast (single request for cards, graph, and rain)
        forecast, current_icon = [], None
        future_pts, future_precip = [], []
        if coords and (show_forecast or show_graph or show_outdoor or show_rain):
            om = h.fetch_open_meteo(coords[0], coords[1], units)
            if om:
                forecast, current_icon = h.parse_forecast(
                    om, tr.get("days_abbr", []), tr.get("weather_codes", {}), t.get("today", "Today"))
                future_pts, future_precip = h.upcoming_hourly(om)

        # ---- Outdoor
        outdoor, outdoor_module = None, self._first_module(modules, "NAModule1")
        wind_dash = (self._first_module(modules, "NAModule2") or {}).get("dashboard_data") or {}
        if show_outdoor and outdoor_module:
            dash = outdoor_module.get("dashboard_data") or {}
            temp, hum = dash.get("Temperature"), dash.get("Humidity")
            dew = h.calculate_dew_point(temp, hum)
            outdoor = {
                "name": self._name(settings, "name_outdoor", outdoor_module, t.get("outdoor", "Outdoor")),
                "temp": h.fmt_float(h.conv_temp(temp, units)),
                "trend": TREND.get(dash.get("temp_trend"), ""),
                "trend_dir": dash.get("temp_trend") or "stable",
                "min": h.fmt_float(h.conv_temp(dash.get("min_temp"), units)),
                "max": h.fmt_float(h.conv_temp(dash.get("max_temp"), units)),
                "humidity": h.fmt_int(hum),
                "dew_point": h.fmt_float(h.conv_temp(dew, units)),
                "icon": current_icon or (forecast[0]["icon"] if forecast else None),
                "feels_like": h.fmt_float(h.conv_temp(h.feels_like(temp, hum, wind_dash.get("WindStrength")), units)),
                "condition": forecast[0]["weather"] if forecast else "",
            }

        # ---- Wind
        wind = None
        wind_module = self._first_module(modules, "NAModule2")
        if show_wind and wind_module:
            dash = wind_module.get("dashboard_data") or {}
            angle = dash.get("WindAngle")
            wind = {
                "name": self._name(settings, "name_wind", wind_module, t.get("wind", "Wind")),
                "speed": h.fmt_int(h.conv_wind(dash.get("WindStrength"), units)),
                "gust": h.fmt_int(h.conv_wind(dash.get("GustStrength"), units)),
                "dir": h.get_cardinal(angle, tr.get("cardinals", [])),
                "angle": h.to_float(angle),
            }

        # ---- Rain
        rain = None
        rain_module = self._first_module(modules, "NAModule3")
        if show_rain and rain_module:
            dash = rain_module.get("dashboard_data") or {}
            rain_1h = dash.get("sum_rain_1")
            if rain_1h is None:
                rain_1h = dash.get("Rain")
            rain = {
                "name": self._name(settings, "name_rain", rain_module, t.get("rain", "Rain")),
                "rain_1h": h.fmt_rain(rain_1h, units),
                "rain_24h": h.fmt_rain(dash.get("sum_rain_24"), units),
                "forecast_6h": None,
            }
            now_ts_rain = int(time.time())
            if future_precip:
                rain["forecast_6h"] = h.fmt_rain(sum(mm for ts, mm in future_precip if now_ts_rain - 1800 <= ts < now_ts_rain + 6 * 3600), units)

        # ---- Indoor
        indoor = []
        if flag("show_main_indoor"):
            indoor.append(self._indoor_card(
                self._name(settings, "name_main_indoor", device, t.get("indoor", "Indoor")),
                main_dash, units, with_noise=True))
        extra = [m for m in modules if m.get("type") == "NAModule4"]
        for i in range(3):
            if flag(f"show_indoor_{i + 1}") and i < len(extra):
                indoor.append(self._indoor_card(
                    self._name(settings, f"name_indoor_{i + 1}", extra[i], f"{t.get('indoor', 'Indoor')} {i + 1}"),
                    extra[i].get("dashboard_data") or {}, units))

        # ---- Moon
        moon = None
        if show_moon:
            phase = h.get_moon_phase(now, tr.get("moon_phases", []))
            lat = coords[0] if coords else None
            moon = {
                "name": _moon_name(phase["age"], tr.get("moon_phases", [])) or phase["name"],
                "illumination": phase["illumination"],
                "icon": moon_icon_name(phase["age"], lat),
            }

        # ---- Graph -24h / +24h
        sparkline = None
        if show_graph:
            now_ts = int(time.time())
            past = []
            if outdoor_module:
                raw = self._temperature_history(device_config, device, outdoor_module)
                past = h.merge_current_reading(
                    [(ts, h.conv_temp(v, units)) for ts, v in raw],
                    now_ts,
                    h.conv_temp((outdoor_module.get("dashboard_data") or {}).get("Temperature"), units),
                )
            # Measured rain from rain gauge (past) + forecast rain (future), in mm
            measured_rain = []
            if rain_module and show_rain:
                measured_rain = self._rain_history(device_config, device, rain_module)
            # Open-Meteo is already in the requested units: no conversion needed.
            svg, markers, vmin, vmax = h.build_sparkline(
                past, future_pts, now_ts, precip=measured_rain + list(future_precip))
            if svg:
                sparkline = {
                    "svg": svg,
                    "markers": markers,
                    "min": h.fmt_float(vmin, 0),
                    "max": h.fmt_float(vmax, 0),
                    "has_rain": any(mm >= 0.2 for _, mm in future_precip),
                }

        has_today = bool(outdoor or wind or rain or sparkline)
        template_params = {
            "t": t,
            "lang": lang,
            "units": ulabels,
            "stale": stale,
            "show_refresh_time": flag("displayRefreshTime"),
            "last_refresh_time": last_refresh_time,
            "moon": moon,
            "outdoor": outdoor,
            "pressure": pressure_data,
            "wind": wind,
            "rain": rain,
            "sparkline": sparkline,
            "has_today": has_today,
            "forecast": forecast if show_forecast else [],
            "indoor": indoor,
            "icons_dir": self.get_plugin_dir("icons").replace("\\", "/"),
            "plugin_settings": settings,
        }
        theme = h.setting_str(settings, "theme")
        if theme == "poster":
            return self.render_image(dimensions, "template_poster.html", "poster.css", template_params)
        return self.render_image(dimensions, "template.html", "template.css", template_params)

    @staticmethod
    def _indoor_card(name, dash, units, with_noise=False):
        co2 = dash.get("CO2")
        return {
            "name": name,
            "temp": h.fmt_float(h.conv_temp(dash.get("Temperature"), units)),
            "humidity": h.fmt_int(dash.get("Humidity")),
            "co2": h.fmt_int(co2),
            "co2_status": h.co2_status(co2),
            "noise": h.fmt_int(dash.get("Noise")) if with_noise and dash.get("Noise") is not None else None,
            "trend": TREND.get(dash.get("temp_trend"), ""),
            "trend_dir": dash.get("temp_trend") or "stable",
            "min": h.fmt_float(h.conv_temp(dash.get("min_temp"), units)),
            "max": h.fmt_float(h.conv_temp(dash.get("max_temp"), units)),
        }

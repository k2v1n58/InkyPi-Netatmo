from datetime import datetime, timedelta, timezone

import pytest

from netatmo_plugin import helpers as h
from netatmo_plugin.i18n import load_translations
from netatmo_plugin.icons import moon_icon_name, weather_icon_name


def test_is_enabled():
    assert h.is_enabled({}, "x", default=True) is True
    assert h.is_enabled({"x": "false"}, "x", default=True) is False
    assert h.is_enabled({"x": "True"}, "x") is True
    assert h.is_enabled({"x": None}, "x", default=False) is False


def test_parse_coordinates():
    assert h.parse_coordinates("48,85", "2.35") == (48.85, 2.35)
    assert h.parse_coordinates("91", "0") is None
    assert h.parse_coordinates("abc", "0") is None
    assert h.parse_coordinates("", "") is None


def test_formatting_is_safe():
    assert h.fmt_float(None) == "--"
    assert h.fmt_float("x") == "--"
    assert h.fmt_float(float("nan")) == "--"
    assert h.fmt_int(12.6) == "13"
    assert h.fmt_int(None) == "--"


def test_units():
    assert h.resolve_units("auto", {"administrative": {"unit": 1}}) == "imperial"
    assert h.resolve_units("auto", {}) == "metric"
    assert h.resolve_units("metric", {"administrative": {"unit": 1}}) == "metric"
    assert h.conv_temp(100, "imperial") == 212
    assert h.fmt_pressure(1013.25, "imperial") == "29.92"
    assert h.fmt_rain(25.4, "imperial") == "1.00"
    assert h.conv_temp(None, "metric") is None


def test_dew_point_and_co2():
    assert h.calculate_dew_point(20, 50) == pytest.approx(9.3, abs=0.1)
    assert h.calculate_dew_point(20, 0) is None
    assert h.calculate_dew_point(None, 50) is None
    assert [h.co2_status(v) for v in (500, 900, 1500, None)] == ["good", "fair", "poor", "unknown"]


def test_cardinals():
    cardinals = load_translations("en")["cardinals"]
    assert h.get_cardinal(0, cardinals) == "N"
    assert h.get_cardinal(359, cardinals) == "N"
    assert h.get_cardinal(90, cardinals) == "E"
    assert h.get_cardinal(None, cardinals) == ""


def test_moon():
    full = h.get_moon_phase(datetime(2000, 1, 21, 4, 40, tzinfo=timezone.utc))
    assert full["illumination"] >= 99
    new = h.get_moon_phase(datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc))
    assert new["illumination"] <= 1
    # 1er novembre 2026 : 60 % à 0 h UTC (spaceweatherlive.com), 54 % à midi UTC
    assert h.get_moon_phase(datetime(2026, 11, 1, tzinfo=timezone.utc))["illumination"] == 60
    assert h.get_moon_phase(datetime(2026, 11, 1, 12, tzinfo=timezone.utc))["illumination"] == 54
    assert moon_icon_name(14.7) == "fullmoon"
    assert moon_icon_name(3, latitude=48) == "waxingcrescent"
    assert moon_icon_name(3, latitude=-33) == "waningcrescent"


def test_weather_icons():
    assert weather_icon_name(0) == "01d"
    assert weather_icon_name(0, is_day=False) == "01n"
    assert weather_icon_name(95) == "11d"
    assert weather_icon_name("garbage") == "01d"


def test_translations_complete_and_fall_back_to_english():
    en = load_translations("en")
    for lang in ("en", "fr", "es", "it", "de"):
        tr = load_translations(lang)
        assert len(tr["cardinals"]) == 16 and len(tr["days_abbr"]) == 7
        assert set(en["labels"]) <= set(tr["labels"])
    assert load_translations("zz")["labels"]["today"] == en["labels"]["today"]


def _open_meteo():
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(-5, 90)]
    return {
        "utc_offset_seconds": 0,
        "current": {"weather_code": 0, "is_day": 0},
        "daily": {
            "time": [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)],
            "weather_code": [0, 61, 3, 95],
            "temperature_2m_max": [10, 11, 12, 13],
            "temperature_2m_min": [1, 2, 3, 4],
            "precipitation_probability_max": [0, 50, None, 90],
        },
        "hourly": {"time": times, "temperature_2m": list(range(len(times))), "precipitation": [0.5] * len(times)},
    }


def test_parse_forecast():
    tr = load_translations("en")
    days, icon = h.parse_forecast(_open_meteo(), tr["days_abbr"], tr["weather_codes"], "Today")
    assert len(days) == 4 and days[0]["date_label"] == "Today"
    assert days[3]["icon"] == "11d" and days[2]["rain_prob"] is None
    assert icon == "01n"  # ciel dégagé, de nuit
    assert h.parse_forecast(None, [], {})[0] == []


def test_hourly_starts_at_current_hour_not_midnight():
    pts, precip = h.upcoming_hourly(_open_meteo(), hours=24)
    assert len(pts) == 24 and len(precip) == 24
    assert pts[0][1] == 5  # series index 5 = current hour
    now = datetime.now(timezone.utc).timestamp()
    assert abs(pts[0][0] - now) <= 3600


def test_sparkline():
    now = 1_000_000
    past = [(now - 3600 * (24 - i), 10 + i % 5) for i in range(24)]
    fut = [(now + 3600 * (i + 1), 12) for i in range(24)]
    rain = [(now + 3600 * (i + 1), 1.5 if i == 3 else 0.0) for i in range(24)]
    svg, markers, vmin, vmax = h.build_sparkline(past, fut, now, precip=rain)
    assert svg.startswith("<svg") and vmin == 10 and vmax == 14
    assert 'stroke-dasharray="5,4"' in svg
    assert svg.count("<rect") == 1  # single rain bar
    kinds = {m["kind"] for m in markers}
    assert "now" in kinds and all(0 <= m["x"] <= 100 and 0 <= m["y"] <= 100 for m in markers)
    assert h.build_sparkline([], [], now) == ("", [], None, None)
    assert h.build_sparkline([(now, 1)], [], now)[0] == ""


def test_forecast_labels_have_no_date():
    tr = load_translations("en")
    days, _ = h.parse_forecast(_open_meteo(), tr["days_abbr"], tr["weather_codes"], "Today")
    assert all(not any(c.isdigit() for c in d["date_label"]) for d in days)


def test_station_coordinates_use_geojson_order():
    device = {"place": {"location": [18.06, 59.33]}}  # [lon, lat]
    assert h.station_coordinates(device) == (59.33, 18.06)
    assert h.station_coordinates({}) is None
    assert h.station_coordinates({"place": {"location": [18.06]}}) is None
    assert h.station_coordinates({"place": {"location": [999, 999]}}) is None


def test_current_reading_sits_on_now_and_replaces_stale_hourly_average():
    now = 1_000_000
    history = [(now - 7200, 8.0), (now - 3600, 9.0), (now - 600, 11.0)]  # dernière moyenne : 10 min
    merged = h.merge_current_reading(history, now, 9.3)
    assert merged[-1] == (now, 9.3)
    assert (now - 600, 11.0) not in merged  # moyenne trop récente retirée
    assert len(merged) == 3
    _, markers, _, _ = h.build_sparkline(merged, [(now + 3600, 10.0)], now)
    now_marker = next(m for m in markers if m["kind"] == "now")
    assert now_marker["label"] == "9.3°" and now_marker["x"] == 50.0
    assert h.merge_current_reading(history, now, None)[-1][0] <= now - 1800


def test_moon_matches_meeus_reference_example():
    """Meeus, « Astronomical Algorithms », exemple 48.a : 12 avril 1992 0 h TD, k = 0,6786."""
    from netatmo_plugin.moon import moon_illumination

    k, _, waxing = moon_illumination(datetime(1992, 4, 11, 23, 59, 1, tzinfo=timezone.utc))
    assert abs(k - 0.6786) < 0.0005 and waxing


def test_calm_wind_has_no_direction():
    cardinals = load_translations("en")["cardinals"]
    assert h.get_cardinal(-1, cardinals) == ""


def test_forecast_never_starts_before_now():
    now = 1_000_000
    past = [(now - 3600, 5.0), (now, 6.0)]
    future = [(now - 1800, 4.0), (now + 1800, 7.0), (now + 5400, 8.0)]
    svg, _, _, _ = h.build_sparkline(past, future, now)
    # Point at now-30 min is ignored: curve does not go backwards
    assert svg and svg.count("stroke-dasharray=\"5,4\"") == 1
    xs = [float(p.split(",")[0]) for p in svg.split('stroke="#1c5fd4"')[0].rsplit("<path d=\"M ", 1)[1].split('"')[0].replace("L ", "").split()]
    assert xs == sorted(xs)


def test_dst_aware_hourly_timestamps():
    from datetime import datetime as dt
    zone = h._zone("Europe/Paris")
    if zone is None:  # tzdata database missing on this host
        return
    before = h._local_to_utc_ts(dt(2026, 10, 25, 1, 0), zone, 7200)   # CEST (UTC+2)
    after = h._local_to_utc_ts(dt(2026, 10, 25, 4, 0), zone, 7200)    # CET (UTC+1)
    assert after - before == 4 * 3600  # 3 h locales mais 4 h réelles


def test_station_response_not_json_dict_is_a_netatmo_error():
    from netatmo_plugin import netatmo_api as api

    class R:
        status_code = 200

        def json(self):
            return ["unexpected"]

    c = api.NetatmoClient("unused.json")
    c._get = lambda *a, **k: R()
    try:
        c.get_station(None)
    except api.NetatmoError:
        pass
    else:
        raise AssertionError("expected NetatmoError")


def test_feels_like():
    # Wind chill: 0 °C and 20 km/h -> ~ -5.2 °C (Environment Canada official table: -5)
    assert round(h.feels_like(0, 80, 20)) == -5
    # Heat index: 32 °C and 70% -> ~ 41 °C (NOAA table: 106 °F)
    assert 40 <= h.feels_like(32, 70, 0) <= 42
    # Mild conditions: measured temperature
    assert h.feels_like(18, 60, 10) == 18
    assert h.feels_like(None, 50, 10) is None

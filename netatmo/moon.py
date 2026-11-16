"""Illuminated fraction and phase of the Moon, based on Jean Meeus.

Algorithm ("Astronomical Algorithms", 2nd ed.):
  - geocentric position of the Moon: chapter 47, principal terms of
    tables 47.A (longitude, distance) and 47.B (latitude);
  - position of the Sun: chapter 25 (low-precision approximation, ~0.01°);
  - phase angle i and illuminated fraction k = (1 + cos i) / 2: chapter 48.

The terms used compute lunar longitude to ~0.005°, giving an illuminated
fraction accurate to better than 0.1%. No external dependencies.
"""

import math
from datetime import datetime, timezone

AU_KM = 149597870.7

# (D, M, M', F, Σl in 1e-6 degrees, Σr in 1e-3 km) — Meeus, table 47.A
_LON_DIST = (
    (0, 0, 1, 0, 6288774, -20905355), (2, 0, -1, 0, 1274027, -3699111),
    (2, 0, 0, 0, 658314, -2955968), (0, 0, 2, 0, 213618, -569925),
    (0, 1, 0, 0, -185116, 48888), (0, 0, 0, 2, -114332, -3149),
    (2, 0, -2, 0, 58793, 246158), (2, -1, -1, 0, 57066, -152138),
    (2, 0, 1, 0, 53322, -170733), (2, -1, 0, 0, 45758, -204586),
    (0, 1, -1, 0, -40923, -129620), (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755), (2, 0, 0, -2, 15327, 10321),
    (0, 0, 1, 2, -12528, 0), (0, 0, 1, -2, 10980, 79661),
    (4, 0, -1, 0, 10675, -34782), (0, 0, 3, 0, 10034, -23210),
    (4, 0, -2, 0, 8548, -21636), (2, 1, -1, 0, -7888, 24208),
    (2, 1, 0, 0, -6766, 30824), (1, 0, -1, 0, -5163, -8379),
    (1, 1, 0, 0, 4987, -16675), (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445), (4, 0, 0, 0, 3861, -11650),
    (2, 0, -3, 0, 3665, 14403), (0, 1, -2, 0, -2689, -7003),
    (2, 0, -1, 2, -2602, 0), (2, -1, -2, 0, 2390, 10056),
    (1, 0, 1, 0, -2348, 6322), (2, -2, 0, 0, 2236, -9884),
)

# (D, M, M', F, Σb in 1e-6 degrees) — Meeus, table 47.B
_LAT = (
    (0, 0, 0, 1, 5128122), (0, 0, 1, 1, 280602), (0, 0, 1, -1, 277693),
    (2, 0, 0, -1, 173237), (2, 0, -1, 1, 55413), (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573), (0, 0, 2, 1, 17198), (2, 0, 1, -1, 9266),
    (0, 0, 2, -1, 8822), (2, -1, 0, -1, 8216), (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200), (2, 1, 0, -1, -3359),
)


def _julian_centuries(dt_utc):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    jd = dt_utc.timestamp() / 86400.0 + 2440587.5
    return (jd - 2451545.0) / 36525.0


def _moon_position(t):
    """Geocentric longitude, latitude (degrees), and distance (km) of the Moon."""
    lp = 218.3164477 + 481267.88123421 * t
    d = 297.8501921 + 445267.1114034 * t
    m = 357.5291092 + 35999.0502909 * t
    mp = 134.9633964 + 477198.8675055 * t
    f = 93.2720950 + 483202.0175233 * t
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    rad = math.radians

    sl = sr = 0.0
    for cd, cm, cmp_, cf, l_coef, r_coef in _LON_DIST:
        arg = rad(cd * d + cm * m + cmp_ * mp + cf * f)
        ecc = e ** abs(cm)
        sl += l_coef * ecc * math.sin(arg)
        sr += r_coef * ecc * math.cos(arg)

    sb = 0.0
    for cd, cm, cmp_, cf, b_coef in _LAT:
        sb += b_coef * (e ** abs(cm)) * math.sin(rad(cd * d + cm * m + cmp_ * mp + cf * f))

    # Additive perturbation terms (Venus, Jupiter, Earth oblateness)
    a1 = rad(119.75 + 131.849 * t)
    a2 = rad(53.09 + 479264.290 * t)
    a3 = rad(313.45 + 481266.484 * t)
    sl += 3958 * math.sin(a1) + 1962 * math.sin(rad(lp - f)) + 318 * math.sin(a2)
    sb += (-2235 * math.sin(rad(lp)) + 382 * math.sin(a3) + 175 * math.sin(a1 - rad(f))
           + 175 * math.sin(a1 + rad(f)) + 127 * math.sin(rad(lp - mp)) - 115 * math.sin(rad(lp + mp)))

    return (lp + sl / 1e6) % 360, sb / 1e6, 385000.56 + sr / 1000.0


def _sun_position(t):
    """Geometric longitude (degrees) and distance (km) of the Sun."""
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    e = 0.016708634 - 0.000042037 * t
    mr = math.radians(m)
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(mr)
         + (0.019993 - 0.000101 * t) * math.sin(2 * mr) + 0.000289 * math.sin(3 * mr))
    nu = math.radians(m + c)
    r_au = 1.000001018 * (1 - e * e) / (1 + e * math.cos(nu))
    return (l0 + c) % 360, r_au * AU_KM


def moon_illumination(dt_utc):
    """Returns (illuminated fraction 0..1, elongation 0..360°, waxing: bool).

    Elongation is 0° at new moon and 180° at full moon; used to determine phase icon.
    """
    t = _julian_centuries(dt_utc)
    lam, beta, delta = _moon_position(t)
    lam0, r = _sun_position(t)

    dlon = math.radians(lam - lam0)
    cos_psi = math.cos(math.radians(beta)) * math.cos(dlon)
    psi = math.acos(max(-1.0, min(1.0, cos_psi)))
    phase_angle = math.atan2(r * math.sin(psi), delta - r * math.cos(psi))
    fraction = (1 + math.cos(phase_angle)) / 2

    waxing = math.sin(dlon) > 0
    elongation = math.degrees(psi) if waxing else 360 - math.degrees(psi)
    return fraction, elongation, waxing

"""Minimal Netatmo Connect API client (weather station) with OAuth2 handling."""

import json
import logging
import os
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.netatmo.com/oauth2/token"
STATIONS_URL = "https://api.netatmo.com/api/getstationsdata"
MEASURE_URL = "https://api.netatmo.com/api/getmeasure"

ENV_CLIENT_ID = "NETATMO_CLIENT_ID"
ENV_CLIENT_SECRET = "NETATMO_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "NETATMO_REFRESH_TOKEN"

# Only one token refresh at a time: Netatmo invalidates the old refresh_token
# as soon as a new one is issued.
_TOKEN_LOCK = threading.Lock()


class NetatmoError(RuntimeError):
    """Readable error displayed in InkyPi's web interface."""


class NetatmoBudgetError(NetatmoError):
    """Call budget reached (plugin-enforced cap or Netatmo HTTP 429)."""


def _build_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(502, 503, 504),
                  allowed_methods=("GET",))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _api_error_text(res):
    """API error message without leaking raw response content (tokens)."""
    try:
        body = res.json()
        err = body.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        return str(err or body.get("error_description") or "")[:120]
    except ValueError:
        return ""


class NetatmoClient:
    def __init__(self, cache_path, env_path_finder=None, budget=None):
        self._cache_path = cache_path
        self.budget = budget  # CallBudget, or None to disable rate limiting (tests)
        self._find_env_path = env_path_finder
        self._session = _build_session()
        self._token = {}

    # ------------------------------------------------------------------ Cache

    def _load_cache(self):
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("Netatmo token cache unreadable (%s), ignored.", e)
            return {}

    def _save_cache(self):
        tmp = f"{self._cache_path}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._token, f)
            os.replace(tmp, self._cache_path)
        except OSError as e:
            logger.warning("Unable to write Netatmo token cache: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _update_env_refresh_token(self, new_token, device_config):
        """Writes the new refresh_token back to .env. Returns True on success."""
        if not self._find_env_path:
            return False
        try:
            env_path = self._find_env_path(device_config)
            if not env_path:
                return False
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_line = f'{ENV_REFRESH_TOKEN}="{new_token}"\n'
            found = False
            for i, line in enumerate(lines):
                if re.match(rf"^\s*{ENV_REFRESH_TOKEN}\s*=", line):
                    lines[i] = new_line
                    found = True
            if not found:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(new_line)
            tmp = f"{env_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, env_path)
            return True
        except OSError as e:
            logger.warning("Failed to update %s in .env: %s", ENV_REFRESH_TOKEN, e)
            return False

    # ------------------------------------------------------------------ OAuth

    def _credentials(self, device_config):
        def get(key):
            return (device_config.load_env_key(key) or os.getenv(key) or "").strip()

        client_id, secret, refresh = get(ENV_CLIENT_ID), get(ENV_CLIENT_SECRET), get(ENV_REFRESH_TOKEN)
        missing = [k for k, v in ((ENV_CLIENT_ID, client_id), (ENV_CLIENT_SECRET, secret),
                                  (ENV_REFRESH_TOKEN, refresh)) if not v]
        if missing:
            raise NetatmoError(
                f"Netatmo credentials missing: {', '.join(missing)}. "
                "Add them on the API Keys page."
            )
        return client_id, secret, refresh

    def _spend(self):
        """Consumes one call from the budget or raises NetatmoBudgetError."""
        if self.budget is not None and not self.budget.try_acquire():
            wait = self.budget.seconds_until_free()
            raise NetatmoBudgetError(f"Netatmo call budget reached, next call possible in {wait // 60 + 1} min.")

    def _rate_limited(self):
        if self.budget is not None:
            self.budget.exhaust()
        raise NetatmoBudgetError("Netatmo rate limit reached (HTTP 429), using cached data.")

    def _request_token(self, client_id, secret, refresh_token):
        self._spend()
        try:
            return self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": secret,
                },
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            raise NetatmoError(f"Network error while contacting Netatmo: {e}") from e

    def get_access_token(self, device_config, force=False):
        with _TOKEN_LOCK:
            if not self._token:
                self._token = self._load_cache()

            client_id, secret, env_refresh = self._credentials(device_config)

            # The cache is only valid for the same application and as long as
            # the user has not pasted a new refresh_token into .env.
            if (self._token.get("client_id") != client_id
                    or self._token.get("env_refresh_token") != env_refresh):
                self._token = {}

            if (not force and self._token.get("access_token")
                    and self._token.get("expires_at", 0) > time.time() + 60):
                return self._token["access_token"]

            candidates = []
            if self._token.get("refresh_token"):
                candidates.append(self._token["refresh_token"])
            if env_refresh not in candidates:
                candidates.append(env_refresh)

            res = None
            for refresh in candidates:
                res = self._request_token(client_id, secret, refresh)
                if res.status_code == 429:
                    self._rate_limited()
                if res.status_code == 200:
                    used = refresh
                    break
                if res.status_code not in (400, 401, 403):
                    break  # Netatmo outage: no use trying the other token
            else:
                used = None

            if res is None or res.status_code != 200:
                self._token = {}
                code = res.status_code if res is not None else "?"
                hint = ""
                if res is not None and res.status_code in (400, 401, 403):
                    hint = " Generate a new refresh token on dev.netatmo.com (scope read_station)."
                raise NetatmoError(
                    f"Netatmo token refresh failed ({code}: {_api_error_text(res)}).{hint}"
                )

            data = res.json()
            access = data.get("access_token")
            if not access:
                raise NetatmoError("Invalid Netatmo response: no access_token.")
            new_refresh = data.get("refresh_token") or used
            try:
                expires_in = float(data.get("expires_in", 10800))
            except (TypeError, ValueError):
                expires_in = 10800

            env_refresh_after = env_refresh
            if new_refresh != env_refresh and self._update_env_refresh_token(new_refresh, device_config):
                env_refresh_after = new_refresh

            self._token = {
                "access_token": access,
                "refresh_token": new_refresh,
                "expires_at": time.time() + expires_in,
                "client_id": client_id,
                "env_refresh_token": env_refresh_after,
            }
            self._save_cache()
            return access

    # -------------------------------------------------------------------- API

    def _get(self, url, device_config, params=None, timeout=10):
        """Authenticated GET with automatic token refresh on 401/403."""
        token = self.get_access_token(device_config)
        for attempt in (1, 2):
            self._spend()
            try:
                res = self._session.get(
                    url, headers={"Authorization": f"Bearer {token}"},
                    params=params, timeout=timeout,
                )
            except requests.exceptions.RequestException as e:
                raise NetatmoError(f"Network error while contacting Netatmo: {e}") from e
            if res.status_code == 429:
                self._rate_limited()
            if res.status_code in (401, 403) and attempt == 1:
                token = self.get_access_token(device_config, force=True)
                continue
            return res

    def get_station(self, device_config, station_name=""):
        """Returns the dictionary for a station (first match or matching name)."""
        res = self._get(STATIONS_URL, device_config)
        if res.status_code != 200:
            raise NetatmoError(f"Netatmo API error ({res.status_code}): {_api_error_text(res)}")
        try:
            payload = res.json()
            body = payload.get("body") if isinstance(payload, dict) else None
            devices = (body.get("devices") or []) if isinstance(body, dict) else []
        except ValueError as e:
            raise NetatmoError("Invalid Netatmo response (not JSON).") from e
        if not devices:
            raise NetatmoError("No Netatmo weather station found on this account.")

        wanted = station_name.strip().lower()
        if wanted:
            for dev in devices:
                names = (dev.get("station_name"), dev.get("home_name"), dev.get("module_name"))
                if any(n and n.strip().lower() == wanted for n in names):
                    return dev
            logger.warning("Netatmo station '%s' not found, using the first one.", station_name)
        return devices[0]

    def get_temperature_history(self, device_config, device_id, module_id=None, hours=24):
        """Hourly temperatures for the past hours: list of (timestamp, °C)."""
        return self.get_history(device_config, device_id, module_id, "temperature", hours)

    def get_rain_history(self, device_config, device_id, module_id, hours=24):
        """Hourly measured rainfall: list of (timestamp, mm)."""
        return self.get_history(device_config, device_id, module_id, "sum_rain", hours)

    def get_history(self, device_config, device_id, module_id, measure, hours=24):
        """Hourly measurements for `measure` (getmeasure): sorted list of (timestamp, value).

        Returns None on failure (network error, rate limit...) so the caller retains
        cached data, and [] if Netatmo simply has no data.
        """
        if not device_id:
            return []
        end = int(time.time())
        params = {
            "device_id": device_id,
            "scale": "1hour",
            "type": measure,
            "date_begin": end - hours * 3600,
            "date_end": end,
            "optimize": "false",
            "real_time": "true",  # timestamp in the middle of interval
        }
        if module_id:
            params["module_id"] = module_id
        try:
            res = self._get(MEASURE_URL, device_config, params=params, timeout=10)
            if res.status_code != 200:
                logger.warning("Netatmo getmeasure: HTTP %s", res.status_code)
                return None
            payload = res.json()
            body = payload.get("body") if isinstance(payload, dict) else None
        except (NetatmoError, ValueError) as e:
            logger.warning("Netatmo history unavailable: %s", e)
            return None

        points = []
        if isinstance(body, dict):
            for ts, values in body.items():
                try:
                    if isinstance(values, list) and values and values[0] is not None:
                        points.append((int(ts), float(values[0])))
                except (TypeError, ValueError):
                    continue
        return sorted(points)

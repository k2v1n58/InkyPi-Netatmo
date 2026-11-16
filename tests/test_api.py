import json
import time

import pytest
import requests

from netatmo_plugin import netatmo_api as api


class Cfg:
    def __init__(self, env):
        self.env = env

    def load_env_key(self, key):
        return self.env.get(key)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


ENV = {"NETATMO_CLIENT_ID": "id", "NETATMO_CLIENT_SECRET": "secret", "NETATMO_REFRESH_TOKEN": "env-rt"}


@pytest.fixture
def client(tmp_path):
    return api.NetatmoClient(str(tmp_path / "token_cache.json"))


def test_missing_credentials(client):
    with pytest.raises(api.NetatmoError, match="NETATMO_CLIENT_SECRET"):
        client.get_access_token(Cfg({"NETATMO_CLIENT_ID": "x", "NETATMO_REFRESH_TOKEN": "y"}))


def test_refresh_and_cache(client, tmp_path, monkeypatch):
    calls = []

    def post(url, data, timeout):
        calls.append(data["refresh_token"])
        return Resp(200, {"access_token": "AT", "refresh_token": "new-rt", "expires_in": 10800})

    monkeypatch.setattr(client._session, "post", post)
    assert client.get_access_token(Cfg(ENV)) == "AT"
    assert client.get_access_token(Cfg(ENV)) == "AT"  # served from cache
    assert calls == ["env-rt"]
    saved = json.loads((tmp_path / "token_cache.json").read_text())
    assert saved["refresh_token"] == "new-rt"


def test_falls_back_to_env_token_when_cached_one_is_rejected(client, tmp_path, monkeypatch):
    (tmp_path / "token_cache.json").write_text(json.dumps({
        "access_token": "old", "expires_at": 0, "refresh_token": "stale-rt",
        "client_id": "id", "env_refresh_token": "env-rt"}))
    tried = []

    def post(url, data, timeout):
        tried.append(data["refresh_token"])
        if data["refresh_token"] == "stale-rt":
            return Resp(400, {"error": "invalid_grant"})
        return Resp(200, {"access_token": "AT2", "refresh_token": "rt2", "expires_in": 100})

    monkeypatch.setattr(client._session, "post", post)
    assert client.get_access_token(Cfg(ENV)) == "AT2"
    assert tried == ["stale-rt", "env-rt"]


def test_new_env_token_discards_cache(client, tmp_path, monkeypatch):
    (tmp_path / "token_cache.json").write_text(json.dumps({
        "access_token": "cached", "expires_at": time.time() + 9999, "refresh_token": "cached-rt",
        "client_id": "id", "env_refresh_token": "OLD-env-rt"}))
    monkeypatch.setattr(client._session, "post",
                        lambda url, data, timeout: Resp(200, {"access_token": "fresh", "expires_in": 100}))
    assert client.get_access_token(Cfg(ENV)) == "fresh"


def test_token_error_never_leaks_secrets(client, monkeypatch):
    monkeypatch.setattr(client._session, "post",
                        lambda url, data, timeout: Resp(500, {"error": "boom"}))
    with pytest.raises(api.NetatmoError) as e:
        client.get_access_token(Cfg(ENV))
    assert "secret" not in str(e.value) and "env-rt" not in str(e.value)


def test_network_error_is_wrapped(client, monkeypatch):
    def post(*a, **k):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(client._session, "post", post)
    with pytest.raises(api.NetatmoError, match="Network error"):
        client.get_access_token(Cfg(ENV))


def test_env_file_is_updated(tmp_path):
    env = tmp_path / ".env"
    env.write_text('A=1\nNETATMO_REFRESH_TOKEN="old"\nB=2')
    c = api.NetatmoClient(str(tmp_path / "c.json"), env_path_finder=lambda cfg: str(env))
    assert c._update_env_refresh_token("new", None) is True
    assert env.read_text() == 'A=1\nNETATMO_REFRESH_TOKEN="new"\nB=2'


def test_station_selection_and_history(client, monkeypatch):
    devices = [{"_id": "a", "station_name": "Cabin"}, {"_id": "b", "station_name": "Home"}]
    body = {"body": {"devices": devices}}
    monkeypatch.setattr(client, "_get", lambda *a, **k: Resp(200, body))
    assert client.get_station(None)["_id"] == "a"
    assert client.get_station(None, "home")["_id"] == "b"
    assert client.get_station(None, "nope")["_id"] == "a"

    monkeypatch.setattr(client, "_get",
                        lambda *a, **k: Resp(200, {"body": {"200": [3.5], "100": [2.0], "300": [None]}}))
    assert client.get_temperature_history(None, "a", "m") == [(100, 2.0), (200, 3.5)]
    monkeypatch.setattr(client, "_get", lambda *a, **k: Resp(500))
    assert client.get_temperature_history(None, "a") is None  # échec : l'appelant garde son cache

    monkeypatch.setattr(client, "_get", lambda *a, **k: Resp(200, {"body": {"devices": []}}))
    with pytest.raises(api.NetatmoError, match="No Netatmo"):
        client.get_station(None)


# ----------------------------------------------------------------- Call budget

def test_budget_blocks_fifth_call_in_the_hour(tmp_path):
    from netatmo_plugin.budget import CallBudget

    b = CallBudget(str(tmp_path / "b.json"), max_calls=4)
    t0 = 1_000_000
    assert all(b.try_acquire(now=t0 + i * 60) for i in range(4))
    assert not b.try_acquire(now=t0 + 600)
    assert b.seconds_until_free(now=t0 + 600) == 3000 + 1
    assert b.try_acquire(now=t0 + 3601)                      # First call has exited the window
    # Counter persists across restarts
    again = CallBudget(str(tmp_path / "b.json"), max_calls=4)
    assert again.remaining(now=t0 + 3602) == 0


def test_client_respects_budget_and_429(client, monkeypatch, tmp_path):
    from netatmo_plugin.budget import CallBudget

    client.budget = CallBudget(str(tmp_path / "budget.json"), max_calls=4)
    posts, gets = [], []
    monkeypatch.setattr(client._session, "post", lambda url, data, timeout: posts.append(1) or Resp(
        200, {"access_token": "AT", "refresh_token": "rt", "expires_in": 10800}))
    monkeypatch.setattr(client._session, "get", lambda *a, **k: gets.append(1) or Resp(
        200, {"body": {"devices": [{"_id": "x"}]}}))
    for _ in range(3):
        client.get_station(Cfg(ENV))                        # 1 token + 3 stations = 4 calls
    with pytest.raises(api.NetatmoBudgetError):
        client.get_station(Cfg(ENV))
    assert len(posts) + len(gets) == 4

    client.budget = CallBudget(str(tmp_path / "budget2.json"), max_calls=4)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: Resp(429, {"error": {"message": "rate"}}))
    with pytest.raises(api.NetatmoBudgetError, match="429"):
        client.get_station(Cfg(ENV))
    assert client.budget.remaining() == 0                   # No calls remaining until the window resets


def test_remaining_at_keeps_a_slot_for_the_next_station_refresh(tmp_path):
    from netatmo_plugin.budget import CallBudget

    b = CallBudget(str(tmp_path / "b.json"), max_calls=4)
    t0 = 1_000_000
    for dt in (0, 1320, 2640):                               # 3 station readings in 44 min
        assert b.try_acquire(now=t0 + dt)
    now = t0 + 2700
    next_station = t0 + 2640 + 1320
    # Would requesting history now take the last available slot at next refresh? No:
    # at t0+3960, the t0 call has exited the window -> a slot remains
    assert b.remaining_at(next_station, extra=1, now=now) >= 1
    # But not if the next refresh happens before the t0 call expires
    assert b.remaining_at(t0 + 3000, extra=1, now=now) == 0

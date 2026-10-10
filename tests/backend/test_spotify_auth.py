"""Spotify accounts (backend): the redirect listener, the Premium gate and the account store."""
import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from backend.spotify import auth as spotify_auth
from backend.spotify.auth import Account, LinkCancelled, LinkError, NotPremium, SignInExpired, select_account

PREMIUM = Account("u1", "Ada", "premium")
PREMIUM2 = Account("u3", "Cy", "premium")
FREE = Account("u2", "Bob", "free")
TOKEN = {"access_token": "t", "refresh_token": "r", "expires_at": 9e12, "scope": spotify_auth.SCOPES}


# ----------------------------------------------------------- redirect handling

def _redirect_after_open(query: str):
    """An open_url stand-in: plays Spotify by hitting the local redirect with `query`."""
    def open_url(_auth_url):
        def hit():
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:8899/callback?{query}", timeout=2).read()
                    return
                except OSError:
                    threading.Event().wait(0.05)
        threading.Thread(target=hit, daemon=True).start()
    return open_url


def test_wait_for_code_returns_code():
    _redirect_after_open("code=abc&state=s1")(None)
    assert spotify_auth._wait_for_code("http://127.0.0.1:8899/callback", "s1", threading.Event(), 10) == "abc"


@pytest.mark.parametrize("query, message", [
    ("code=abc&state=WRONG", "did not match"),
    ("error=access_denied&state=s1", "not granted"),
])
def test_wait_for_code_rejects_bad_responses(query, message):
    _redirect_after_open(query)(None)
    with pytest.raises(LinkError, match=message):
        spotify_auth._wait_for_code("http://127.0.0.1:8899/callback", "s1", threading.Event(), 10)


def test_wait_for_code_can_be_cancelled():
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(LinkCancelled):
        spotify_auth._wait_for_code("http://127.0.0.1:8899/callback", "s1", cancelled, 10)


# ----------------------------------------------------------- premium gate

@pytest.fixture
def fake_spotify(monkeypatch):
    """Stub out the token exchange and /me; set `account` to choose who signs in."""
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8899/callback")
    monkeypatch.delenv("SPOTIPY_CLIENT_SECRET", raising=False)
    state = {"account": PREMIUM}

    def fake_token(self, code=None, check_cache=True):
        assert self.code_verifier  # PKCE: the verifier, not a client secret, proves the request
        self.cache_handler.save_token_to_cache(dict(TOKEN, scope=self.scope))
        return "t"

    def fake_fetch(_sp):
        if isinstance(state["account"], Exception):
            raise state["account"]
        return state["account"]

    from spotipy.oauth2 import SpotifyPKCE
    monkeypatch.setattr(spotify_auth, "_wait_for_code", lambda *_a: "code")
    monkeypatch.setattr(SpotifyPKCE, "get_access_token", fake_token)
    monkeypatch.setattr(spotify_auth, "fetch_account", fake_fetch)
    return state


def test_premium_account_is_linked_saved_and_active(fake_spotify, store):
    opened = []
    account = spotify_auth.link_account(store, open_url=opened.append)
    assert account == PREMIUM
    assert store.accounts() == [PREMIUM] and store.active() == PREMIUM
    assert store.token_path(PREMIUM.id).exists()
    query = parse_qs(urlparse(opened[0]).query)
    assert query["show_dialog"] == ["true"]  # Spotify lets the user pick the account
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"]
    assert query["client_id"] == ["public-client-id"]
    assert "secret" not in opened[0]
    assert spotify_auth.linked_account(store) == PREMIUM


def test_free_account_is_refused_and_not_saved(fake_spotify, store):
    fake_spotify["account"] = FREE
    with pytest.raises(NotPremium) as info:
        spotify_auth.link_account(store, open_url=lambda _u: None)
    assert info.value.account == FREE
    assert store.accounts() == [] and store.active() is None
    assert not store.token_path(FREE.id).exists()


def test_several_accounts_and_switching(fake_spotify, store):
    spotify_auth.link_account(store, open_url=lambda _u: None)
    fake_spotify["account"] = PREMIUM2
    spotify_auth.link_account(store, open_url=lambda _u: None)
    assert store.accounts() == [PREMIUM, PREMIUM2]
    assert store.active() == PREMIUM2  # the newest link is active

    fake_spotify["account"] = PREMIUM
    assert select_account(store, PREMIUM.id) == PREMIUM
    assert store.active() == PREMIUM


def test_daemon_token_follows_the_active_account(store):
    store.save(PREMIUM, dict(TOKEN, access_token="ada"))
    store.save(PREMIUM2, dict(TOKEN, access_token="cy"))
    handler = store.active_cache_handler()
    assert handler.get_cached_token()["access_token"] == "cy"
    store.set_active(PREMIUM.id)
    assert handler.get_cached_token()["access_token"] == "ada"
    store.log_out()
    assert handler.get_cached_token() is None


def test_log_out_forgets_only_the_active_account(store):
    store.save(PREMIUM, TOKEN)
    store.save(PREMIUM2, TOKEN)
    assert store.log_out() == PREMIUM2
    assert store.accounts() == [PREMIUM]
    assert store.active() is None
    assert not store.token_path(PREMIUM2.id).exists()
    assert store.log_out() is None  # nothing active


def test_select_removes_account_that_lost_premium(fake_spotify, store):
    store.save(PREMIUM, TOKEN)
    fake_spotify["account"] = Account(PREMIUM.id, PREMIUM.display_name, "free")
    with pytest.raises(NotPremium):
        select_account(store, PREMIUM.id)
    assert store.accounts() == []


def test_select_removes_account_with_expired_sign_in(fake_spotify, store):
    store.save(PREMIUM, dict(TOKEN, scope="user-read-private"))  # scopes out of date
    with pytest.raises(SignInExpired):
        select_account(store, PREMIUM.id)
    assert store.accounts() == []


def test_select_works_offline_with_saved_account(fake_spotify, store):
    store.save(PREMIUM, TOKEN)
    store.set_active(None)
    fake_spotify["account"] = requests.ConnectionError("offline")
    assert select_account(store, PREMIUM.id) == PREMIUM
    assert store.active() == PREMIUM


def test_app_settings_need_only_a_client_id(monkeypatch, tmp_config):
    for k in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        monkeypatch.delenv(k, raising=False)
    cfg = json.loads(tmp_config.read_text(encoding="utf-8"))
    cfg["spotify"] = {"client_id": "abc123"}
    tmp_config.write_text(json.dumps(cfg), encoding="utf-8")
    settings = spotify_auth.app_settings(tmp_config)
    assert settings.client_id == "abc123"
    assert settings.redirect_uri == spotify_auth.DEFAULT_REDIRECT_URI

    cfg["spotify"] = {}
    tmp_config.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(LinkError, match="Client ID"):
        spotify_auth.app_settings(tmp_config)


def test_account_without_plan_info_is_linked_as_unconfirmed(fake_spotify, store):
    # Spotify's February 2026 API may omit `product` from /me.
    unknown = Account("u9", "Dee", "unknown")
    fake_spotify["account"] = unknown
    assert spotify_auth.link_account(store, open_url=lambda _u: None) == unknown
    assert unknown.premium_unconfirmed and not unknown.is_premium
    assert store.active() == unknown
    assert spotify_auth.linked_account(store) == unknown


def test_fetch_account_maps_missing_product_to_unknown():
    class Me:
        def current_user(self):
            return {"id": "x", "display_name": "X"}

    assert spotify_auth.fetch_account(Me()).product == "unknown"


def test_live_client_builds_on_the_active_account(monkeypatch, store):
    # spotipy only accepts CacheHandler subclasses; this once crashed the daemon at startup.
    from backend.clock import FakeClock
    from backend.spotify.client import LiveSpotifyClient

    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "public-client-id")
    store.save(PREMIUM, TOKEN)
    client = LiveSpotifyClient(FakeClock(), store)
    assert client.sp.auth_manager.cache_handler.get_cached_token()["access_token"] == "t"

"""Linking Spotify accounts, with a Premium gate.

Spotify only lets Premium accounts control playback (including add-to-queue),
so an account is linked only if it is Premium. Several accounts can be linked
on one computer; one of them is active and receives the queued tracks.

    AccountStore              the linked accounts (data/spotify_accounts/): an
                              index.json with names/plans and the active id,
                              plus one token file per account.
    link_account(store)       browser sign-in -> account picked -> Premium check.
                              The token is saved and the account made active
                              only if it is Premium; otherwise NotPremium is
                              raised and nothing is saved.
    select_account(store, id) re-check a saved account and make it active.
    linked_account(store)     the active account, verified as still Premium.
    store.log_out()           forget the active account on this computer.

Sign-in uses the Authorization Code flow with PKCE, so the app ships no
client secret: each user signs in to their own Spotify account and gets their
own token. The only app setting is the public Client ID of the registered
Spotify app (config/config.json "spotify", overridable with SPOTIPY_CLIENT_ID /
SPOTIPY_REDIRECT_URI in the environment).

All functions are blocking; the UI runs them on a worker thread.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from spotipy.cache_handler import CacheHandler

from backend.spotify.client import SCOPES
from shared.config_io import APP_HOME, DEFAULT_CONFIG_PATH, atomic_write_json, load_config

log = logging.getLogger(__name__)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
DEFAULT_ACCOUNTS_DIR = APP_HOME / "data" / "spotify_accounts"

_DONE_PAGE = (b"<!doctype html><meta charset='utf-8'><title>Ambient Mood</title>"
              b"<body style='font-family:Segoe UI,sans-serif;background:#1c1c22;color:#f2f2f5;"
              b"display:grid;place-items:center;height:90vh'><div style='text-align:center'>"
              b"<h2>You can close this tab</h2><p>Return to Ambient Mood to finish.</p></div>")


@dataclass(frozen=True)
class Account:
    id: str
    display_name: str
    product: str  # "premium", "free", "open", ... or "unknown" (see below)

    @property
    def is_premium(self) -> bool:
        return self.product == "premium"

    @property
    def premium_unconfirmed(self) -> bool:
        """Spotify's 2026 API may omit `product`. Such an account is linked, and the first
        playback command decides (403 PREMIUM_REQUIRED = not Premium)."""
        return self.product == "unknown"

    @property
    def may_link(self) -> bool:
        return self.is_premium or self.premium_unconfirmed


class LinkError(Exception):
    """Sign-in could not be completed (denied, timed out, no Client ID...)."""


class LinkCancelled(LinkError):
    pass


class NotPremium(LinkError):
    def __init__(self, account: Account):
        super().__init__(f"{account.display_name} does not have Spotify Premium")
        self.account = account


class SignInExpired(LinkError):
    def __init__(self, account: Account):
        super().__init__(f"the saved sign-in for {account.display_name} has expired; please connect it again")
        self.account = account


class NotLinked(LinkError):
    """No active account (e.g. the user logged out while the daemon runs)."""


# --------------------------------------------------------------------------- store

class AccountStore:
    def __init__(self, root: Path | str = DEFAULT_ACCOUNTS_DIR):
        self.root = Path(root)
        self.index_path = self.root / "index.json"

    def _read(self) -> dict:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"active": None, "accounts": {}}
        data.setdefault("accounts", {})
        return data

    def _write(self, data: dict) -> None:
        atomic_write_json(self.index_path, data)

    def token_path(self, account_id: str) -> Path:
        return self.root / f"{account_id}.token.json"

    def accounts(self) -> list[Account]:
        """Linked accounts that still have a saved token, in the order they were linked."""
        return [Account(**a) for a in self._read()["accounts"].values() if self.token_path(a["id"]).exists()]

    def get(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts() if a.id == account_id), None)

    def active_id(self) -> str | None:
        return self._read().get("active")

    def active(self) -> Account | None:
        active = self.active_id()
        return self.get(active) if active else None

    def save(self, account: Account, token_info: dict) -> None:
        """Store (or refresh) an account's token and make it the active account."""
        atomic_write_json(self.token_path(account.id), token_info)
        data = self._read()
        data["accounts"][account.id] = asdict(account)
        data["active"] = account.id
        self._write(data)

    def set_active(self, account_id: str | None) -> None:
        data = self._read()
        data["active"] = account_id
        self._write(data)

    def remove(self, account_id: str) -> None:
        self.token_path(account_id).unlink(missing_ok=True)
        data = self._read()
        data["accounts"].pop(account_id, None)
        if data.get("active") == account_id:
            data["active"] = None
        self._write(data)

    def log_out(self) -> Account | None:
        """Forget the active account's sign-in on this computer. Returns it."""
        account = self.active()
        if account:
            self.remove(account.id)
        return account

    def cache_handler(self, account_id: str):
        from spotipy.cache_handler import CacheFileHandler

        return CacheFileHandler(cache_path=str(self.token_path(account_id)))

    def active_cache_handler(self) -> "ActiveAccountCacheHandler":
        return ActiveAccountCacheHandler(self)


class ActiveAccountCacheHandler(CacheHandler):
    """A spotipy cache handler that always follows the store's active account.

    The long-running daemon uses this, so switching accounts in the UI moves
    the queue to the new account without a restart, and logging out stops it.
    """

    def __init__(self, store: AccountStore):
        self.store = store

    def get_cached_token(self):
        account = self.store.active()
        return self.store.cache_handler(account.id).get_cached_token() if account else None

    def save_token_to_cache(self, token_info):
        if account := self.store.active():
            self.store.cache_handler(account.id).save_token_to_cache(token_info)


# --------------------------------------------------------------------------- app settings

@dataclass(frozen=True)
class AppSettings:
    client_id: str
    redirect_uri: str


def app_settings(config_path: Path | str = DEFAULT_CONFIG_PATH) -> AppSettings:
    """The Spotify app's public Client ID and redirect URI (no secret needed)."""
    try:
        spotify = load_config(config_path).get("spotify", {})
    except (OSError, ValueError):
        spotify = {}
    client_id = os.environ.get("SPOTIPY_CLIENT_ID") or spotify.get("client_id") or ""
    redirect_uri = os.environ.get("SPOTIPY_REDIRECT_URI") or spotify.get("redirect_uri") or DEFAULT_REDIRECT_URI
    if not client_id.strip():
        raise LinkError('no Spotify Client ID: set "spotify.client_id" in config/config.json')
    return AppSettings(client_id.strip(), redirect_uri.strip())


def make_auth(cache_handler, settings: AppSettings | None = None):
    from spotipy.oauth2 import SpotifyPKCE

    class NoPromptPKCE(SpotifyPKCE):
        # spotipy would otherwise fall back to an interactive prompt when no
        # token is cached, which would hang a background thread.
        def get_authorization_code(self, response=None):
            raise NotLinked("no Spotify account is signed in")

    settings = settings or app_settings()
    return NoPromptPKCE(client_id=settings.client_id, redirect_uri=settings.redirect_uri, scope=SCOPES,
                        cache_handler=cache_handler, open_browser=False, requests_timeout=10)


def fetch_account(sp) -> Account:
    me = sp.current_user()
    return Account(id=me["id"], display_name=me.get("display_name") or me["id"],
                   product=(me.get("product") or "unknown").lower())


# --------------------------------------------------------------------------- sign-in

def _wait_for_code(redirect_uri: str, state: str, cancelled: threading.Event, timeout_s: float) -> str:
    """Serve the OAuth redirect on 127.0.0.1 and return the authorization code."""
    url = urlparse(redirect_uri)
    if url.hostname not in ("127.0.0.1", "localhost") or not url.port:
        raise LinkError(f"the redirect URI must be a local address with a port, got {redirect_uri}")
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            req = urlparse(self.path)
            if req.path != url.path:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in parse_qs(req.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE)

        def log_message(self, *_args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", url.port), Handler)
    except OSError as e:
        raise LinkError(f"port {url.port} is busy; close whatever is using it and try again") from e
    server.timeout = 0.25  # so cancellation is noticed quickly
    try:
        waited = 0.0
        while not result:
            if cancelled.is_set():
                raise LinkCancelled("sign-in cancelled")
            if waited >= timeout_s:
                raise LinkError("timed out waiting for Spotify sign-in")
            server.handle_request()
            waited += server.timeout
    finally:
        server.server_close()

    if result.get("state") != state:
        raise LinkError("sign-in response did not match this request; please try again")
    if "error" in result:
        raise LinkError("access was not granted" if result["error"] == "access_denied"
                        else f"Spotify returned an error: {result['error']}")
    if "code" not in result:
        raise LinkError("Spotify did not return an authorization code")
    return result["code"]


def link_account(store: AccountStore, cancelled: threading.Event | None = None,
                 timeout_s: float = 300.0, open_url=webbrowser.open) -> Account:
    """Sign in through the browser, letting the user choose the account.

    Returns the Account if it is Premium (saved to the store and made active).
    Raises NotPremium for a non-Premium account, in which case nothing is saved.
    """
    import spotipy
    from spotipy.cache_handler import MemoryCacheHandler

    cancelled = cancelled or threading.Event()
    memory = MemoryCacheHandler()
    auth = make_auth(memory)
    state = secrets.token_urlsafe(16)
    # show_dialog makes Spotify show the consent page with "Not you?", so the
    # user can pick which account to link even if the browser is signed in.
    open_url(auth.get_authorize_url(state=state) + "&show_dialog=true")
    code = _wait_for_code(auth.redirect_uri, state, cancelled, timeout_s)
    try:
        auth.get_access_token(code, check_cache=False)
        account = fetch_account(spotipy.Spotify(auth_manager=auth, requests_timeout=10))
    except spotipy.SpotifyException as e:
        raise LinkError(f"Spotify rejected the sign-in: {e.msg}") from e
    except spotipy.oauth2.SpotifyOauthError as e:
        raise LinkError(f"Spotify rejected the sign-in: {e.error_description or e.error}") from e

    if not account.may_link:
        raise NotPremium(account)
    store.save(account, memory.get_cached_token())
    return account


def check_account(store: AccountStore, account_id: str) -> Account:
    """Re-check a saved account against Spotify (refreshing its token).

    Raises SignInExpired if the saved sign-in no longer works and NotPremium
    if the account has lost Premium. Network errors propagate.
    """
    import spotipy

    saved = store.get(account_id)
    if saved is None:
        raise LinkError("that account is no longer linked")
    auth = make_auth(store.cache_handler(account_id))
    try:
        # Refreshes an expired token; None if the cached scopes are out of date.
        if not auth.validate_token(auth.cache_handler.get_cached_token()):
            raise SignInExpired(saved)
    except spotipy.oauth2.SpotifyOauthError as e:
        raise SignInExpired(saved) from e  # refresh token revoked
    account = fetch_account(spotipy.Spotify(auth_manager=auth, requests_timeout=10))
    if not account.may_link:
        raise NotPremium(account)
    return account


def select_account(store: AccountStore, account_id: str) -> Account:
    """Make a saved account active, after checking it is still usable.

    If Spotify can't be reached the saved account is used as-is, so the app
    still starts offline. A non-Premium or expired account is removed.
    """
    import requests

    try:
        account = check_account(store, account_id)
    except (NotPremium, SignInExpired):
        store.remove(account_id)
        raise
    except (requests.RequestException, OSError) as e:
        account = store.get(account_id)
        if account is None:
            raise
        log.warning("could not reach Spotify (%s); using the saved account %s", e, account.display_name)
        store.set_active(account.id)
        return account
    store.save(account, store.cache_handler(account.id).get_cached_token())  # keeps name/plan fresh
    return account


def linked_account(store: AccountStore) -> Account | None:
    """The active account if it is still a usable Premium account, else None.

    Network errors propagate, so callers can tell "offline" from "not linked".
    """
    active = store.active()
    if active is None:
        return None
    try:
        return check_account(store, active.id)
    except (NotPremium, SignInExpired, LinkError):
        return None

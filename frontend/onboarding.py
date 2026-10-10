"""The Spotify account window, shown every time the app starts (and after log out).

    home ─┬─ choose: the accounts linked on this computer, one button each,
          │          plus "Connect another account"
          └─ intro:  (no accounts yet) "Spotify Premium required" + Connect Spotify

    pick an account ──> checking ──┬─ still Premium ───> accept()
                                   ├─ not Premium ─────> not_premium
                                   └─ sign-in expired ─> error (Connect again)
    Connect ──> waiting (browser: user picks an account)
                   ├─ Premium ─────> connected ──Continue──> accept()
                   ├─ not Premium ─> not_premium ──Use a different account──> waiting
                   └─ error ───────> error ──Try again──> waiting

The dialog only accepts once a Premium account is active; closing it any
other way rejects, and the app quits. Spotify calls (`link_fn`, `select_fn`,
by default backend.spotify.auth.link_account / select_account) run on a
worker thread.
"""
from __future__ import annotations

import threading
from typing import Callable

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout,
                             QWidget)

from backend.spotify.auth import Account, AccountStore, LinkCancelled, NotPremium, link_account, select_account
from frontend.components import TEXT

SPOTIFY_GREEN = "#1DB954"

DIALOG_QSS = f"""
QDialog {{ background: #1C1C22; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#Heading {{ font-size: 18px; font-weight: 600; }}
QLabel#Body {{ color: rgba(242, 242, 245, 190); font-size: 12px; }}
QLabel#Badge {{ font-size: 30px; }}
QPushButton {{
    color: {TEXT};
    background: rgba(255, 255, 255, 20);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 16px;
    padding: 8px 18px;
    font-size: 12px;
}}
QPushButton:hover {{ background: rgba(255, 255, 255, 36); }}
QPushButton#Primary {{ background: {SPOTIFY_GREEN}; border: none; color: #0B0B0D; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #1ED760; }}
QPushButton#AccountRow {{
    text-align: left;
    padding: 10px 14px;
    border-radius: 12px;
    font-size: 13px;
}}
QPushButton#AccountRow:hover {{ border: 1px solid {SPOTIFY_GREEN}; }}
"""


class SpotifyWorker(QThread):
    """Runs one Spotify call: fn(cancelled) -> Account."""
    succeeded = pyqtSignal(object)    # Account
    notPremium = pyqtSignal(object)   # Account
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[threading.Event], Account], parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancelled = threading.Event()

    def run(self) -> None:
        try:
            account = self.fn(self.cancelled)
        except NotPremium as e:
            self.notPremium.emit(e.account)
        except LinkCancelled:
            pass
        except Exception as e:
            self.failed.emit(str(e))
        else:
            self.succeeded.emit(account)


class Page(QWidget):
    def __init__(self, badge: str, heading: str, body: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.badge = QLabel(badge)
        self.badge.setObjectName("Badge")
        self.heading = QLabel(heading)
        self.heading.setObjectName("Heading")
        self.heading.setWordWrap(True)
        self.body = QLabel(body)
        self.body.setObjectName("Body")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        for w in (self.badge, self.heading, self.body):
            lay.addWidget(w)
        self.extra = QVBoxLayout()
        self.extra.setSpacing(8)
        lay.addLayout(self.extra)
        lay.addStretch(1)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        lay.addLayout(self.buttons)

    def add_button(self, text: str, slot: Callable, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            b.setObjectName("Primary")
            b.setDefault(True)
        b.clicked.connect(slot)
        self.buttons.addWidget(b)
        return b


class SpotifyAccountDialog(QDialog):
    def __init__(self, store: AccountStore, link_fn: Callable[..., Account] = link_account,
                 select_fn: Callable[..., Account] = select_account, parent=None):
        super().__init__(parent)
        self.store = store
        self.link_fn = link_fn
        self.select_fn = select_fn
        self.account: Account | None = None
        self.worker: SpotifyWorker | None = None
        self.account_buttons: dict[str, QPushButton] = {}

        self.setWindowTitle("Ambient Mood - Spotify account")
        self.setStyleSheet(DIALOG_QSS)
        self.setFixedWidth(440)
        self.setMinimumHeight(300)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self.choose = Page("♫", "Choose a Spotify account",
                           "These accounts are linked on this computer. Songs Ambient Mood picks will be "
                           "added to the queue of the account you choose (Spotify Premium required).")
        self.choose.add_button("Quit", self.reject)
        self.add_btn = self.choose.add_button("Connect another account", self.start_link)

        self.intro = Page(
            "♫", "Spotify Premium required",
            "Ambient Mood adds songs to your Spotify queue to steer your mood. Spotify only allows "
            "this for <b>Premium</b> accounts.<br><br>Click <b>Connect Spotify</b>, then choose the "
            "account to use in your browser. We'll check that it has Premium before linking it.")
        self.intro.add_button("Quit", self.reject)
        self.connect_btn = self.intro.add_button("Connect Spotify", self.start_link, primary=True)

        self.waiting = Page("…", "Waiting for Spotify",
                            "Choose your account and approve access in the browser window that just "
                            "opened. Use <b>Not you?</b> there to switch to another account.")
        self.waiting.add_button("Cancel", self.cancel)

        self.checking = Page("…", "Checking your account")
        self.checking.add_button("Cancel", self.cancel)

        self.connected = Page("✓", "Connected")
        self.connected.add_button("Continue", self.accept, primary=True)

        self.not_premium = Page("✕", "You don't have Spotify Premium")
        self.not_premium.add_button("Back", self.go_home)
        self.retry_btn = self.not_premium.add_button("Use a different account", self.start_link, primary=True)

        self.error = Page("!", "Couldn't connect to Spotify")
        self.error.add_button("Back", self.go_home)
        self.error.add_button("Try again", self.start_link, primary=True)

        for page in (self.choose, self.intro, self.waiting, self.checking, self.connected,
                     self.not_premium, self.error):
            self.stack.addWidget(page)
        self.go_home()

    # ------------------------------------------------------------- pages
    def go_home(self) -> None:
        accounts = self.store.accounts()
        if not accounts:
            self.show_page(self.intro)
            return
        while self.choose.extra.count():
            self.choose.extra.takeAt(0).widget().deleteLater()
        self.account_buttons = {}
        last = self.store.active_id()
        for acc in accounts:
            plan = "Spotify Premium" if acc.is_premium else "Premium not confirmed yet"
            tag = plan + ("  ·  last used" if acc.id == last else "")
            b = QPushButton(f"{acc.display_name}\n{tag}")
            b.setObjectName("AccountRow")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, a=acc: self.choose_account(a))
            self.choose.extra.addWidget(b)
            self.account_buttons[acc.id] = b
        self.show_page(self.choose)

    def show_page(self, page: Page) -> None:
        self.stack.setCurrentWidget(page)
        self.adjustSize()

    def _surface(self, page: Page) -> None:
        self.show_page(page)
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------- flow
    def _run(self, fn: Callable[[threading.Event], Account]) -> bool:
        if self.worker and self.worker.isRunning():
            if not self.worker.cancelled.is_set():
                return False
            self.worker.wait(2000)  # a cancelled attempt frees the redirect port within ~0.25 s
        self.worker = SpotifyWorker(fn, self)
        self.worker.notPremium.connect(self.on_not_premium)
        self.worker.failed.connect(self.on_failed)
        return True

    def start_link(self) -> None:
        if not self._run(lambda cancelled: self.link_fn(self.store, cancelled)):
            return
        self.worker.succeeded.connect(self.on_linked)
        self.show_page(self.waiting)
        self.worker.start()

    def choose_account(self, account: Account) -> None:
        if not self._run(lambda _cancelled: self.select_fn(self.store, account.id)):
            return
        self.worker.succeeded.connect(self.on_selected)
        self.checking.body.setText(f"Checking that <b>{_esc(account.display_name)}</b> still has Spotify Premium…")
        self.show_page(self.checking)
        self.worker.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancelled.set()
            # The cancelled call's result is no longer wanted.
            for sig in (self.worker.succeeded, self.worker.notPremium, self.worker.failed):
                sig.disconnect()
        self.go_home()

    def on_linked(self, account: Account) -> None:
        self.account = account
        self.connected.body.setText(f"Linked to <b>{_esc(account.display_name)}</b> (Spotify Premium). "
                                    "Songs Ambient Mood picks will be added to this account's queue.")
        self._surface(self.connected)

    def on_selected(self, account: Account) -> None:
        self.account = account
        self.accept()

    def on_not_premium(self, account: Account) -> None:
        plan = account.product.capitalize() or "a non-Premium plan"
        self.not_premium.body.setText(
            f"The account <b>{_esc(account.display_name)}</b> is on <b>{_esc(plan)}</b>. Ambient Mood can't "
            "add songs to your queue without Spotify Premium, so this account was not connected."
            "<br><br>Upgrade at spotify.com/premium, or sign in with a different account.")
        self._surface(self.not_premium)

    def on_failed(self, message: str) -> None:
        self.error.body.setText(_esc(message[:1].upper() + message[1:]))
        self._surface(self.error)

    def done(self, result: int) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancelled.set()
            self.worker.wait(2000)
        super().done(result)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

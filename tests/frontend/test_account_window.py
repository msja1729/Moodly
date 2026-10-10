"""Spotify accounts (frontend): the account window, and the account + log out in the tray."""
import pytest
from PyQt6.QtCore import Qt

from backend.spotify import auth as spotify_auth
from backend.spotify.auth import Account, LinkCancelled, LinkError, NotPremium
from frontend.onboarding import SpotifyAccountDialog

PREMIUM = Account("u1", "Ada", "premium")
PREMIUM2 = Account("u3", "Cy", "premium")
FREE = Account("u2", "Bob", "free")
TOKEN = {"access_token": "t", "refresh_token": "r", "expires_at": 9e12, "scope": spotify_auth.SCOPES}


# ----------------------------------------------------------- the account window

def _dialog(qtbot, store, link_fn=None, select_fn=None):
    dlg = SpotifyAccountDialog(store, link_fn=link_fn or (lambda *_: PREMIUM),
                               select_fn=select_fn or (lambda _s, account_id: store.get(account_id)))
    qtbot.addWidget(dlg)
    dlg.show()
    return dlg


def test_dialog_without_accounts_starts_by_explaining_premium(qtbot, store):
    dlg = _dialog(qtbot, store)
    assert dlg.stack.currentWidget() is dlg.intro
    assert "Premium" in dlg.intro.heading.text()


def test_dialog_lists_linked_accounts_to_choose_from(qtbot, store):
    store.save(PREMIUM, TOKEN)
    store.save(PREMIUM2, TOKEN)
    dlg = _dialog(qtbot, store)
    assert dlg.stack.currentWidget() is dlg.choose
    assert list(dlg.account_buttons) == [PREMIUM.id, PREMIUM2.id]
    assert "last used" in dlg.account_buttons[PREMIUM2.id].text()

    qtbot.mouseClick(dlg.account_buttons[PREMIUM.id], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: dlg.result() == SpotifyAccountDialog.DialogCode.Accepted)
    assert dlg.account == PREMIUM


def test_dialog_connect_another_account(qtbot, store):
    store.save(PREMIUM, TOKEN)
    dlg = _dialog(qtbot, store, link_fn=lambda *_: PREMIUM2)
    dlg.add_btn.click()
    qtbot.waitUntil(lambda: dlg.stack.currentWidget() is dlg.connected)
    assert "Cy" in dlg.connected.body.text()
    dlg.accept()
    assert dlg.account == PREMIUM2


def test_dialog_free_account_says_no_premium_then_can_switch(qtbot, store):
    accounts = iter([FREE, PREMIUM])

    def link(*_):
        a = next(accounts)
        if not a.is_premium:
            raise NotPremium(a)
        return a

    dlg = _dialog(qtbot, store, link_fn=link)
    dlg.connect_btn.click()
    qtbot.waitUntil(lambda: dlg.stack.currentWidget() is dlg.not_premium)
    assert "don't have Spotify Premium" in dlg.not_premium.heading.text()
    assert "Bob" in dlg.not_premium.body.text()
    assert dlg.account is None

    dlg.retry_btn.click()  # "Use a different account"
    qtbot.waitUntil(lambda: dlg.stack.currentWidget() is dlg.connected)
    assert dlg.account == PREMIUM


def test_dialog_chosen_account_without_premium(qtbot, store):
    store.save(PREMIUM, TOKEN)

    def select(_s, _id):
        raise NotPremium(Account(PREMIUM.id, "Ada", "free"))

    dlg = _dialog(qtbot, store, select_fn=select)
    dlg.account_buttons[PREMIUM.id].click()
    qtbot.waitUntil(lambda: dlg.stack.currentWidget() is dlg.not_premium)
    assert dlg.account is None


def test_dialog_shows_errors(qtbot, store):
    def link(*_):
        raise LinkError("timed out waiting for Spotify sign-in")

    dlg = _dialog(qtbot, store, link_fn=link)
    dlg.connect_btn.click()
    qtbot.waitUntil(lambda: dlg.stack.currentWidget() is dlg.error)
    assert "Timed out" in dlg.error.body.text()


def test_dialog_cancel_returns_home(qtbot, store):
    def link(_store, cancelled):
        cancelled.wait(5)
        raise LinkCancelled()

    dlg = _dialog(qtbot, store, link_fn=link)
    dlg.connect_btn.click()
    assert dlg.stack.currentWidget() is dlg.waiting
    dlg.cancel()
    assert dlg.stack.currentWidget() is dlg.intro
    dlg.worker.wait(2000)
    assert not dlg.worker.isRunning()


# ----------------------------------------------------------- tray: account + log out

@pytest.fixture
def signed_in_tray(qtbot, tmp_config, store):
    from frontend.app import MoodTrayApp

    store.save(PREMIUM2, TOKEN)
    store.save(PREMIUM, TOKEN)
    chosen = {"next": PREMIUM2, "calls": 0}

    def choose(_store):
        chosen["calls"] += 1
        return chosen["next"]

    app = MoodTrayApp(tmp_config, tmp_config.parent / "state.json", poll_ms=60_000,
                      account=PREMIUM, store=store, choose_account=choose)
    qtbot.addWidget(app.window)
    yield app, chosen
    app.timer.stop()
    app.tray.hide()


def test_sidebar_shows_account_in_corner(signed_in_tray):
    app, _ = signed_in_tray
    assert "Ada" in app.panel.account_label.text()
    assert not app.panel.logout.isHidden()
    assert "Ada" in app.tray.toolTip()
    assert app.account_action.text() == "Spotify: Ada"


def test_log_out_forgets_account_and_asks_again(qtbot, signed_in_tray, store):
    app, chosen = signed_in_tray
    qtbot.mouseClick(app.panel.logout, Qt.MouseButton.LeftButton)
    assert chosen["calls"] == 1
    assert store.get(PREMIUM.id) is None  # Ada's sign-in is gone
    assert app.account == PREMIUM2
    assert "Cy" in app.panel.account_label.text()


def test_log_out_then_quit(monkeypatch, signed_in_tray):
    import frontend.app as app_module

    app, chosen = signed_in_tray
    chosen["next"] = None  # user closes the account window
    quits = []

    class FakeQApplication:
        @staticmethod
        def quit():
            quits.append(True)

    monkeypatch.setattr(app_module, "QApplication", FakeQApplication)
    app.log_out()
    assert quits == [True]

"""Offline tests for SurveyCTO console and macOS Keychain authentication."""

from __future__ import annotations

import subprocess

import pytest
import requests

import surveycto_upload as upload


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        url: str = "https://example.test/",
        status_code: int = 200,
        json_body=None,
    ):
        self.text = text
        self.url = url
        self.status_code = status_code
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_body is None:
            raise ValueError("not JSON")
        return self._json_body


class FakeSession:
    def __init__(self, *, get_responses=None, post_responses=None):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_responses.pop(0)


def login_page(csrf="login-csrf"):
    return f"<script>var csrfToken = '{csrf}';</script>"


def test_store_keychain_password_uses_secure_prompt(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(upload.subprocess, "run", fake_run)

    upload.store_keychain_password("server.surveycto.com", "user@example.com")

    command = captured["command"]
    assert command[-1] == "-w"
    assert "-U" in command
    assert "user@example.com" in command
    assert captured["kwargs"] == {"check": False}


def test_read_keychain_password_preserves_significant_whitespace(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=" pass \n", stderr="")

    monkeypatch.setattr(upload.subprocess, "run", fake_run)

    assert upload.read_keychain_password("server.surveycto.com", "user") == " pass "


def test_read_keychain_password_returns_none_when_item_missing(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 44, stdout="", stderr="missing")

    monkeypatch.setattr(upload.subprocess, "run", fake_run)

    assert upload.read_keychain_password("server.surveycto.com", "user") is None


def test_password_login_matches_live_console_contract(monkeypatch):
    session = FakeSession(
        get_responses=[
            FakeResponse(
                text=login_page(),
                url="https://server.surveycto.com/index.html",
            )
        ],
        post_responses=[
            FakeResponse(json_body={"requiresExternalAuth": False}),
            FakeResponse(
                text="<script>var csrfToken = 'console-csrf';</script>",
                url="https://server.surveycto.com/main.html",
            ),
        ],
    )
    monkeypatch.setattr(upload.requests, "Session", lambda: session)

    result = upload.login_with_password(
        "server.surveycto.com", "user@example.com", "secret"
    )

    assert result is session
    assert getattr(result, "_surveycto_auth_source") == "keychain"
    assert [call[0:2] for call in session.calls] == [
        ("GET", "https://server.surveycto.com/index.html"),
        ("POST", "https://server.surveycto.com/users/options"),
        ("POST", "https://server.surveycto.com/login"),
    ]
    login_data = session.calls[-1][2]["data"]
    assert login_data == {
        "username": "user@example.com",
        "password": "secret",
        "csrf_token": "login-csrf",
        "timezoneOffsetMinutes": "0",
    }


def test_password_login_rejects_external_sso(monkeypatch):
    session = FakeSession(
        get_responses=[FakeResponse(text=login_page())],
        post_responses=[
            FakeResponse(json_body={"requiresExternalAuth": True}),
        ],
    )
    monkeypatch.setattr(upload.requests, "Session", lambda: session)

    with pytest.raises(upload.UploadError, match="single sign-on") as exc:
        upload.login_with_password("server.surveycto.com", "user", "secret")

    assert exc.value.exit_code == 1
    assert len(session.calls) == 2


def test_password_login_reports_rejected_credential(monkeypatch):
    session = FakeSession(
        get_responses=[FakeResponse(text=login_page())],
        post_responses=[
            FakeResponse(json_body={"requiresExternalAuth": False}),
            FakeResponse(
                text='<input id="login-password">',
                url="https://server.surveycto.com/index.html",
            ),
        ],
    )
    monkeypatch.setattr(upload.requests, "Session", lambda: session)

    with pytest.raises(upload.UploadError, match="rejected") as exc:
        upload.login_with_password("server.surveycto.com", "user", "wrong")

    assert exc.value.exit_code == 1


def test_load_session_prefers_configured_keychain(monkeypatch):
    marker = FakeSession()
    monkeypatch.setattr(upload, "read_keychain_password", lambda server, user: "pw")
    monkeypatch.setattr(
        upload,
        "login_with_password",
        lambda server, user, password: marker,
    )
    monkeypatch.setattr(
        upload.browser_cookie3,
        "chrome",
        lambda **kwargs: pytest.fail("Chrome should not be read"),
    )

    result = upload.load_session(
        "server.surveycto.com", username="user@example.com"
    )

    assert result is marker


def test_load_session_falls_back_to_chrome_when_keychain_missing(monkeypatch):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("JSESSIONID", "session", domain="server.surveycto.com")
    monkeypatch.setattr(upload, "read_keychain_password", lambda server, user: None)
    monkeypatch.setattr(upload.browser_cookie3, "chrome", lambda **kwargs: jar)

    result = upload.load_session(
        "server.surveycto.com", username="user@example.com"
    )

    assert result.cookies is jar
    assert getattr(result, "_surveycto_auth_source") == "chrome"


def test_fetch_csrf_rejects_redirect_to_login_page():
    session = FakeSession(
        get_responses=[
            FakeResponse(
                text=login_page(),
                url="https://server.surveycto.com/index.html",
            )
        ]
    )
    setattr(session, "_surveycto_auth_source", "chrome")

    with pytest.raises(upload.UploadError, match="not valid") as exc:
        upload.fetch_csrf_token(session, "server.surveycto.com")

    assert exc.value.exit_code == 1


def test_login_only_never_requires_or_uploads_a_form(monkeypatch, capsys):
    session = FakeSession()
    setattr(session, "_surveycto_auth_source", "keychain")
    monkeypatch.setattr(upload, "load_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(upload, "fetch_csrf_token", lambda *args: "csrf-token")
    monkeypatch.setattr(
        upload,
        "upload_form",
        lambda **kwargs: pytest.fail("login-only must not upload"),
    )

    rc = upload.main([
        "--server", "server.surveycto.com",
        "--username", "user@example.com",
        "--login-only",
    ])

    assert rc == 0
    assert "no form uploaded" in capsys.readouterr().out

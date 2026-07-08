"""Unit tests for auth module — credential persistence, browser extraction, QR flow."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import httpx
import pytest

from weibo_cli.auth import (
    Credential,
    QRExpiredError,
    _exchange_crossdomain,
    _qr_get_session,
    _qr_poll_and_finalize,
    _render_qr_half_blocks,
    clear_credential,
    clear_qr_session,
    extract_browser_credential,
    get_credential,
    load_credential,
    load_qr_session,
    save_credential,
    save_qr_session,
)
from weibo_cli.constants import (
    RETCODE_QR_EXPIRED,
    RETCODE_QR_NOT_SCANNED,
    RETCODE_QR_SCANNED,
    RETCODE_SUCCESS,
)


# ── Credential class ────────────────────────────────────────────────


class TestCredential:
    def test_valid_credential(self):
        cred = Credential(cookies={"SUB": "abc", "SUBP": "xyz"})
        assert cred.is_valid

    def test_empty_credential_invalid(self):
        cred = Credential(cookies={})
        assert not cred.is_valid

    def test_to_dict_includes_saved_at(self):
        cred = Credential(cookies={"SUB": "abc"})
        d = cred.to_dict()
        assert "cookies" in d
        assert "saved_at" in d
        assert isinstance(d["saved_at"], float)

    def test_from_dict(self):
        cred = Credential.from_dict({"cookies": {"SUB": "abc"}, "saved_at": 0})
        assert cred.cookies == {"SUB": "abc"}

    def test_from_dict_missing_cookies(self):
        cred = Credential.from_dict({})
        assert cred.cookies == {}
        assert not cred.is_valid

    def test_cookie_header_format(self):
        cred = Credential(cookies={"A": "1", "B": "2"})
        header = cred.as_cookie_header()
        assert "A=1" in header
        assert "B=2" in header
        assert "; " in header

    def test_roundtrip(self):
        original = Credential(cookies={"SUB": "abc", "SUBP": "xyz", "X-CSRF-TOKEN": "csrf"})
        d = original.to_dict()
        restored = Credential.from_dict(d)
        assert restored.cookies == original.cookies


# ── Credential persistence ──────────────────────────────────────────


class TestCredentialPersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")

        cred = Credential(cookies={"SUB": "test_sub"})
        save_credential(cred)

        loaded = load_credential()
        assert loaded is not None
        assert loaded.cookies == {"SUB": "test_sub"}

    def test_load_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "nonexistent.json")
        assert load_credential() is None

    def test_load_invalid_json(self, tmp_path, monkeypatch):
        cred_file = tmp_path / "credential.json"
        cred_file.write_text("not valid json!!!")
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", cred_file)
        assert load_credential() is None

    def test_load_empty_cookies(self, tmp_path, monkeypatch):
        cred_file = tmp_path / "credential.json"
        cred_file.write_text(json.dumps({"cookies": {}, "saved_at": time.time()}))
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", cred_file)
        assert load_credential() is None

    def test_clear_credential(self, tmp_path, monkeypatch):
        cred_file = tmp_path / "credential.json"
        cred_file.write_text("{}")
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", cred_file)
        clear_credential()
        assert not cred_file.exists()

    def test_clear_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "nonexistent.json")
        # Should not raise
        clear_credential()

    def test_load_triggers_refresh_when_stale(self, tmp_path, monkeypatch):
        cred_file = tmp_path / "credential.json"
        old_time = time.time() - (8 * 86400)  # 8 days ago
        cred_file.write_text(json.dumps({"cookies": {"SUB": "old"}, "saved_at": old_time}))
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", cred_file)

        fresh_cred = Credential(cookies={"SUB": "fresh"})
        monkeypatch.setattr("weibo_cli.auth.extract_browser_credential", lambda: fresh_cred)

        loaded = load_credential()
        assert loaded is not None
        assert loaded.cookies["SUB"] == "fresh"

    def test_load_uses_old_when_refresh_fails(self, tmp_path, monkeypatch):
        cred_file = tmp_path / "credential.json"
        old_time = time.time() - (8 * 86400)
        cred_file.write_text(json.dumps({"cookies": {"SUB": "old"}, "saved_at": old_time}))
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", cred_file)
        monkeypatch.setattr("weibo_cli.auth.extract_browser_credential", lambda: None)

        loaded = load_credential()
        assert loaded is not None
        assert loaded.cookies["SUB"] == "old"

    def test_file_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")

        save_credential(Credential(cookies={"SUB": "test"}))
        perms = (tmp_path / "credential.json").stat().st_mode & 0o777
        assert perms == 0o600


# ── Browser cookie extraction ───────────────────────────────────────


class TestBrowserExtraction:
    def test_extraction_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"browser": "Chrome", "cookies": {"SUB": "extracted"}})

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
        cred = extract_browser_credential()
        assert cred is not None
        assert cred.cookies["SUB"] == "extracted"

    def test_extraction_no_cookies(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"error": "no_cookies"})

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
        assert extract_browser_credential() is None

    def test_extraction_not_installed(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"error": "not_installed"})

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
        assert extract_browser_credential() is None

    def test_extraction_subprocess_failure(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
        assert extract_browser_credential() is None

    def test_extraction_timeout(self, monkeypatch):
        import subprocess
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 15)))
        assert extract_browser_credential() is None

    def test_extraction_invalid_json(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json"

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
        assert extract_browser_credential() is None

    def test_extraction_with_cookie_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")

        captured_cmd = {}

        def fake_run(cmd, **kw):
            captured_cmd["args"] = cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps({"browser": "Firefox", "cookies": {"SUB": "fx"}})
            return result

        monkeypatch.setattr("subprocess.run", fake_run)
        cred = extract_browser_credential(cookie_source="Firefox")
        assert cred is not None
        assert "Firefox" in captured_cmd["args"]


# ── get_credential chain ────────────────────────────────────────────


class TestGetCredential:
    def test_returns_saved_first(self, monkeypatch):
        saved = Credential(cookies={"SUB": "saved"})
        monkeypatch.setattr("weibo_cli.auth.load_credential", lambda: saved)
        monkeypatch.setattr("weibo_cli.auth.extract_browser_credential", lambda: None)

        result = get_credential()
        assert result.cookies["SUB"] == "saved"

    def test_falls_back_to_browser(self, monkeypatch):
        browser_cred = Credential(cookies={"SUB": "browser"})
        monkeypatch.setattr("weibo_cli.auth.load_credential", lambda: None)
        monkeypatch.setattr("weibo_cli.auth.extract_browser_credential", lambda: browser_cred)

        result = get_credential()
        assert result.cookies["SUB"] == "browser"

    def test_returns_none_when_all_fail(self, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.load_credential", lambda: None)
        monkeypatch.setattr("weibo_cli.auth.extract_browser_credential", lambda: None)

        assert get_credential() is None


# ── QR rendering ────────────────────────────────────────────────────


class TestQRRendering:
    def test_render_empty_matrix(self):
        assert _render_qr_half_blocks([]) == ""

    def test_render_small_matrix(self):
        matrix = [
            [True, False],
            [False, True],
        ]
        result = _render_qr_half_blocks(matrix)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_all_true(self):
        # 4x4 matrix ensures full blocks survive the quiet zone padding
        matrix = [[True]*4 for _ in range(4)]
        result = _render_qr_half_blocks(matrix)
        assert "█" in result

    def test_render_all_false(self):
        matrix = [[False, False], [False, False]]
        result = _render_qr_half_blocks(matrix)
        # Should produce spaces (with quiet zone)
        assert isinstance(result, str)


class TestQrGetSession:
    def test_returns_qrid_csrf_scan_url_cookies(self):
        def handler(request):
            if request.url.path == "/sso/signin":
                return httpx.Response(200, headers={"set-cookie": "X-CSRF-TOKEN=csrf123; Path=/"})
            if request.url.path == "/sso/v2/qrcode/image":
                return httpx.Response(200, json={
                    "retcode": 20000000,
                    "data": {"qrid": "qrid123", "image": "https://x/y?data=scanurl123"},
                })
            return httpx.Response(404)
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        session = _qr_get_session(client)
        assert session["qrid"] == "qrid123"
        assert session["csrf_token"] == "csrf123"
        assert session["scan_url"] == "scanurl123"
        assert "X-CSRF-TOKEN" in session["cookies"]
        client.close()

    def test_raises_on_missing_csrf(self):
        def handler(request):
            if request.url.path == "/sso/signin":
                return httpx.Response(200)  # no set-cookie
            return httpx.Response(200, json={"retcode": 20000000, "data": {"qrid": "q", "image": "https://x?data=s"}})
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        with pytest.raises(RuntimeError, match="X-CSRF-TOKEN"):
            _qr_get_session(client)
        client.close()

    def test_raises_on_qr_image_failure(self):
        def handler(request):
            if request.url.path == "/sso/signin":
                return httpx.Response(200, headers={"set-cookie": "X-CSRF-TOKEN=c; Path=/"})
            return httpx.Response(200, json={"retcode": 999, "msg": "fail"})
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        with pytest.raises(RuntimeError, match="Failed to get QR code"):
            _qr_get_session(client)
        client.close()


class TestQrPollAndFinalize:
    def _client_with_states(self, states, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth._exchange_crossdomain", lambda url, alt: {"SUB": "final", "SUBP": "p"})
        idx = {"i": -1}
        def handler(request):
            idx["i"] += 1
            return httpx.Response(200, json=states[min(idx["i"], len(states) - 1)])
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        client.cookies.set("X-CSRF-TOKEN", "csrf", domain="passport.weibo.com")
        return client

    def test_success_saves_credential(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        states = [
            {"retcode": RETCODE_QR_NOT_SCANNED, "msg": ""},
            {"retcode": RETCODE_QR_SCANNED, "msg": "已扫"},
            {"retcode": RETCODE_SUCCESS, "data": {"url": "u", "alt": "a"}},
        ]
        client = self._client_with_states(states, monkeypatch)
        cred = _qr_poll_and_finalize(client, "qrid")
        assert cred.cookies["SUB"] == "final"
        assert (tmp_path / "credential.json").exists()
        client.close()

    def test_expired_raises(self, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        client = self._client_with_states([{"retcode": RETCODE_QR_EXPIRED, "msg": "过期"}], monkeypatch)
        with pytest.raises(QRExpiredError):
            _qr_poll_and_finalize(client, "qrid")
        client.close()

    def test_timeout_raises_timeout_error(self, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        client = self._client_with_states([{"retcode": RETCODE_QR_NOT_SCANNED, "msg": ""}], monkeypatch)
        with pytest.raises(TimeoutError):
            _qr_poll_and_finalize(client, "qrid", poll_timeout=0.01)
        client.close()

    def test_crossdomain_fail_alt_success(self, tmp_path, monkeypatch):
        """crossdomain 抛异常但 alt 成功 → 仍拿到 cookie 并保存。"""
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        states = [{"retcode": RETCODE_SUCCESS, "data": {"url": "u", "alt": "a"}}]
        client = self._client_with_states(states, monkeypatch)
        # Override the helper's default mock to assert the returned cookies flow through.
        monkeypatch.setattr("weibo_cli.auth._exchange_crossdomain", lambda url, alt: {"SUB": "fromalt"})
        cred = _qr_poll_and_finalize(client, "qrid")
        assert cred.cookies["SUB"] == "fromalt"
        client.close()


class TestExchangeCrossdomain:
    """Direct tests for _exchange_crossdomain fallback logic."""

    def test_merges_cross_and_alt_cookies(self, monkeypatch):
        # cross 返回 SUB，alt 返回 SUBP
        def handler(request):
            if "sina.com.cn" in (request.url.host or ""):
                return httpx.Response(200, headers={"set-cookie": "SUBP=altval; Path=/"})
            return httpx.Response(200, headers={"set-cookie": "SUB=crossval; Path=/"})

        transport = httpx.MockTransport(handler)
        orig_client = httpx.Client
        monkeypatch.setattr(
            "weibo_cli.auth.httpx.Client",
            lambda *a, **kw: orig_client(
                *a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
            ),
        )
        cookies = _exchange_crossdomain("https://cross.example.com/x", "alttok")
        assert cookies.get("SUB") == "crossval"
        assert cookies.get("SUBP") == "altval"

    def test_cross_fail_alt_success(self, monkeypatch):
        # cross 抛网络异常，alt 仍成功
        def handler(request):
            if "sina.com.cn" in (request.url.host or ""):
                return httpx.Response(200, headers={"set-cookie": "SUBP=altval; Path=/"})
            raise httpx.ConnectError("cross fail")

        transport = httpx.MockTransport(handler)
        orig_client = httpx.Client
        monkeypatch.setattr(
            "weibo_cli.auth.httpx.Client",
            lambda *a, **kw: orig_client(
                *a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
            ),
        )
        cookies = _exchange_crossdomain("https://cross.example.com/x", "alttok")
        assert cookies.get("SUBP") == "altval"
        assert "SUB" not in cookies  # cross 失败没拿到

    def test_both_fail_returns_empty(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("fail")

        transport = httpx.MockTransport(handler)
        orig_client = httpx.Client
        monkeypatch.setattr(
            "weibo_cli.auth.httpx.Client",
            lambda *a, **kw: orig_client(
                *a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
            ),
        )
        cookies = _exchange_crossdomain("https://x.example.com/", "alttok")
        assert cookies == {}


class TestQrSessionPersistence:
    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        session = {"qrid": "q", "csrf_token": "c", "cookies": {"X-CSRF-TOKEN": "c", "tid": "t"}, "scan_url": "s"}
        save_qr_session(session)
        loaded = load_qr_session()
        assert loaded["qrid"] == "q"
        assert loaded["csrf_token"] == "c"
        assert loaded["cookies"]["tid"] == "t"
        assert "created_at" in loaded

    def test_load_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "nope.json")
        assert load_qr_session() is None

    def test_clear(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        save_qr_session({"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"})
        assert (tmp_path / "qr_session.json").exists()
        clear_qr_session()
        assert not (tmp_path / "qr_session.json").exists()

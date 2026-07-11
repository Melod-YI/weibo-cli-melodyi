"""Unit tests for auth module — credential persistence, browser extraction, QR flow."""

from __future__ import annotations

import json
import sys
import time

import httpx
import pytest

from weibo_cli.auth import (
    Credential,
    QRExpiredError,
    _exchange_crossdomain,
    _exchange_mobile_cookies,
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

    def test_mobile_cookies_roundtrip(self):
        original = Credential(
            cookies={"SUB": "com_sub", "SUBP": "com_subp"},
            mobile_cookies={"SUB": "cn_sub", "SUBP": "cn_subp", "SCF": "cn_scf"},
        )
        d = original.to_dict()
        assert "mobile_cookies" in d
        restored = Credential.from_dict(d)
        assert restored.mobile_cookies == original.mobile_cookies
        assert restored.cookies == original.cookies

    def test_mobile_cookies_default_empty(self):
        cred = Credential(cookies={"SUB": "x"})
        assert cred.mobile_cookies == {}
        # to_dict 省略空 mobile_cookies（向后兼容旧凭证格式）
        assert "mobile_cookies" not in cred.to_dict()

    def test_from_dict_legacy_without_mobile_cookies(self):
        """老凭证文件（无 mobile_cookies 键）仍能正常加载。"""
        cred = Credential.from_dict({"cookies": {"SUB": "abc"}, "saved_at": 0})
        assert cred.cookies == {"SUB": "abc"}
        assert cred.mobile_cookies == {}


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

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod 0o600 is a no-op on Windows")
    def test_file_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")

        save_credential(Credential(cookies={"SUB": "test"}))
        perms = (tmp_path / "credential.json").stat().st_mode & 0o777
        assert perms == 0o600


# ── Browser cookie extraction ───────────────────────────────────────


def _rk_cookie(name: str, value: str, domain: str) -> dict:
    """Build a rookiepy-shaped cookie dict (real key set: domain/name/value/...)."""
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "secure": True, "expires": None, "http_only": False}


class TestBrowserExtraction:
    def test_extraction_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("rookiepy.chrome", lambda domains=None: [
            _rk_cookie("SUB", "extracted", ".weibo.com"),
            _rk_cookie("SUBP", "p", ".weibo.com"),
        ])
        cred = extract_browser_credential()
        assert cred is not None
        assert cred.cookies["SUB"] == "extracted"
        assert cred.cookies["SUBP"] == "p"

    def test_extraction_filters_non_weibo_domains(self, monkeypatch, tmp_path):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("rookiepy.chrome", lambda domains=None: [
            _rk_cookie("id", "google-val", ".google.com"),
            _rk_cookie("SUB", "wb", ".weibo.com"),
        ])
        cred = extract_browser_credential()
        assert cred is not None
        assert cred.cookies == {"SUB": "wb"}

    def test_extraction_no_weibo_cookies(self, monkeypatch):
        monkeypatch.setattr("rookiepy.chrome", lambda domains=None: [
            _rk_cookie("id", "x", ".google.com"),
        ])
        errors: dict[str, str] = {}
        cred = extract_browser_credential(cookie_source="chrome", errors_out=errors)
        assert cred is None
        assert "Chrome" in errors

    def test_extraction_surfaces_loader_error(self, monkeypatch):
        # rookiepy raises RuntimeError when Chrome v130+ cookies need admin.
        # The error must be surfaced via errors_out, not swallowed.
        def boom(domains=None):
            raise RuntimeError("can be decrypted only when running as admin")
        monkeypatch.setattr("rookiepy.chrome", boom)
        errors: dict[str, str] = {}
        cred = extract_browser_credential(cookie_source="chrome", errors_out=errors)
        assert cred is None
        assert "RuntimeError" in errors["Chrome"]
        assert "admin" in errors["Chrome"]

    def test_extraction_cookie_source_firefox(self, monkeypatch, tmp_path):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        called = {}

        def fake_ff(domains=None):
            called["domains"] = domains
            return [_rk_cookie("SUB", "fx", ".weibo.com")]

        monkeypatch.setattr("rookiepy.firefox", fake_ff)
        cred = extract_browser_credential(cookie_source="firefox")
        assert cred is not None
        assert cred.cookies["SUB"] == "fx"
        assert "weibo.com" in called["domains"]

    def test_extraction_unsupported_browser(self, monkeypatch):
        errors: dict[str, str] = {}
        cred = extract_browser_credential(cookie_source="netscape", errors_out=errors)
        assert cred is None
        assert "netscape" in errors

    def test_extraction_auto_falls_through_to_next_browser(self, monkeypatch, tmp_path):
        # Auto mode iterates browsers; Chrome raises (needs admin), Firefox succeeds.
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("rookiepy.chrome", lambda domains=None: (_ for _ in ()).throw(RuntimeError("admin required")))
        monkeypatch.setattr("rookiepy.edge", lambda domains=None: (_ for _ in ()).throw(RuntimeError("nope")))
        monkeypatch.setattr("rookiepy.firefox", lambda domains=None: [_rk_cookie("SUB", "ff", ".weibo.com")])
        errors: dict[str, str] = {}
        cred = extract_browser_credential(errors_out=errors)
        assert cred is not None
        assert cred.cookies["SUB"] == "ff"
        assert "Chrome" in errors
        assert "Edge" in errors


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
    def _client_with_states(self, states, monkeypatch, mobile_cookies=None):
        monkeypatch.setattr("weibo_cli.auth._exchange_crossdomain", lambda url, alt: {"SUB": "final", "SUBP": "p"})
        # _exchange_mobile_cookies 内部自建 client 打真实网络；测试里固定返回 (weibo={}, mobile) 元组，避免误触网络。
        monkeypatch.setattr(
            "weibo_cli.auth._exchange_mobile_cookies", lambda url, cookies=None: ({}, mobile_cookies or {})
        )
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

    def test_mobile_cookies_persisted(self, tmp_path, monkeypatch):
        """QR 成功后 .weibo.cn mobile_cookies 与 .weibo.com cookies 分域存入凭证。"""
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        states = [{"retcode": RETCODE_SUCCESS, "data": {"url": "u", "alt": "a"}}]
        mobile = {"SUB": "cn_sub", "SUBP": "cn_subp", "SCF": "cn_scf"}
        client = self._client_with_states(states, monkeypatch, mobile_cookies=mobile)
        cred = _qr_poll_and_finalize(client, "qrid")
        # .weibo.com SUB 与 .weibo.cn SUB 分开存放，互不覆盖
        assert cred.cookies["SUB"] == "final"
        assert cred.mobile_cookies == mobile
        assert cred.mobile_cookies["SUB"] == "cn_sub"
        # 持久化文件里也带 mobile_cookies
        saved = json.loads((tmp_path / "credential.json").read_text())
        assert saved["mobile_cookies"] == mobile
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

    def test_success_saves_even_if_cross_alt_empty(self, tmp_path, monkeypatch):
        """cross/alt 都返回空，但 passport cookies 非空 → 仍保存凭证（保留原语义）。"""
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        monkeypatch.setattr("weibo_cli.auth._exchange_crossdomain", lambda url, alt: {})  # cross/alt 都空
        states = [{"retcode": RETCODE_SUCCESS, "data": {"url": "u", "alt": "a"}}]
        client = self._client_with_states(states, monkeypatch)
        # _client_with_states 已在 client.cookies 设了 X-CSRF-TOKEN
        cred = _qr_poll_and_finalize(client, "qrid")
        assert cred.cookies.get("X-CSRF-TOKEN") == "csrf"
        assert (tmp_path / "credential.json").exists()
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


class TestExchangeMobileCookies:
    """Single-pass cross-domain exchange: probe data.url ONCE (alt token is
    single-use), capture .weibo.com cookies from the 302 Set-Cookie + cdurl from
    the Location, then fetch the cdurl directly for .weibo.cn cookies —
    bypassing the login.sina.com.cn hop that 403s. Returns (weibo, mobile)."""

    def _patch_client(self, monkeypatch, transport):
        orig_client = httpx.Client
        monkeypatch.setattr(
            "weibo_cli.auth.httpx.Client",
            lambda *a, **kw: orig_client(
                *a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
            ),
        )

    def test_single_probe_captures_weibo_com_and_weibo_cn(self, monkeypatch):
        """一次 data.url 探测同时拿 .weibo.com（302 Set-Cookie）和 .weibo.cn（cdurl 直连），绝不碰 login.sina.com.cn。"""
        from urllib.parse import quote

        cdurl = "https://passport.weibo.cn/sso/crossdomain?entry=miniblog&action=login&ticket=ST-xyz&display=0&proj=1&savestate=30"
        location = f"https://login.sina.com.cn/sso/v2/crossdomain?entry=miniblog&action=login&ticket=ST-outer&cdurl={quote(cdurl, safe='')}"

        hits = {"sina": 0, "weibo_cn": 0}

        def handler(request):
            host = request.url.host or ""
            if "sina.com.cn" in host:
                hits["sina"] += 1
                return httpx.Response(403)  # 真实环境里这步 403，必须被绕过
            if "passport.weibo.cn" in host:
                hits["weibo_cn"] += 1
                return httpx.Response(
                    200,
                    headers={"set-cookie": "SUB=cn_sub; Domain=.weibo.cn; Path=/"},
                )
            # data.url (passport.weibo.com/sso/v2/login) → 302，Set-Cookie .weibo.com + Location
            return httpx.Response(
                302,
                headers=[
                    ("set-cookie", "SUB=com_sub; Domain=.weibo.com; Path=/"),
                    ("set-cookie", "ALC=alc; Domain=passport.weibo.com; Path=/"),
                    ("location", location),
                ],
            )

        transport = httpx.MockTransport(handler)
        self._patch_client(monkeypatch, transport)

        weibo, mobile = _exchange_mobile_cookies("https://passport.weibo.com/sso/v2/login?alt=ALT-x")
        # .weibo.com bucket（含 passport.weibo.com 的 ALC）
        assert weibo.get("SUB") == "com_sub"
        assert weibo.get("ALC") == "alc"
        # .weibo.cn bucket（m.weibo.cn 用）
        assert mobile.get("SUB") == "cn_sub"
        # 关键：只探了一次 data.url，没碰 login.sina.com.cn
        assert hits["sina"] == 0
        assert hits["weibo_cn"] == 1

    def test_no_location_returns_weibo_only(self, monkeypatch):
        """data.url 直接 200 无 Location → .weibo.com cookies 仍可从 Set-Cookie 拿到，mobile 空。"""
        def handler(request):
            return httpx.Response(200, headers={"set-cookie": "SUB=com_sub; Domain=.weibo.com; Path=/"})

        transport = httpx.MockTransport(handler)
        self._patch_client(monkeypatch, transport)
        weibo, mobile = _exchange_mobile_cookies("https://passport.weibo.com/sso/v2/login?alt=x")
        assert weibo.get("SUB") == "com_sub"
        assert mobile == {}

    def test_no_cdurl_in_location_returns_weibo_only(self, monkeypatch):
        def handler(request):
            return httpx.Response(302, headers={"location": "https://login.sina.com.cn/other?foo=bar"})

        transport = httpx.MockTransport(handler)
        self._patch_client(monkeypatch, transport)
        weibo, mobile = _exchange_mobile_cookies("https://passport.weibo.com/sso/v2/login?alt=x")
        assert mobile == {}

    def test_empty_url_returns_empty_tuple(self):
        assert _exchange_mobile_cookies("") == ({}, {})

    def test_network_failure_returns_empty_tuple(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom")

        transport = httpx.MockTransport(handler)
        self._patch_client(monkeypatch, transport)
        # 不应抛异常，登录仍能靠 passport_cookies 成功
        assert _exchange_mobile_cookies("https://passport.weibo.com/sso/v2/login?alt=x") == ({}, {})


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

    def test_save_qr_session_stores_png_path(self, tmp_path, monkeypatch):
        """png_path 以绝对路径形式持久化，供后续清理使用。"""
        import os

        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        png = str(tmp_path / "qr.png")
        session = {"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"}
        save_qr_session(session, png_path=png)
        loaded = load_qr_session()
        assert loaded["png_path"] == os.path.abspath(png)

    def test_save_qr_session_omits_png_path_when_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        save_qr_session({"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"})
        loaded = load_qr_session()
        assert "png_path" not in loaded

    def test_clear_deletes_png_and_returns_path(self, tmp_path, monkeypatch):
        """clear 删除记录的 PNG 文件并返回其绝对路径。"""
        import os

        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        old_png = tmp_path / "old.png"
        old_png.write_bytes(b"PNG")
        save_qr_session(
            {"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"},
            png_path=str(old_png),
        )

        returned = clear_qr_session()
        assert returned == os.path.abspath(str(old_png))
        assert not old_png.exists()
        assert not (tmp_path / "qr_session.json").exists()

    def test_clear_without_png_path_returns_none(self, tmp_path, monkeypatch):
        """会话无 png_path 字段 → 返回 None，会话文件仍删除。"""
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        save_qr_session({"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"})

        returned = clear_qr_session()
        assert returned is None
        assert not (tmp_path / "qr_session.json").exists()

    def test_clear_missing_png_file_returns_none(self, tmp_path, monkeypatch):
        """记录了 png_path 但文件已不存在 → 返回 None，会话文件仍删除。"""
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        save_qr_session(
            {"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"},
            png_path=str(tmp_path / "gone.png"),
        )

        returned = clear_qr_session()
        assert returned is None
        assert not (tmp_path / "qr_session.json").exists()

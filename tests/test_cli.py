"""Tests for Weibo CLI — importability, command registration, and output format."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from weibo_cli.cli import cli


# ── Import & registration tests ─────────────────────────────────────


def test_import_cli():
    """CLI module is importable."""
    from weibo_cli import cli as cli_mod
    assert cli_mod is not None


def test_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Weibo CLI" in result.output


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.2.1" in result.output


EXPECTED_COMMANDS = [
    "login", "logout", "status", "me",
    "hot", "feed", "detail", "comments", "trending", "search",
    "profile", "weibos", "following", "followers", "reposts", "home",
]


@pytest.mark.parametrize("cmd", EXPECTED_COMMANDS)
def test_command_registered(cmd):
    """All expected commands are registered."""
    runner = CliRunner()
    result = runner.invoke(cli, [cmd, "--help"])
    assert result.exit_code == 0, f"{cmd} --help failed: {result.output}"


def test_command_count():
    """Ensure we have exactly the expected number of commands."""
    assert len(cli.commands) == len(EXPECTED_COMMANDS)


# ── Constants tests ─────────────────────────────────────────────────


def test_constants_urls():
    from weibo_cli.constants import BASE_URL, PASSPORT_URL, HOT_SEARCH_URL
    assert BASE_URL == "https://weibo.com"
    assert PASSPORT_URL == "https://passport.weibo.com"
    assert HOT_SEARCH_URL.startswith("/ajax/")


def test_constants_headers():
    from weibo_cli.constants import HEADERS
    assert "User-Agent" in HEADERS
    assert "Chrome" in HEADERS["User-Agent"]


# ── Exception tests ─────────────────────────────────────────────────


def test_exception_hierarchy():
    from weibo_cli.exceptions import WeiboApiError, SessionExpiredError, QRExpiredError, error_code_for_exception
    assert issubclass(SessionExpiredError, WeiboApiError)
    assert issubclass(QRExpiredError, WeiboApiError)
    assert error_code_for_exception(SessionExpiredError()) == "not_authenticated"
    assert error_code_for_exception(QRExpiredError()) == "qr_expired"


def test_all_error_codes():
    from weibo_cli.exceptions import (
        AuthRequiredError, ParamError, RateLimitError, error_code_for_exception
    )
    assert error_code_for_exception(AuthRequiredError()) == "not_authenticated"
    assert error_code_for_exception(RateLimitError()) == "rate_limited"
    assert error_code_for_exception(ParamError("test")) == "invalid_params"
    assert error_code_for_exception(ValueError("test")) == "unknown_error"


# ── Command help text ───────────────────────────────────────────────


@pytest.mark.parametrize("cmd,expected_text", [
    ("hot", "热搜"),
    ("feed", "Feed"),
    ("detail", "详情"),
    ("comments", "评论"),
    ("trending", "趋势"),
    ("search", "搜索"),
    ("profile", "用户资料"),
    ("weibos", "微博列表"),
    ("following", "关注列表"),
    ("followers", "粉丝列表"),
    ("reposts", "转发"),
    ("home", "关注者"),
])
def test_command_help_text(cmd, expected_text):
    """Each command has appropriate help description."""
    runner = CliRunner()
    result = runner.invoke(cli, [cmd, "--help"])
    assert expected_text in result.output


@pytest.mark.parametrize("cmd", ["hot", "feed", "detail", "comments", "trending", "search", "profile", "weibos", "following", "followers", "reposts", "home"])
def test_json_option_available(cmd):
    """All data commands support --json flag."""
    runner = CliRunner()
    result = runner.invoke(cli, [cmd, "--help"])
    assert "--json" in result.output


# ── login group / qr subcommands ────────────────────────────────────


def test_qr_subcommands_registered():
    """login group 下有 qr-start / qr-done 子命令。"""
    runner = CliRunner()
    for sub in ["qr-start", "qr-done"]:
        result = runner.invoke(cli, ["login", sub, "--help"])
        assert result.exit_code == 0, f"login {sub} --help failed: {result.output}"


def test_qr_start_does_not_trigger_default_login(monkeypatch):
    """qr-start 不能误触发 group 回调的默认登录流程。"""
    called = {"get_credential": 0, "qr_login": 0}
    # get_credential 与 qr_login 均在 login 函数体内 from ..auth import，需 patch 源模块
    import weibo_cli.auth as auth_core
    monkeypatch.setattr(auth_core, "get_credential", lambda: called.__setitem__("get_credential", called["get_credential"] + 1) or None)
    monkeypatch.setattr(auth_core, "qr_login", lambda: called.__setitem__("qr_login", called["qr_login"] + 1) or None)
    # qr-start 不带 --png 应因参数缺失而非 0 退出，但不应调用 get_credential/qr_login
    runner = CliRunner()
    runner.invoke(cli, ["login", "qr-start"])
    assert called["get_credential"] == 0
    assert called["qr_login"] == 0


def test_qr_done_success_clears_session(monkeypatch, tmp_path):
    """qr-done 成功 → 删 session + 输出 status:success。"""
    import weibo_cli.auth as auth_core
    from weibo_cli.auth import Credential

    session_file = tmp_path / "qr_session.json"
    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", session_file)
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": __import__("time").time(),
    })
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", lambda client, qrid, **kw: Credential(cookies={"SUB": "final"}))
    deleted = {"called": False}
    monkeypatch.setattr(auth_core, "clear_qr_session", lambda path=None: deleted.__setitem__("called", True))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 0
    assert "status: success" in result.output
    assert deleted["called"] is True


def test_qr_done_timeout_keeps_session(monkeypatch, tmp_path):
    """qr-done 超时 → 保留 session + exit 1。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": __import__("time").time(),
    })
    def _raise(*a, **kw):
        raise TimeoutError("poll timed out")
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", _raise)
    deleted = {"called": False}
    monkeypatch.setattr(auth_core, "clear_qr_session", lambda path=None: deleted.__setitem__("called", True))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 1
    assert deleted["called"] is False  # 超时保留 session


def test_qr_done_expired_clears_session(monkeypatch, tmp_path):
    """qr-done EXPIRED → 删 session + exit 1。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": __import__("time").time(),
    })
    def _raise(*a, **kw):
        raise auth_core.QRExpiredError()
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", _raise)
    deleted = {"called": False}
    monkeypatch.setattr(auth_core, "clear_qr_session", lambda path=None: deleted.__setitem__("called", True))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 1
    assert deleted["called"] is True


def test_qr_done_ttl_expired_clears_session(monkeypatch, tmp_path):
    """qr-done session 文件超 240s TTL → 删 session + exit 1。"""
    import time as _time
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": _time.time() - 999,  # 远超 TTL
    })
    poll_called = {"called": False}
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", lambda *a, **kw: poll_called.__setitem__("called", True))
    deleted = {"called": False}
    monkeypatch.setattr(auth_core, "clear_qr_session", lambda path=None: deleted.__setitem__("called", True))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 1
    assert poll_called["called"] is False  # TTL 过期不轮询
    assert deleted["called"] is True


def test_qr_done_no_session_exits(monkeypatch, tmp_path):
    """qr-done 无 session 文件 → exit 1。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: None)

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 1
    assert "未找到 QR 会话" in (result.output + (result.stderr or ""))


def test_qr_done_runtime_error_clears_session(monkeypatch, tmp_path):
    """qr-done _qr_poll_and_finalize 抛 RuntimeError → 删 session + exit 1。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": __import__("time").time(),
    })
    def _raise(*a, **kw):
        raise RuntimeError("Login succeeded but no cookies were obtained")
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", _raise)
    deleted = {"called": False}
    monkeypatch.setattr(auth_core, "clear_qr_session", lambda path=None: deleted.__setitem__("called", True))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 1
    assert deleted["called"] is True
    assert "Login succeeded but no cookies" in (result.output + (result.stderr or ""))

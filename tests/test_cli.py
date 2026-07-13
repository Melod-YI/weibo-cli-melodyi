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


def test_qr_start_cleans_residual_png(monkeypatch, tmp_path):
    """qr-start 检测到上次残留的二维码图片会删除并提示（哪怕新路径不同）。"""
    import os

    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "CONFIG_DIR", tmp_path)

    # 上次残留：旧 PNG + 旧会话（png_path 指向旧 PNG）
    old_png = tmp_path / "old.png"
    old_png.write_bytes(b"OLD")
    auth_core.save_qr_session(
        {"qrid": "old", "csrf_token": "c", "cookies": {}, "scan_url": "s"},
        png_path=str(old_png),
    )

    # mock 网络/图片生成（不真正打 passport）
    monkeypatch.setattr(auth_core, "_qr_get_session", lambda client: {
        "qrid": "new", "csrf_token": "c2", "cookies": {"X-CSRF-TOKEN": "c2"}, "scan_url": "s2",
    })
    new_png = tmp_path / "new.png"
    monkeypatch.setattr(auth_core, "_write_qr_png", lambda data, path: __import__("pathlib").Path(path).write_bytes(b"NEW"))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-start", "--png", str(new_png)])
    assert result.exit_code == 0, result.output
    assert not old_png.exists()  # 旧 PNG 已删除
    assert "之前登录残留" in result.output
    assert os.path.abspath(str(old_png)) in result.output


def test_qr_start_no_residual_no_cleanup_message(monkeypatch, tmp_path):
    """qr-start 无残留 → 不输出清理提示。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth_core, "_qr_get_session", lambda client: {
        "qrid": "new", "csrf_token": "c2", "cookies": {"X-CSRF-TOKEN": "c2"}, "scan_url": "s2",
    })
    monkeypatch.setattr(auth_core, "_write_qr_png", lambda data, path: __import__("pathlib").Path(path).write_bytes(b"NEW"))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-start", "--png", str(tmp_path / "new.png")])
    assert result.exit_code == 0, result.output
    assert "已删除" not in result.output


def test_qr_start_terminal_renders_colored_qr(monkeypatch, tmp_path):
    """qr-start --terminal 在终端渲染强制配色的二维码，仍保存会话并打印结构化字段。"""
    import weibo_cli.auth as auth_core
    from weibo_cli.auth import _QR_BLACK_FG, _QR_WHITE_BG

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth_core, "_qr_get_session", lambda client: {
        "qrid": "t1", "csrf_token": "c", "cookies": {"X-CSRF-TOKEN": "c"}, "scan_url": "https://scan.example/qr",
    })

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-start", "--terminal"])
    assert result.exit_code == 0, result.output
    # 终端渲染：强制黑墨 + 白底，黑底终端亦可扫
    assert _QR_BLACK_FG in result.output
    assert _QR_WHITE_BG in result.output
    # 仍打印结构化字段并保存会话
    assert "qrid: t1" in result.output
    assert "session:" in result.output
    assert (tmp_path / "qr_session.json").exists()


def test_qr_start_without_target_errors(monkeypatch, tmp_path):
    """qr-start 不给 --png 也不给 --terminal → 非零退出并提示。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "CONFIG_DIR", tmp_path)
    # 不该真正拉会话
    monkeypatch.setattr(auth_core, "_qr_get_session", lambda client: pytest.fail("不应在缺目标时拉会话"))

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-start"])
    assert result.exit_code != 0
    assert "输出目标" in result.output or "--terminal" in result.output


def test_qr_done_success_prints_cleanup_message(monkeypatch, tmp_path):
    """qr-done 成功且 clear_qr_session 返回路径 → 输出登录成功清理提示。"""
    import weibo_cli.auth as auth_core
    from weibo_cli.auth import Credential

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": __import__("time").time(),
    })
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", lambda client, qrid, **kw: Credential(cookies={"SUB": "final"}))
    monkeypatch.setattr(auth_core, "clear_qr_session", lambda path=None: "/abs/old.png")

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 0
    assert "登录成功，二维码图片已删除" in result.output
    assert "/abs/old.png" in result.output


def test_qr_done_timeout_no_cleanup_message(monkeypatch, tmp_path):
    """qr-done 轮询超时（会话仍有效）→ 不清理、不输出清理提示。"""
    import weibo_cli.auth as auth_core

    monkeypatch.setattr(auth_core, "QR_SESSION_FILE", tmp_path / "qr_session.json")
    monkeypatch.setattr(auth_core, "load_qr_session", lambda path=None: {
        "qrid": "q", "csrf_token": "c", "cookies": {"SUB": "x"},
        "scan_url": "s", "created_at": __import__("time").time(),
    })
    def _raise(*a, **kw):
        raise TimeoutError("poll timed out")
    monkeypatch.setattr(auth_core, "_qr_poll_and_finalize", _raise)

    runner = CliRunner()
    result = runner.invoke(cli, ["login", "qr-done"])
    assert result.exit_code == 1
    assert "二维码图片已删除" not in result.output


# ── me command ──────────────────────────────────────────────────────


def test_me_renders_uid(monkeypatch, profile_response, mock_credential):
    """weibo me 在纯文本输出中显示当前用户的 uid。"""
    import weibo_cli.commands.auth as auth_cmds
    import weibo_cli.commands._common as common

    monkeypatch.setattr(auth_cmds, "require_auth", lambda: mock_credential)

    class _FakeClient:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_current_uid(self): return "1699432410"
        def get_profile(self, uid):
            assert uid == "1699432410"
            return profile_response["data"]

    monkeypatch.setattr(common, "WeiboClient", _FakeClient)

    runner = CliRunner()
    result = runner.invoke(cli, ["me"])
    assert result.exit_code == 0, result.output
    assert "1699432410" in result.output
    assert "UID" in result.output


# ── following: self-routing + --all/--search ──────────────────────────


def _patch_following_client(monkeypatch, fake_client):
    import weibo_cli.commands.personal as personal
    import weibo_cli.commands._common as common
    monkeypatch.setattr(personal, "require_auth", lambda: __import__("weibo_cli.auth", fromlist=["Credential"]).Credential(cookies={"SUB": "x"}))
    monkeypatch.setattr(common, "WeiboClient", fake_client)
    return personal, common


def test_following_self_routes_to_follow_content(monkeypatch):
    """本人 uid → is_self=True → followContent，输出含页/总数摘要。"""
    calls = {}

    class _FakeClient:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_current_uid(self): return "5555027006"
        def get_following_list(self, uid, *, is_self, page=1, fetch_all=False, q=None):
            calls["is_self"] = is_self
            return {"users": [{"idstr": "1", "screen_name": "Alice", "followers_count": 0, "description": ""}],
                    "total": 308, "fetched": 1, "source": "followContent", "search": None}

    _patch_following_client(monkeypatch, _FakeClient)
    result = CliRunner().invoke(cli, ["following", "5555027006"])
    assert result.exit_code == 0, result.output
    assert calls["is_self"] is True
    assert "第 1 页" in result.output and "共 308" in result.output
    assert "Alice" in result.output


def test_following_non_self_routes_to_friendships(monkeypatch):
    """非本人 uid → is_self=False → friendships。"""

    class _FakeClient:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_current_uid(self): return "5555027006"
        def get_following_list(self, uid, *, is_self, page=1, fetch_all=False, q=None):
            assert is_self is False
            return {"users": [{"idstr": "2", "screen_name": "Bob", "followers_count": 0, "description": ""}],
                    "total": 10, "fetched": 1, "source": "friendships", "search": None}

    _patch_following_client(monkeypatch, _FakeClient)
    result = CliRunner().invoke(cli, ["following", "1699432410"])
    assert result.exit_code == 0, result.output
    assert "Bob" in result.output


def test_following_all_summary(monkeypatch):
    """--all 输出 '已加载 N' 摘要。"""

    class _FakeClient:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_current_uid(self): return "5555027006"
        def get_following_list(self, uid, *, is_self, page=1, fetch_all=False, q=None):
            assert fetch_all is True
            return {"users": [{"idstr": "1", "screen_name": "Alice", "followers_count": 0, "description": ""}],
                    "total": 308, "fetched": 279, "source": "followContent", "search": None}

    _patch_following_client(monkeypatch, _FakeClient)
    result = CliRunner().invoke(cli, ["following", "5555027006", "--all"])
    assert result.exit_code == 0, result.output
    assert "已加载 279" in result.output and "共 308" in result.output


def test_following_search_summary(monkeypatch):
    """--search 输出搜索摘要行。"""

    class _FakeClient:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_current_uid(self): return "5555027006"
        def get_following_list(self, uid, *, is_self, page=1, fetch_all=False, q=None):
            assert q == "bl"
            return {"users": [{"idstr": "1", "screen_name": "碧蓝航线", "followers_count": 0, "description": ""}],
                    "total": None, "fetched": 1, "source": "followContent", "search": "bl"}

    _patch_following_client(monkeypatch, _FakeClient)
    result = CliRunner().invoke(cli, ["following", "5555027006", "-s", "bl"])
    assert result.exit_code == 0, result.output
    assert '搜索"bl"' in result.output and "命中 1" in result.output
    assert "碧蓝航线" in result.output

"""纯文本渲染测试：无 Rich 边框字符、无 markup 残留、含预期字段。"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from weibo_cli.auth import Credential
from weibo_cli.cli import cli
from weibo_cli.commands.renderers import (
    render_comment_list,
    render_repost_list,
    render_user_table,
    render_weibo_card,
    render_weibo_list,
)


# 捕获 click.echo 输出
@pytest.fixture
def capture(monkeypatch):
    lines = []
    monkeypatch.setattr("weibo_cli.commands.renderers.click.echo", lambda msg="": lines.append(msg))
    return lines


BOX_CHARS = "│┌┐└┘─┃┏┓┗┛━"


def _assert_no_box(lines):
    for ln in lines:
        assert not any(c in ln for c in BOX_CHARS), f"含边框字符: {ln!r}"
        assert "[" not in ln or "/]" not in ln, f"含 Rich markup 残留: {ln!r}"


def test_render_weibo_card_plain(capture):
    s = {
        "text_raw": "这是一条微博",
        "created_at": "2026-07-08 12:34",
        "reposts_count": 3, "comments_count": 12, "attitudes_count": 45,
        "mblogid": "Qw06Kd98p",
        "user": {"screen_name": "张三", "verified": True, "idstr": "1699432410"},
    }
    render_weibo_card(s, 1)
    _assert_no_box(capture)
    joined = "\n".join(capture)
    assert "@张三" in joined
    assert "评论12" in joined and "转发3" in joined and "赞45" in joined
    assert "Qw06Kd98p" in joined
    assert "✓" in joined  # 认证标记保留
    assert "uid=1699432410" in joined  # 作者 uid 暴露，便于跳转 profile/weibos


def test_render_weibo_card_uid_falls_back_to_id(capture):
    """user 只有数字 id、无 idstr 时也应当打出 uid。"""
    s = {
        "text_raw": "x", "created_at": "t",
        "user": {"screen_name": "李四", "id": 12345},
    }
    render_weibo_card(s, 1)
    assert "uid=12345" in "\n".join(capture)


def test_render_weibo_card_source_strips_html(capture):
    """带链接的来源应去 HTML，只留链接文本（weibos 列表场景）。"""
    s = {
        "text_raw": "正文", "created_at": "Jul 11",
        "source": '<a href="https://shop.sc.weibo.com/..." rel="nofollow">鹤屋通贩的小店</a>',
    }
    render_weibo_card(s, 1, show_user=False)
    joined = "\n".join(capture)
    assert "<a" not in joined  # HTML 标签被剥离
    assert "鹤屋通贩的小店" in joined  # 链接文本保留
    assert "via" in joined


def test_detail_command_shows_uid_and_full_text(monkeypatch):
    """detail 输出含作者 UID 行，且渲染完整 text_raw（非截断）。"""
    long_weibo = {
        "mblogid": "R8cuZ8uMW",
        "isLongText": True,
        "text_raw": "长微博全文，结尾是 hashtag\n#胜利女神nikke[超话]#",
        "created_at": "Jul 11 17:00",
        "reposts_count": 1, "comments_count": 2, "attitudes_count": 3, "reads_count": 100,
        "source": "",
        "user": {"screen_name": "某博主", "idstr": "9876543210", "verified": False},
    }
    _stub_client(monkeypatch, {"get_weibo_detail": long_weibo})
    monkeypatch.setattr("weibo_cli.commands._common.require_auth", lambda: Credential(cookies={"SUB": "x"}))
    runner = CliRunner()
    result = runner.invoke(cli, ["detail", "R8cuZ8uMW"])
    assert result.exit_code == 0
    out = result.output
    assert "@某博主" in out
    assert "UID: 9876543210" in out  # 作者 uid 单独成行，方便复制跳转
    assert "#胜利女神nikke[超话]#" in out  # 全文结尾，未被截断


def test_detail_command_source_strips_html(monkeypatch):
    """detail 的 via 来源带 HTML 时应剥离，只留链接文本。"""
    weibo = {
        "mblogid": "X1",
        "text_raw": "正文",
        "created_at": "Jul 11 17:00",
        "source": '<a href="https://shop.sc.weibo.com/..." rel="nofollow">鹤屋通贩的小店</a>',
        "user": {"screen_name": "某博主", "idstr": "1"},
    }
    _stub_client(monkeypatch, {"get_weibo_detail": weibo})
    monkeypatch.setattr("weibo_cli.commands._common.require_auth", lambda: Credential(cookies={"SUB": "x"}))
    result = CliRunner().invoke(cli, ["detail", "X1"])
    assert result.exit_code == 0
    assert "<a" not in result.output
    assert "via 鹤屋通贩的小店" in result.output


def test_render_weibo_list_empty(capture):
    render_weibo_list([], empty_msg="暂无微博")
    assert capture == ["暂无微博"]


def test_render_user_table_plain(capture):
    users = [{"id": 1699432410, "screen_name": "张三", "verified": False, "followers_count": 12000, "description": "简介"}]
    render_user_table(users, title="关注列表")
    _assert_no_box(capture)
    joined = "\n".join(capture)
    assert "1699432410" in joined
    assert "张三" in joined
    assert "1.2万" in joined


def test_render_user_table_empty(capture):
    render_user_table([], empty_msg="暂无用户")
    assert capture == ["暂无用户"]


def test_render_comment_list_plain(capture):
    comments = [{"user": {"screen_name": "李四"}, "text": "说得好", "created_at": "2026-07-08 12:34", "like_counts": 5}]
    render_comment_list(comments)
    _assert_no_box(capture)
    joined = "\n".join(capture)
    assert "@李四" in joined
    assert "说得好" in joined
    assert "赞5" in joined


def test_render_comment_list_empty(capture):
    render_comment_list([])
    assert capture == ["暂无评论"]


def test_render_repost_list_empty(capture):
    render_repost_list([])
    assert capture == ["暂无转发"]


def _stub_client(monkeypatch, methods):
    """让 handle_command 用一个返回固定数据的 stub WeiboClient。"""
    from weibo_cli.commands import _common

    class _Stub:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    for k, v in methods.items():
        setattr(_Stub, k, lambda self, *a, _v=v: _v)
    monkeypatch.setattr(_common, "WeiboClient", _Stub)
    monkeypatch.setattr("weibo_cli.commands._common.get_credential", lambda: Credential(cookies={"SUB": "x"}))


def test_hot_command_plain(monkeypatch):
    _stub_client(monkeypatch, {"get_hot_search": {"realtime": [
        {"word": "科技", "num": 12345, "icon_desc": "热"},
        {"word": "娱乐", "num": 98765, "icon_desc": "沸"},
    ]}})
    runner = CliRunner()
    result = runner.invoke(cli, ["hot"])
    assert result.exit_code == 0
    assert "科技" in result.output and "娱乐" in result.output
    assert "1.2万" in result.output
    for c in "│┌┐└┘":
        assert c not in result.output


def test_status_command_plain(monkeypatch):
    monkeypatch.setattr("weibo_cli.commands._common.get_credential", lambda: Credential(cookies={"SUB": "x", "SUBP": "y"}))
    runner = CliRunner()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "authenticated" in result.output
    assert "cookies=2" in result.output


def test_me_uid_missing_exits_nonzero(monkeypatch):
    """me 拿不到 uid 时 stderr 报错并 exit_code=1。"""
    _stub_client(monkeypatch, {"get_current_uid": None})
    runner = CliRunner()
    result = runner.invoke(cli, ["me"])
    assert result.exit_code == 1
    # Click 8.4 默认分离 stderr；两处都查以兼容
    combined = (result.output or "") + (result.stderr if result.stderr is not None else "")
    assert "无法获取当前 uid" in combined

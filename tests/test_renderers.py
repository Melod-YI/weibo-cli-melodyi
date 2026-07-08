"""纯文本渲染测试：无 Rich 边框字符、无 markup 残留、含预期字段。"""
from __future__ import annotations

import pytest

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
        "user": {"screen_name": "张三", "verified": True},
    }
    render_weibo_card(s, 1)
    _assert_no_box(capture)
    joined = "\n".join(capture)
    assert "@张三" in joined
    assert "评论12" in joined and "转发3" in joined and "赞45" in joined
    assert "Qw06Kd98p" in joined
    assert "✓" in joined  # 认证标记保留


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

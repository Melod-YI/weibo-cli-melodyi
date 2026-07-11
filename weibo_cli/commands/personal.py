"""Personal & profile commands: profile, weibos, following, followers, reposts, home."""

from __future__ import annotations

import click

from ._common import format_count, handle_command, require_auth, structured_output_options
from .renderers import render_repost_list, render_user_table, render_weibo_list


@click.command()
@click.argument("uid")
@structured_output_options
def profile(uid, as_json, as_yaml):
    """查看用户资料 (weibo profile <uid>)"""
    cred = require_auth()

    def _render(data):
        user = data.get("user", data)
        lines = []
        name = user.get("screen_name", "未知")
        verified = " ✓" if user.get("verified") else ""
        lines.append(f"昵称: {name}{verified}")
        if user.get("verified_reason"):
            lines.append(f"认证: {user['verified_reason']}")
        if user.get("description"):
            lines.append(f"简介: {user['description']}")
        stats = []
        if user.get("followers_count") is not None:
            stats.append(f"粉丝: {format_count(user['followers_count'])}")
        if user.get("friends_count") is not None:
            stats.append(f"关注: {format_count(user['friends_count'])}")
        if user.get("statuses_count") is not None:
            stats.append(f"微博: {format_count(user['statuses_count'])}")
        if stats:
            lines.append("  ".join(stats))
        if user.get("location"):
            lines.append(f"位置: {user['location']}")
        click.echo("\n".join(lines))
        tabs = data.get("tabList", [])
        if tabs:
            tab_names = [t.get("tabName", t.get("name", "")) for t in tabs]
            click.echo(f"可用 Tab: {' | '.join(tab_names)}")

    def _action(client):
        return client.get_profile(uid)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("uid")
@click.option("--page", "-p", default=1, help="页码")
@click.option("--count", "-n", default=20, help="条数")
@structured_output_options
def weibos(uid, page, count, as_json, as_yaml):
    """查看用户微博列表 (weibo weibos <uid>)"""
    cred = require_auth()

    def _render(data):
        statuses = data if isinstance(data, list) else data.get("list", data.get("statuses", []))
        render_weibo_list(statuses, count=count, show_user=False)

    def _action(client):
        return client.get_user_weibos(uid, page=page)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("uid")
@click.option("--page", "-p", default=1, help="页码")
@click.option("--all", "fetch_all", is_flag=True, help="拉取全部关注（自动翻页）")
@click.option("--search", "-s", "search", default=None, help="按关键字搜索：本人走微博原生拼音搜索，非本人本地 contains 匹配")
@structured_output_options
def following(uid, page, fetch_all, search, as_json, as_yaml):
    """查看用户关注列表 (weibo following <uid>)"""
    cred = require_auth()

    def _render(data):
        users = data.get("users", []) if isinstance(data, dict) else data
        # 摘要行：让"只有 19 条"这类情况不再反直觉
        if isinstance(data, dict):
            total = data.get("total")
            kw = data.get("search")
            if kw is not None:
                click.echo(f'关注列表 搜索"{kw}" (命中 {len(users)})')
            elif fetch_all:
                tot = f"共 {total} 人，" if total is not None else ""
                loaded = data.get("fetched", len(users))
                click.echo(f"关注列表 ({tot}已加载 {loaded})")
            else:
                tot = f"，共 {total}" if total is not None else ""
                click.echo(f"关注列表 (第 {page} 页，本页 {len(users)}{tot})")
        render_user_table(users, title="关注列表", empty_msg="暂无关注")

    def _action(client):
        # 本人 → followContent（大页 + 原生搜索）；否则 → friendships/friends
        is_self = uid == client.get_current_uid()
        return client.get_following_list(
            uid, is_self=is_self, page=page, fetch_all=fetch_all, q=search,
        )

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("uid")
@click.option("--page", "-p", default=1, help="页码")
@structured_output_options
def followers(uid, page, as_json, as_yaml):
    """查看用户粉丝列表 (weibo followers <uid>)"""
    cred = require_auth()

    def _render(data):
        users = data.get("users", []) if isinstance(data, dict) else data
        render_user_table(users, title="粉丝列表", empty_msg="暂无粉丝")

    def _action(client):
        return client.get_followers(uid, page=page)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("mblogid")
@click.option("--count", "-n", default=10, help="转发条数")
@click.option("--page", "-p", default=1, help="页码")
@structured_output_options
def reposts(mblogid, count, page, as_json, as_yaml):
    """查看微博转发 (weibo reposts <mblogid>)"""
    cred = require_auth()

    def _render(data):
        repost_list = data.get("data", []) if isinstance(data, dict) else data
        render_repost_list(repost_list, count=count)

    def _action(client):
        weibo = client.get_weibo_detail(mblogid)
        weibo_id = str(weibo.get("id", weibo.get("mid", "")))
        return client.get_reposts(weibo_id, page=page, count=count)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.option("--count", "-n", default=20, help="条数 (1-50)")
@structured_output_options
def home(count, as_json, as_yaml):
    """查看关注者 Feed (weibo home)"""
    cred = require_auth()

    def _render(data):
        statuses = data.get("statuses", [])
        render_weibo_list(statuses, count=count, empty_msg="暂无关注者微博")

    def _action(client):
        return client.get_friends_timeline(count=min(count, 50))

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)

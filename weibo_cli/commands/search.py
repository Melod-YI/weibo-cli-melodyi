"""Search, hot-search and feed commands."""

from __future__ import annotations

import click

from ._common import format_count, handle_command, require_auth, strip_html, structured_output_options
from .renderers import render_comment_list, render_weibo_list


@click.command(name="hot")
@click.option("--count", "-n", default=50, help="条数 (默认50)")
@structured_output_options
def hot(count, as_json, as_yaml):
    """查看微博热搜榜"""
    from ..auth import get_credential

    cred = get_credential()

    def _render(data):
        items = data.get("realtime") or data.get("band_list") or []
        if not items:
            click.echo("（无热搜）")
            return
        for i, item in enumerate(items[:count], 1):
            word = item.get("word", item.get("note", ""))
            icon = item.get("icon_desc", item.get("label_name", ""))
            num = item.get("num", item.get("raw_hot", ""))
            num_str = format_count(num) if num else ""
            click.echo(f"#{i:<3} {word}  {icon}  {num_str}")

    def _action(client):
        return client.get_hot_search()

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.option("--count", "-n", default=10, help="条数 (1-20)")
@structured_output_options
def feed(count, as_json, as_yaml):
    """查看热门微博 Feed"""
    from ..auth import get_credential

    cred = get_credential()

    def _render(data):
        statuses = data.get("statuses", [])
        render_weibo_list(statuses, count=count, empty_msg="暂无热门微博")

    def _action(client):
        return client.get_hot_timeline(count=min(count, 20))

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("mblogid")
@structured_output_options
def detail(mblogid, as_json, as_yaml):
    """查看微博详情 (weibo detail <mblogid>)"""
    cred = require_auth()

    def _render(data):
        user = data.get("user", {})
        name = user.get("screen_name", "未知")
        verified = " ✓" if user.get("verified") else ""
        uid = str(user.get("idstr", user.get("id", ""))) or ""
        text = strip_html(data.get("text_raw", data.get("text", "")))
        source = strip_html(data.get("source", ""))
        created = data.get("created_at", "")
        reposts = data.get("reposts_count", 0)
        comments_count = data.get("comments_count", 0)
        likes = data.get("attitudes_count", 0)
        reads = data.get("reads_count", 0)

        lines = [f"@{name}{verified}"]
        if uid:
            lines.append(f"UID: {uid}")
        if user.get("verified_reason"):
            lines.append(f"  {user['verified_reason']}")
        lines.append(f"{created}{f'  via {source}' if source else ''}")
        lines.append("")
        lines.append(text)
        lines.append("")
        if data.get("pic_ids"):
            lines.append(f"图片{len(data['pic_ids'])} 张")
        lines.append(f"阅读{reads} 评论{comments_count} 转发{reposts} 赞{likes}  ID:{data.get('mblogid', '')}")
        click.echo("\n".join(lines))

    def _action(client):
        return client.get_weibo_detail(mblogid)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("mblogid")
@click.option("--count", "-n", default=20, help="评论条数")
@structured_output_options
def comments(mblogid, count, as_json, as_yaml):
    """查看微博评论 (weibo comments <mblogid>)"""
    cred = require_auth()

    def _render(data):
        comment_list = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        render_comment_list(comment_list, count=count)

    def _action(client):
        weibo = client.get_weibo_detail(mblogid)
        weibo_id = str(weibo.get("id", weibo.get("mid", "")))
        return client.get_comments(weibo_id, count=count)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.option("--count", "-n", default=16, help="条数 (默认16)")
@structured_output_options
def trending(count, as_json, as_yaml):
    """查看实时搜索趋势"""
    from ..auth import get_credential

    cred = get_credential()

    def _render(data):
        items = data.get("realtime", [])
        if not items:
            click.echo("（无趋势）")
            return
        for i, item in enumerate(items[:count], 1):
            word = item.get("word", "")
            desc = str(item.get("description", ""))[:40]
            click.echo(f"#{i:<3} {word}  {desc}")

    def _action(client):
        return client.get_search_band()

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)


@click.command()
@click.argument("keyword")
@click.option("--count", "-n", default=10, help="显示条数")
@click.option("--page", "-p", default=1, help="页码")
@structured_output_options
def search(keyword, count, page, as_json, as_yaml):
    """搜索微博 (weibo search <关键词>)"""
    from ..auth import get_credential

    cred = get_credential()

    def _render(data):
        # Mobile API returns cards in data.cards or data.data.cards
        cards = []
        if isinstance(data, dict):
            cards_data = data.get("data", data)
            if isinstance(cards_data, dict):
                cards = cards_data.get("cards", [])

        # Extract weibos from cards
        statuses = []
        for card in cards:
            if card.get("card_type") == 9:
                mblog = card.get("mblog", {})
                if mblog:
                    statuses.append(mblog)
            elif card.get("card_group"):
                for sub in card["card_group"]:
                    if sub.get("card_type") == 9:
                        mblog = sub.get("mblog", {})
                        if mblog:
                            statuses.append(mblog)

        if not statuses:
            click.echo(f'未找到 "{keyword}" 相关微博')
            return

        render_weibo_list(statuses, count=count)

    def _action(client):
        return client.search_weibo(keyword, page=page)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)

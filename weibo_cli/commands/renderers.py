"""Shared renderers for CLI output — plain text, agent-friendly.

Each renderer takes parsed API data and prints plain text via click.echo.
No Rich tables/panels, no emoji, no box-drawing characters.
"""

from __future__ import annotations

import click

from ._common import format_count, strip_html


# ── Weibo card ──────────────────────────────────────────────────────


def render_weibo_card(s: dict, index: int, *, show_user: bool = True, max_text: int = 200, **_) -> None:
    """Render a single weibo status as plain text. Used by: feed, home, search, weibos."""
    text = strip_html(s.get("text_raw", s.get("text", "")))
    created = s.get("created_at", "")
    reposts = s.get("reposts_count", 0)
    comments_count = s.get("comments_count", 0)
    likes = s.get("attitudes_count", 0)
    mblogid = s.get("mblogid", s.get("bid", ""))

    lines: list[str] = []
    if show_user:
        user = s.get("user", {})
        name = user.get("screen_name", "未知")
        verified = " ✓" if user.get("verified") else ""
        uid = str(user.get("idstr", user.get("id", ""))) or ""
        lines.append(f"#{index}  @{name}{verified}{f'  uid={uid}' if uid else ''}  {created}")
    else:
        source = strip_html(s.get("source", ""))
        lines.append(f"#{index}  {created}{f'  via {source}' if source else ''}")

    lines.append(f"    {text[:max_text]}")

    pic_ids = s.get("pic_ids", s.get("pics", []))
    extras = []
    if pic_ids:
        extras.append(f"图片{len(pic_ids)}")
    extras.append(f"评论{comments_count} 转发{reposts} 赞{likes}")
    if mblogid:
        extras.append(f"ID:{mblogid}")
    lines.append("    " + "  ".join(extras))

    click.echo("\n".join(lines))


def render_weibo_list(statuses: list[dict], *, count: int = 20, show_user: bool = True, empty_msg: str = "暂无微博", **_) -> None:
    """Render a list of weibo statuses. Used by feed, home, search, weibos."""
    if not statuses:
        click.echo(empty_msg)
        return
    for i, s in enumerate(statuses[:count], 1):
        render_weibo_card(s, i, show_user=show_user)
        click.echo()  # blank line between cards


# ── User list ───────────────────────────────────────────────────────


def render_user_table(users: list[dict], *, title: str = "用户列表", empty_msg: str = "暂无用户") -> None:
    """Render a user list as aligned plain text. Used by following, followers."""
    if not users:
        click.echo(empty_msg)
        return
    rows = []
    for u in users:
        uid_str = str(u.get("id", u.get("idstr", "")))
        name = u.get("screen_name", "")
        verified = " ✓" if u.get("verified") else ""
        follower_count = format_count(u.get("followers_count", 0))
        desc = (u.get("description", "") or "")[:40]
        rows.append((uid_str, f"{name}{verified}", follower_count, desc))

    # column widths
    w_uid = max(len("UID"), max(len(r[0]) for r in rows))
    w_name = max(len("昵称"), max(len(r[1]) for r in rows))
    w_fans = max(len("粉丝"), max(len(r[2]) for r in rows))
    fmt = f"{{:<{w_uid}}}  {{:<{w_name}}}  {{:>{w_fans}}}  {{}}"
    click.echo(fmt.format("UID", "昵称", "粉丝", "简介"))
    for r in rows:
        click.echo(fmt.format(*r))


# ── Comment list ────────────────────────────────────────────────────


def render_comment_list(comments: list[dict], *, count: int = 20) -> None:
    """Render comment entries. Used by comments command."""
    if not comments:
        click.echo("暂无评论")
        return
    for c in comments[:count]:
        user = c.get("user", {})
        name = user.get("screen_name", "未知")
        text = strip_html(c.get("text", ""))
        created = c.get("created_at", "")
        likes = c.get("like_counts", 0)
        click.echo(f"@{name}  {created}")
        click.echo(f"  {text}")
        if likes:
            click.echo(f"  赞{likes}")
        click.echo()


# ── Repost list ─────────────────────────────────────────────────────


def render_repost_list(reposts: list[dict], *, count: int = 10) -> None:
    """Render repost entries. Used by reposts command."""
    if not reposts:
        click.echo("暂无转发")
        return
    for r in reposts[:count]:
        user = r.get("user", {})
        name = user.get("screen_name", "未知")
        text = strip_html(r.get("text", ""))
        created = r.get("created_at", "")
        click.echo(f"@{name}  {created}")
        click.echo(f"  {text}")
        click.echo()

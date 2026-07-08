"""Auth commands: login, logout, status."""

from __future__ import annotations

import json

import click

from ._common import format_count, handle_command, require_auth, structured_output_options


@click.command()
@click.option("--qrcode", is_flag=True, help="直接使用二维码扫码登录（跳过浏览器 Cookie 提取）")
@click.option("--cookie-source", type=str, default=None, help="指定浏览器 (chrome/firefox/edge/brave/arc/...)")
def login(qrcode, cookie_source):
    """登录微博（自动提取浏览器 Cookie 或 --qrcode 扫码）"""
    from ..auth import extract_browser_credential, get_credential, qr_login

    if qrcode:
        # Skip browser cookies, go straight to QR login
        try:
            cred = qr_login()
            if cred:
                click.echo("登录成功")
            else:
                click.echo("error: 登录失败", err=True)
        except Exception as e:
            click.echo(f"error: 登录失败: {e}", err=True)
        return

    if cookie_source:
        # Try specific browser only
        cred = extract_browser_credential(cookie_source=cookie_source)
        if cred:
            click.echo(f"已从 {cookie_source} 提取 Cookie 并登录")
        else:
            click.echo(f"未在 {cookie_source} 找到有效 Cookie", err=True)
            click.echo("提示: 使用 weibo login --qrcode 扫码登录", err=True)
        return

    # Default: try saved → browser → QR
    cred = get_credential()
    if cred:
        click.echo("已登录（如需重新登录请先执行 weibo logout）")
        return

    try:
        cred = qr_login()
        if cred:
            click.echo("登录成功")
        else:
            click.echo("error: 登录失败", err=True)
    except Exception as e:
        click.echo(f"error: 登录失败: {e}", err=True)


@click.command()
def logout():
    """清除已保存的登录凭证"""
    from ..auth import clear_credential

    clear_credential()
    click.echo("已清除登录凭证")


@click.command()
@structured_output_options
def status(as_json, as_yaml):
    """查看当前登录状态"""
    from ._common import get_credential

    cred = get_credential()
    info = {
        "authenticated": cred is not None,
        "cookie_count": len(cred.cookies) if cred else 0,
    }
    if as_json:
        click.echo(json.dumps(info, indent=2))
    elif as_yaml:
        try:
            import yaml
            click.echo(yaml.dump(info, allow_unicode=True, default_flow_style=False))
        except ImportError:
            click.echo(json.dumps(info, indent=2))
    else:
        if cred:
            click.echo(f"authenticated cookies={len(cred.cookies)}")
        else:
            click.echo("unauthenticated")


@click.command()
@structured_output_options
def me(as_json, as_yaml):
    """查看个人资料"""
    cred = require_auth()

    def _render(data):
        user = data.get("user", data)
        if not user.get("screen_name"):
            click.echo("无法获取个人资料")
            return
        lines = []
        lines.append(f"昵称: {user['screen_name']}")
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
        if user.get("verified_reason"):
            lines.append(f"认证: {user['verified_reason']}")
        click.echo("\n".join(lines))

    def _action(client):
        # /ajax/profile/me is 404; get_config's data has no uid. The reliable
        # source is the x-log-uid response header set on authenticated ajax calls.
        uid = client.get_current_uid()
        if not uid:
            click.echo("error: 无法获取当前 uid，请确认已登录（weibo login）", err=True)
            raise SystemExit(1)
        return client.get_profile(uid)

    handle_command(cred, action=_action, render=_render, as_json=as_json, as_yaml=as_yaml)

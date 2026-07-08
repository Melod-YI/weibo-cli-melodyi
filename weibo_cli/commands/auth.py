"""Auth commands: login, logout, status."""

from __future__ import annotations

import json

import click

from ._common import format_count, handle_command, require_auth, structured_output_options


@click.group(invoke_without_command=True)
@click.option("--qrcode", is_flag=True, help="直接使用二维码扫码登录（跳过浏览器 Cookie 提取，终端阻塞，人用）")
@click.option("--cookie-source", type=str, default=None, help="指定浏览器 (chrome/firefox/edge/brave/arc/...)")
@click.pass_context
def login(ctx, qrcode, cookie_source):
    """登录微博（自动提取浏览器 Cookie 或 --qrcode 扫码；agent 用 qr-start/qr-done）"""
    if ctx.invoked_subcommand is not None:
        return  # 交给子命令处理
    from ..auth import extract_browser_credential, get_credential, qr_login

    if qrcode:
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
        cred = extract_browser_credential(cookie_source=cookie_source)
        if cred:
            click.echo(f"已从 {cookie_source} 提取 Cookie 并登录")
        else:
            click.echo(f"未在 {cookie_source} 找到有效 Cookie", err=True)
            click.echo("提示: 使用 weibo login --qrcode 或 weibo login qr-start 扫码登录", err=True)
        return

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


@login.command(name="qr-start")
@click.option("--png", required=True, help="二维码 PNG 输出路径")
def qr_start(png):
    """生成二维码登录图片（非交互），配合 weibo login qr-done 完成。"""
    import sys

    import httpx

    from ..auth import _qr_get_session, _write_qr_png, save_qr_session
    from ..constants import PASSPORT_HEADERS, PASSPORT_URL, QR_SESSION_FILE, QR_SESSION_TTL_S

    with httpx.Client(
        base_url=PASSPORT_URL,
        headers=dict(PASSPORT_HEADERS),
        follow_redirects=True,
        timeout=httpx.Timeout(30),
    ) as client:
        try:
            session = _qr_get_session(client)
        except Exception as e:
            click.echo(f"error: 获取二维码会话失败: {e}", err=True)
            sys.exit(1)

    try:
        _write_qr_png(session["scan_url"], png)
    except Exception as e:
        click.echo(f"error: 生成 PNG 失败: {e}", err=True)
        sys.exit(1)

    save_qr_session(session)
    click.echo(f"image: {png}")
    click.echo(f"qrid: {session['qrid']}")
    click.echo(f"session: {QR_SESSION_FILE}")
    click.echo(f"qr_expires_in: {QR_SESSION_TTL_S}")


@login.command(name="qr-done")
@click.option("--timeout", default=60, help="轮询超时秒数（默认60，用户已扫码应很快）")
@click.option("--session", "session_path", default=None, help="会话文件路径（默认 ~/.config/weibo-cli/qr_session.json）")
def qr_done(timeout, session_path):
    """完成二维码登录（轮询扫码结果并保存凭证）。"""
    import sys
    import time

    import httpx

    from ..auth import (
        QRExpiredError,
        _qr_poll_and_finalize,
        clear_qr_session,
        load_qr_session,
    )
    from ..constants import (
        CREDENTIAL_FILE,
        PASSPORT_HEADERS,
        PASSPORT_URL,
        QR_SESSION_FILE,
        QR_SESSION_TTL_S,
        RETCODE_SUCCESS,
    )

    f = session_path or QR_SESSION_FILE
    session = load_qr_session(f)
    if not session:
        click.echo("error: 未找到 QR 会话，请先运行 weibo login qr-start --png <path>", err=True)
        sys.exit(1)

    created_at = session.get("created_at", 0)
    if time.time() - created_at > QR_SESSION_TTL_S:
        clear_qr_session(f)
        click.echo("error: qr session 已过期，请重新运行 weibo login qr-start", err=True)
        sys.exit(1)

    cookies = session["cookies"]
    csrf = session["csrf_token"]
    qrid = session["qrid"]
    headers = {**dict(PASSPORT_HEADERS), "x-csrf-token": csrf}

    with httpx.Client(
        base_url=PASSPORT_URL,
        headers=headers,
        cookies=cookies,
        follow_redirects=True,
        timeout=httpx.Timeout(30),
    ) as client:
        try:
            def _on_status(retcode, msg):
                if retcode != RETCODE_SUCCESS:
                    click.echo(f"status: {msg}", err=True)

            _qr_poll_and_finalize(client, qrid, on_status=_on_status, poll_timeout=timeout)
        except QRExpiredError:
            clear_qr_session(f)
            click.echo("error: 二维码已过期，请重新运行 weibo login qr-start", err=True)
            sys.exit(1)
        except TimeoutError:
            click.echo("status: 轮询超时（会话已保留，可再次运行 weibo login qr-done 重试）", err=True)
            sys.exit(1)

    click.echo("status: success")
    click.echo(f"credential saved: {CREDENTIAL_FILE}")
    clear_qr_session(f)


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

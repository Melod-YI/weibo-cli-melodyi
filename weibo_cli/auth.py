"""Authentication for Weibo.

Strategy:
1. Try loading saved credential from ~/.config/weibo-cli/credential.json
2. Try extracting cookies from local browsers via rookiepy
3. Fallback: QR code login in terminal

QR Login Flow (reverse-engineered from passport.weibo.com):
1. GET  /sso/signin → obtain X-CSRF-TOKEN cookie
2. GET  /sso/v2/qrcode/image → get qrid + QR image URL
3. Render QR code in terminal (data = scan URL with qrid)
4. Poll GET /sso/v2/qrcode/check every 2s until success
5. On success, follow crossdomain URL to obtain session cookies
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import qrcode
import rookiepy

from .constants import (
    CONFIG_DIR,
    CREDENTIAL_FILE,
    PASSPORT_HEADERS,
    PASSPORT_URL,
    QR_ALT_URL,
    QR_CHECK_URL,
    QR_ENTRY,
    QR_IMAGE_URL,
    QR_REDIRECT_URL,
    QR_SESSION_FILE,
    QR_SOURCE,
    QR_VERSION,
    RETCODE_QR_EXPIRED,
    RETCODE_QR_NOT_SCANNED,
    RETCODE_QR_SCANNED,
    RETCODE_SUCCESS,
    SSO_SIGNIN_URL,
)
from .exceptions import QRExpiredError

logger = logging.getLogger(__name__)

# ── ANSI SGR for forced QR colors ───────────────────────────────────
# 256-color codes: 0 = black, 15 = bright white. Forced so QR renders
# black-on-white regardless of the terminal's default fg/bg (dark-bg
# Windows bash would otherwise invert the half-block colors and break
# scanning). See _render_qr_half_blocks.
_QR_BLACK_FG = "\033[38;5;0m"
_QR_WHITE_FG = "\033[38;5;15m"
_QR_BLACK_BG = "\033[48;5;0m"
_QR_WHITE_BG = "\033[48;5;15m"
_QR_RESET = "\033[0m"

# Credential TTL: warn and attempt refresh after 7 days
CREDENTIAL_TTL_DAYS = 7
_CREDENTIAL_TTL_SECONDS = CREDENTIAL_TTL_DAYS * 86400

# QR poll config
POLL_INTERVAL_S = 2
POLL_TIMEOUT_S = 240  # 4 minutes


# ── Credential data class ───────────────────────────────────────────


class Credential:
    """Holds Weibo session cookies.

    `cookies` are the `.weibo.com`-domain session cookies (used for weibo.com/ajax/*
    and passport). `mobile_cookies`, when present, are the `.weibo.cn`-domain
    session cookies obtained via the QR cross-domain cdurl exchange; they back
    `m.weibo.cn` endpoints (keyword search) which require a separate .weibo.cn
    session that .weibo.com cookies cannot cover (different registrable domain).
    """

    def __init__(self, cookies: dict[str, str], mobile_cookies: dict[str, str] | None = None):
        self.cookies = cookies
        self.mobile_cookies = mobile_cookies or {}

    @property
    def is_valid(self) -> bool:
        return bool(self.cookies)

    def to_dict(self) -> dict[str, Any]:
        d = {"cookies": self.cookies, "saved_at": time.time()}
        if self.mobile_cookies:
            d["mobile_cookies"] = self.mobile_cookies
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Credential:
        return cls(cookies=data.get("cookies", {}), mobile_cookies=data.get("mobile_cookies", {}))

    def as_cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


# ── Credential persistence ──────────────────────────────────────────


def save_credential(credential: Credential) -> None:
    """Save credential to config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIAL_FILE.write_text(json.dumps(credential.to_dict(), indent=2, ensure_ascii=False))
    CREDENTIAL_FILE.chmod(0o600)
    logger.info("Credential saved to %s", CREDENTIAL_FILE)


def load_credential() -> Credential | None:
    """Load credential from saved file with TTL-based auto-refresh."""
    if not CREDENTIAL_FILE.exists():
        return None
    try:
        data = json.loads(CREDENTIAL_FILE.read_text())
        cred = Credential.from_dict(data)
        if not cred.is_valid:
            return None

        # Check TTL — auto-refresh if stale
        saved_at = data.get("saved_at", 0)
        if saved_at and (time.time() - saved_at) > _CREDENTIAL_TTL_SECONDS:
            logger.info("Credential older than %d days, attempting browser refresh", CREDENTIAL_TTL_DAYS)
            fresh = extract_browser_credential()
            if fresh:
                logger.info("Auto-refreshed credential from browser")
                return fresh
            logger.warning("Cookie refresh failed; using existing cookies (age: %d+ days)", CREDENTIAL_TTL_DAYS)
        return cred
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to load saved credential: %s", e)
    return None


def clear_credential() -> None:
    """Remove saved credential file."""
    if CREDENTIAL_FILE.exists():
        CREDENTIAL_FILE.unlink()
        logger.info("Credential removed: %s", CREDENTIAL_FILE)


# ── Browser cookie extraction ───────────────────────────────────────


def _normalize_browser_name(name: str) -> str:
    return name.lower().replace(" ", "").replace("_", "")


# (display name, rookiepy function name). Iteration order matters for auto mode —
# common browsers first.
_BROWSER_LOADERS: list[tuple[str, str]] = [
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Firefox", "firefox"),
    ("Brave", "brave"),
    ("Chromium", "chromium"),
    ("Opera", "opera"),
    ("Opera GX", "opera_gx"),
    ("Vivaldi", "vivaldi"),
    ("Arc", "arc"),
    ("LibreWolf", "librewolf"),
    ("Internet Explorer", "internet_explorer"),
]


def extract_browser_credential(
    cookie_source: str | None = None, *, errors_out: dict[str, str] | None = None
) -> Credential | None:
    """Extract Weibo cookies from local browsers via rookiepy (Rust backend).

    rookiepy decrypts Chrome v127+ App-Bound Encryption cookies, but on
    Windows Chrome v130+ requires the calling process to run as admin —
    otherwise it raises RuntimeError.

    Per-browser failure reasons are written into *errors_out* (if given) and
    logged at WARNING, so callers can surface them instead of a generic message.
    """
    logger.info("Extracting browser cookies (source=%s)", cookie_source or "auto")

    browsers = _BROWSER_LOADERS
    if cookie_source:
        target = _normalize_browser_name(cookie_source)
        browsers = [
            (n, fn) for n, fn in browsers
            if _normalize_browser_name(n) == target or _normalize_browser_name(fn) == target
        ]
        if not browsers:
            logger.warning("Unsupported browser: %s", cookie_source)
            if errors_out is not None:
                errors_out[cookie_source] = f"unsupported browser: {cookie_source}"
            return None

    for name, fn_name in browsers:
        loader = getattr(rookiepy, fn_name, None)
        if loader is None:
            continue
        try:
            raw = loader(domains=["weibo.com", "sina.com"])
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            logger.warning("cookie extract failed [%s]: %s", name, reason)
            if errors_out is not None:
                errors_out[name] = reason
            continue

        cookies = {
            c.get("name"): c.get("value")
            for c in raw
            if c.get("name") and c.get("value") is not None
            and ("weibo.com" in (c.get("domain") or "") or "sina.com" in (c.get("domain") or ""))
        }
        if not cookies:
            logger.debug("%s: no weibo cookies found", name)
            if errors_out is not None:
                errors_out[name] = "no weibo cookies found"
            continue

        logger.info("Found cookies in %s (%d cookies)", name, len(cookies))
        cred = Credential(cookies=cookies)
        save_credential(cred)
        return cred

    return None


# ── QR Code terminal rendering ──────────────────────────────────────


def _render_qr_half_blocks(matrix: list[list[bool]], *, invert: bool = False) -> str:
    """Render QR matrix using Unicode half-block characters (▀▄█), forcing
    black-on-white via ANSI SGR so the result is scannable on any terminal
    regardless of its default fg/bg colors (incl. dark-bg Windows bash).

    QR dark module (matrix True) → BLACK ink; light module → WHITE ink
    (or swapped when invert=True). Half-block semantics:
      ▀ = top half = fg, bottom half = bg
      ▄ = bottom half = fg, top half = bg
      █ = full cell = fg
    """
    if not matrix:
        return ""

    # Add 1-module quiet zone (light → white, with forced colors it stays white)
    size = len(matrix)
    padded = [[False] * (size + 2)]
    for row in matrix:
        padded.append([False] + list(row) + [False])
    padded.append([False] * (size + 2))
    matrix = padded
    rows = len(matrix)

    # Check terminal width
    term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    qr_width = len(matrix[0])
    if qr_width > term_cols:
        logger.warning("Terminal too narrow (%d) for QR (%d)", term_cols, qr_width)
        return ""

    # FG vs BG roles (escape codes differ: 38; vs 48;)
    dark_fg, dark_bg = (_QR_BLACK_FG, _QR_BLACK_BG) if not invert else (_QR_WHITE_FG, _QR_WHITE_BG)
    light_fg, light_bg = (_QR_WHITE_FG, _QR_WHITE_BG) if not invert else (_QR_BLACK_FG, _QR_BLACK_BG)
    lines: list[str] = []
    for y in range(0, rows, 2):
        top_row = matrix[y]
        bottom_row = matrix[y + 1] if y + 1 < rows else [False] * len(top_row)
        out: list[str] = []
        cur_fg = cur_bg = None
        for x in range(len(top_row)):
            top = top_row[x]
            bottom = bottom_row[x]
            if top and bottom:
                ch, fg, bg = "█", dark_fg, None
            elif not top and not bottom:
                ch, fg, bg = "█", light_fg, None
            elif top and not bottom:
                ch, fg, bg = "▀", dark_fg, light_bg  # fg=top(dark), bg=bottom(light)
            else:  # not top and bottom
                ch, fg, bg = "▄", dark_fg, light_bg  # fg=bottom(dark), bg=top(light)
            if fg != cur_fg or bg != cur_bg:
                if fg is not None and fg != cur_fg:
                    out.append(fg)
                    cur_fg = fg
                if bg is not None and bg != cur_bg:
                    out.append(bg)
                    cur_bg = bg
            out.append(ch)
        out.append(_QR_RESET)
        lines.append("".join(out))
    return "\n".join(lines)


def _display_qr_in_terminal(data: str, *, invert: bool = False) -> bool:
    """Display *data* as a QR code in the terminal using Unicode half-blocks,
    forcing black-on-white so it scans on any terminal.

    Returns True on success.
    """
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.get_matrix()

    rendered = _render_qr_half_blocks(modules, invert=invert)
    if rendered:
        print(rendered)
        return True

    # Fallback to basic ASCII — still force white bg + black fg so the
    # dark/light cells read correctly on dark terminals.
    import io

    buf = io.StringIO()
    qr2 = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr2.add_data(data)
    qr2.make(fit=True)
    qr2.print_ascii(invert=not invert, out=buf)
    print(f"{_QR_WHITE_BG}{_QR_BLACK_FG}{buf.getvalue()}{_QR_RESET}")
    return True


# ── QR session persistence ──────────────────────────────────────────


def save_qr_session(session: dict, png_path: str | None = None) -> None:
    """Persist QR session (qrid + csrf + passport cookies) for qr-done.

    If *png_path* is given, it is resolved to an absolute path and stored so a
    later qr-start/qr-done can clean up the residual QR image.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "qrid": session["qrid"],
        "csrf_token": session["csrf_token"],
        "cookies": session["cookies"],
        "scan_url": session["scan_url"],
        "created_at": time.time(),
    }
    if png_path:
        payload["png_path"] = os.path.abspath(png_path)
    QR_SESSION_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    try:
        QR_SESSION_FILE.chmod(0o600)
    except OSError:
        # chmod is a no-op on Windows; ignore
        pass
    logger.info("QR session saved to %s", QR_SESSION_FILE)


def load_qr_session(path=None) -> dict | None:
    """Load QR session from file. Returns None if missing/unreadable."""
    f = path or QR_SESSION_FILE
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load QR session: %s", e)
        return None


def clear_qr_session(path=None) -> str | None:
    """Remove QR session file and any associated residual QR image.

    Returns the absolute path of the PNG that was actually deleted, or None if
    no image was removed (none recorded, or the file was already gone). The
    session file itself is always removed when present.
    """
    f = path or QR_SESSION_FILE
    removed_png: str | None = None
    if f.exists():
        # Best-effort: read recorded png_path and delete the residual image too.
        png_path = None
        try:
            data = json.loads(f.read_text())
            png_path = data.get("png_path")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read QR session for image cleanup: %s", e)
        if png_path and os.path.exists(png_path):
            try:
                os.remove(png_path)
                removed_png = png_path
                logger.info("Removed residual QR image: %s", png_path)
            except OSError as e:
                logger.warning("Failed to remove QR image %s: %s", png_path, e)
        f.unlink()
        logger.info("QR session removed: %s", f)
    return removed_png


def _write_qr_png(data: str, path: str) -> None:
    """Render *data* as a QR PNG image to *path*."""
    img = qrcode.make(data)
    img.save(path)


# ── QR Login flow (decomposed for reuse by qr-start / qr-done) ──────


def _qr_get_session(client: httpx.Client) -> dict:
    """Steps 1-2: obtain CSRF token, qrid, scan_url, and a cookie snapshot.

    Returns {qrid, scan_url, csrf_token, cookies}. Cookies are snapshotted
    AFTER the image request (it may set additional cookies).
    """
    # Step 1: Get CSRF token by visiting login page
    logger.info("Getting CSRF token from login page...")
    resp = client.get(
        SSO_SIGNIN_URL,
        params={"entry": QR_ENTRY, "source": QR_SOURCE, "url": QR_REDIRECT_URL},
    )
    resp.raise_for_status()
    csrf_token = client.cookies.get("X-CSRF-TOKEN")
    if not csrf_token:
        raise RuntimeError("Failed to obtain X-CSRF-TOKEN from passport.weibo.com")
    client.headers["x-csrf-token"] = csrf_token
    logger.info("Got CSRF token: %s...", csrf_token[:20])

    # Step 2: Get QR code
    logger.info("Requesting QR code...")
    resp = client.get(QR_IMAGE_URL, params={"entry": QR_ENTRY, "size": "180"})
    resp.raise_for_status()
    qr_data = resp.json()
    if qr_data.get("retcode") != RETCODE_SUCCESS:
        raise RuntimeError(f"Failed to get QR code: {qr_data.get('msg', 'Unknown error')}")

    qrid = qr_data["data"]["qrid"]
    image_url = qr_data["data"]["image"]
    parsed = urlparse(image_url)
    qs = parse_qs(parsed.query)
    scan_url = qs.get("data", [f"https://passport.weibo.cn/signin/qrcode/scan?qr={qrid}"])[0]
    logger.info("Got qrid: %s", qrid)

    # Snapshot cookies AFTER image request
    cookies = dict(client.cookies.items())
    return {"qrid": qrid, "scan_url": scan_url, "csrf_token": csrf_token, "cookies": cookies}


def _exchange_crossdomain(cross_url: str, alt: str) -> dict:
    """Follow crossdomain URL + alt token exchange, return collected cookies."""
    cookies: dict[str, str] = {}
    ua = PASSPORT_HEADERS["User-Agent"]
    if cross_url:
        try:
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30), headers={"User-Agent": ua}) as c:
                c.get(cross_url)
                for k, v in c.cookies.items():
                    cookies[k] = v
        except Exception as e:
            logger.warning("Cross-domain follow failed: %s", e)
    if alt:
        try:
            alt_url = QR_ALT_URL.format(alt=alt)
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30), headers={"User-Agent": ua}) as c:
                c.get(alt_url)
                for k, v in c.cookies.items():
                    cookies[k] = v
        except Exception as e:
            logger.warning("Alt token exchange failed: %s", e)
    return cookies


def _exchange_mobile_cookies(
    cross_url: str, passport_cookies: dict[str, str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Single-pass cross-domain exchange returning BOTH `.weibo.com` and
    `.weibo.cn` cookie buckets.

    The SSO `alt` token is single-use: the first GET of `cross_url` (data.url =
    passport.weibo.com/sso/v2/login?...&alt=...) returns 302 (sets .weibo.com
    cookies + Location carrying the cdurl); a second GET returns 200 with no
    redirect. So `_exchange_crossdomain` and a separate data.url probe cannot
    both run — this function probes data.url exactly ONCE, `follow_redirects=
    False`, capturing:

      * `.weibo.com` cookies from the 302 Set-Cookie (these previously came from
        `_exchange_crossdomain`'s data.url follow);
      * the `cdurl` param from the 302 Location.

    It then fetches the cdurl directly (`passport.weibo.cn/sso/crossdomain?...
    &ticket=...`), bypassing the `login.sina.com.cn` hop that 403s, and captures
    `.weibo.cn` cookies (the m.weibo.cn session that search needs).

    Set-Cookie headers are parsed directly (not via the cookie jar) so the
    injected `.weibo.com` session cookies can't collide with the `.weibo.cn`
    ones of the same name; cookies are bucketed by domain (`weibo.com` vs
    `weibo.cn`).

    Returns (weibo_cookies, mobile_cookies); both {} on any failure. Login
    still succeeds via passport_cookies + weibo_cookies even if mobile fails —
    only m.weibo.cn search stays unavailable in that case.
    """
    weibo: dict[str, str] = {}
    mobile: dict[str, str] = {}
    if not cross_url:
        return weibo, mobile
    ua = PASSPORT_HEADERS["User-Agent"]
    probe_headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Referer": f"{PASSPORT_URL}/",
    }
    sess = passport_cookies or {}

    def _bucket_cookies(response: httpx.Response, bucket: dict[str, str], domain_marker: str) -> None:
        host = response.url.host or ""
        for raw in response.headers.get_list("set-cookie"):
            name, _, val = raw.partition("=")
            name = name.strip()
            val = val.split(";", 1)[0]
            domain = ""
            for part in raw.split(";"):
                p = part.strip()
                if p.lower().startswith("domain="):
                    domain = p.split("=", 1)[1].strip()
            if name and (domain_marker in domain or domain_marker in host):
                bucket[name] = val

    try:
        # Step 1: probe data.url without following → 302 sets .weibo.com cookies
        # and carries the cdurl in Location. The passport session cookies are
        # required for the 302 to be issued.
        with httpx.Client(
            follow_redirects=False, timeout=httpx.Timeout(30),
            headers=probe_headers, cookies=sess,
        ) as probe:
            resp = probe.get(cross_url)
            _bucket_cookies(resp, weibo, "weibo.com")
            location = resp.headers.get("location", "")
        if not location:
            logger.debug("mobile cdurl: no Location from %s (status %s)", cross_url, resp.status_code)
            return weibo, mobile
        cdurl = unquote(parse_qs(urlparse(location).query).get("cdurl", [""])[0])
        if not cdurl:
            logger.debug("mobile cdurl: no cdurl param in Location %s", location[:120])
            return weibo, mobile

        # Step 2: fetch cdurl directly (bypass login.sina.com.cn) → .weibo.cn cookies
        def _collect_mobile(response: httpx.Response) -> None:
            _bucket_cookies(response, mobile, "weibo.cn")

        with httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(30),
            headers=probe_headers, cookies=sess,
            event_hooks={"response": [_collect_mobile]},
        ) as c:
            c.get(cdurl)
        if mobile:
            logger.info("Obtained .weibo.cn mobile cookies: %s", sorted(mobile))
        else:
            logger.warning("Mobile cdurl exchange returned no .weibo.cn cookies: %s", cdurl[:120])
    except Exception as e:
        logger.warning("Mobile (.weibo.cn) cross-domain exchange failed: %s", e)
    return weibo, mobile


def _qr_poll_and_finalize(
    client: httpx.Client,
    qrid: str,
    *,
    on_status=None,
    poll_timeout: float = POLL_TIMEOUT_S,
) -> Credential:
    """Steps 4-5: poll /sso/v2/qrcode/check, on success exchange cookies and save.

    on_status(retcode, msg) is called on each retcode change (for progress).
    Raises QRExpiredError on EXPIRED retcode; TimeoutError on poll exhaustion.
    """
    start_time = time.time()
    last_status = None

    while (time.time() - start_time) < poll_timeout:
        try:
            resp = client.get(
                QR_CHECK_URL,
                params={
                    "entry": QR_ENTRY,
                    "source": QR_SOURCE,
                    "url": QR_REDIRECT_URL,
                    "qrid": qrid,
                    "rid": "",
                    "ver": QR_VERSION,
                },
            )
            resp.raise_for_status()
            check_data = resp.json()
            retcode = check_data.get("retcode")
            msg = check_data.get("msg", "")

            if retcode != last_status:
                logger.info("QR check: retcode=%s msg=%s", retcode, msg)
                last_status = retcode
                if on_status:
                    on_status(retcode, msg)

            if retcode == RETCODE_SUCCESS:
                data = check_data.get("data", {})
                cross_url = data.get("url", "")
                passport_cookies = dict(client.cookies.items())
                # Single-pass exchange: data.url's alt token is single-use, so
                # probe it once for both .weibo.com cookies and the cdurl, then
                # fetch the cdurl for .weibo.cn cookies (m.weibo.cn session).
                weibo_cross, mobile_cookies = _exchange_mobile_cookies(cross_url, passport_cookies)
                # Alt token follow (login.sina.com.cn); cross_url="" skips the
                # now-redundant data.url follow inside _exchange_crossdomain.
                alt_cookies = _exchange_crossdomain("", data.get("alt", ""))
                cookies = {**passport_cookies, **weibo_cross, **alt_cookies}
                if not cookies:
                    raise RuntimeError("Login succeeded but no cookies were obtained")
                credential = Credential(cookies=cookies, mobile_cookies=mobile_cookies)
                save_credential(credential)
                return credential
            elif retcode == RETCODE_QR_NOT_SCANNED:
                pass  # keep polling
            elif retcode == RETCODE_QR_SCANNED:
                pass  # scanned, waiting for confirm
            elif retcode == RETCODE_QR_EXPIRED:
                raise QRExpiredError()
            # unknown retcode: keep polling

        except httpx.TimeoutException:
            logger.debug("QR check timeout, retrying...")

        time.sleep(POLL_INTERVAL_S)

    raise TimeoutError(f"QR poll timed out after {poll_timeout}s")


def qr_login() -> Credential:
    """Full blocking QR login flow for terminal (human use via --qrcode).

    Reuses _qr_get_session + _qr_poll_and_finalize.
    """
    with httpx.Client(
        base_url=PASSPORT_URL,
        headers=dict(PASSPORT_HEADERS),
        follow_redirects=True,
        timeout=httpx.Timeout(30),
    ) as client:
        session = _qr_get_session(client)
        import click
        click.echo("请使用微博APP扫描以下二维码登录：", err=True)
        click.echo("打开微博手机APP → 我的页面 → 扫一扫", err=True)
        _display_qr_in_terminal(session["scan_url"])
        click.echo(f"等待扫码中...（超时: {POLL_TIMEOUT_S // 60} 分钟）", err=True)

        def _on_status(retcode, msg):
            if retcode == RETCODE_QR_SCANNED:
                click.echo("已扫码，请在手机上确认登录", err=True)
            elif retcode == RETCODE_SUCCESS:
                click.echo("扫码成功，正在完成登录", err=True)

        cred = _qr_poll_and_finalize(client, session["qrid"], on_status=_on_status)
        click.echo("登录成功，凭证已保存")
        return cred


# ── Unified get_credential ──────────────────────────────────────────


def get_credential() -> Credential | None:
    """Try all auth methods and return credential.

    1. Saved credential file
    2. Browser cookie extraction
    """
    cred = load_credential()
    if cred:
        logger.info("Loaded credential from %s", CREDENTIAL_FILE)
        return cred

    cred = extract_browser_credential()
    if cred:
        logger.info("Extracted credential from browser")
        return cred

    return None

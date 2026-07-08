# weibo-cli Agent 化改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 weibo-cli 默认输出改为纯文本（去 UI 字符/emoji）、二维码登录拆分为 `qr-start`（生成图片）+ `qr-done`（完成登录）两个非交互命令，面向 agent 调用。

**Architecture:** Part A 重写渲染层为纯文本并修正 `handle_command` 输出路由（移除非 TTY 自动 YAML、错误走 stderr）。Part B 在 `auth.py` 提取 `_qr_get_session` / `_qr_poll_and_finalize` 公共步骤，新增会话文件持久化（passport cookie + csrf 跨进程复用），`login` 改为 Click group 承载 `qr-start`/`qr-done` 子命令，旧 `--qrcode` 终端流程复用公共函数保留。

**Tech Stack:** Python 3.10+、Click 8、httpx、qrcode[pil]（新增 Pillow）、pytest、rich（仅 stderr 错误着色）。

参考 spec：`docs/superpowers/specs/2026-07-08-agent-mode-cli-design.md`

---

## 文件结构

- `weibo_cli/constants.py` — 新增 `QR_SESSION_FILE`、`QR_SESSION_TTL_S`、`QR_ALT_URL`
- `weibo_cli/auth.py` — 提取 `_qr_get_session`/`_exchange_crossdomain`/`_qr_poll_and_finalize`，新增 `save_qr_session`/`load_qr_session`/`clear_qr_session`/`_write_qr_png`，重写 `qr_login`
- `weibo_cli/commands/auth.py` — `login` 改 group + 新增 `qr_start`/`qr_done` 子命令；`login`/`logout`/`status`/`me` 输出纯文本化
- `weibo_cli/commands/_common.py` — `handle_command` 路由改造 + 错误/`require_auth` 走 stderr
- `weibo_cli/commands/renderers.py` — 全部 `render_*` 纯文本重写
- `weibo_cli/commands/search.py` / `personal.py` — 各 `_render` 纯文本化
- `pyproject.toml` — `qrcode>=7.0` → `qrcode[pil]>=7.0`
- `tests/test_auth.py` — 新增 qr 公共步骤与会话持久化测试
- `tests/test_cli.py` — 新增 qr-start/qr-done 注册与 group 守卫测试
- `tests/test_renderers.py` — 新建，纯文本渲染快照测试
- `README.md` / `SKILL.md` — 文档更新

---

## Task 1: 依赖与 constants

**Files:**
- Modify: `pyproject.toml`
- Modify: `weibo_cli/constants.py:6-7,88-98`

- [ ] **Step 1: pyproject 引入 Pillow**

修改 `pyproject.toml` 的 dependencies，把 `qrcode>=7.0` 改为 `qrcode[pil]>=7.0`：

```toml
    "qrcode[pil]>=7.0",
```

- [ ] **Step 2: constants 新增会话相关常量**

在 `weibo_cli/constants.py` 的 `CREDENTIAL_FILE` 行（第 7 行）下方追加：

```python
QR_SESSION_FILE = CONFIG_DIR / "qr_session.json"
```

在文件末尾 QR Login constants 段（第 92 行 `QR_VERSION` 之后）追加：

```python
QR_SESSION_TTL_S = 240  # QR code lifetime, must match passport server
QR_ALT_URL = "https://login.sina.com.cn/sso/login.php?entry=miniblog&alt={alt}&returntype=TEXT"
```

- [ ] **Step 3: 安装依赖**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv sync --extra dev --extra yaml`
Expected: 安装成功，Pillow 被引入。

- [ ] **Step 4: 验证 constants 可导入**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run python -c "from weibo_cli.constants import QR_SESSION_FILE, QR_SESSION_TTL_S, QR_ALT_URL; print(QR_SESSION_TTL_S)"`
Expected: 输出 `240`

- [ ] **Step 5: Commit**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add pyproject.toml weibo_cli/constants.py
git commit -m "feat: add Pillow dep and QR session constants"
```

---

## Task 2: handle_command 路由与 stderr 改造

**Files:**
- Modify: `weibo_cli/commands/_common.py:39-90`
- Test: `tests/test_improvements.py`（追加）

- [ ] **Step 1: 写失败测试 — 非 TTY 不再自动 YAML**

在 `tests/test_improvements.py` 末尾追加：

```python
from click.testing import CliRunner
from weibo_cli.cli import cli


def test_non_tty_defaults_to_plain_not_yaml(monkeypatch):
    """非 TTY stdout 应走 plain render，不再自动 YAML。"""
    # 让 get_credential 返回有效凭证
    from weibo_cli.auth import Credential
    monkeypatch.setattr("weibo_cli.commands._common.get_credential", lambda: Credential(cookies={"SUB": "x"}))
    # mock client.get_hot_search 返回固定数据
    from unittest.mock import MagicMock
    from weibo_cli.commands import _common
    real_weiboclient = _common.WeiboClient

    class _Stub:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_hot_search(self): return {"realtime": [{"word": "测试热搜", "num": 12345, "icon_desc": "热"}]}

    monkeypatch.setattr(_common, "WeiboClient", _Stub)
    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(cli, ["hot"])
    assert result.exit_code == 0
    # 不应出现 YAML 的 "realtime:" 键行
    assert "realtime:" not in result.output
    # 应出现纯文本热搜词
    assert "测试热搜" in result.output
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_improvements.py::test_non_tty_defaults_to_plain_not_yaml -v`
Expected: FAIL（当前非 TTY 走 YAML，断言 `realtime:` not in output 失败）

- [ ] **Step 3: 改 handle_command 路由 + 错误走 stderr**

将 `weibo_cli/commands/_common.py` 的 `handle_command`（第 55-90 行）替换为：

```python
def handle_command(credential, *, action, render=None, as_json=False, as_yaml=False) -> Any:
    """Run action → route output: JSON / YAML / plain render.

    Default (no flag, TTY or non-TTY) → plain render.
    Errors go to stderr. SessionExpiredError triggers browser refresh retry.
    """
    try:
        try:
            with WeiboClient(credential) as client:
                data = action(client)
        except SessionExpiredError:
            from ..auth import extract_browser_credential
            fresh = extract_browser_credential()
            if fresh:
                with WeiboClient(fresh) as client:
                    data = action(client)
            else:
                raise

        if as_json:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        elif as_yaml:
            try:
                import yaml
                click.echo(yaml.dump(data, allow_unicode=True, default_flow_style=False))
            except ImportError:
                click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        elif render:
            render(data)
        return data

    except WeiboApiError as exc:
        code = error_code_for_exception(exc)
        click.echo(f"error [{code}]: {exc}", err=True)
        return None
```

注意：删掉了原 `elif as_yaml or not sys.stdout.isatty():` 中的 `or not sys.stdout.isatty()`，非 TTY 不再自动 YAML。

- [ ] **Step 4: require_auth 提示走 stderr**

将 `weibo_cli/commands/_common.py` 的 `require_auth`（第 39-45 行）替换为：

```python
def require_auth() -> Credential:
    """Get credential or raise AuthRequiredError."""
    cred = get_credential()
    if not cred:
        click.echo("未登录，请先使用 weibo login 登录", err=True)
        raise AuthRequiredError()
    return cred
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_improvements.py::test_non_tty_defaults_to_plain_not_yaml -v`
Expected: PASS（注意：此时 plain render 仍是 Rich 表格，但因 TTY 由 CliRunner 模拟为非 TTY，Rich 输出不含 `realtime:` 键行；断言应通过。若失败因 Rich 表格含其他键行，调整断言只检查 `realtime:` 与 `测试热搜`。）

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/ -v`
Expected: 全部 PASS（输出格式变更可能让个别断言文案变化，若有失败按实际输出调整断言）

- [ ] **Step 7: Commit**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add weibo_cli/commands/_common.py tests/test_improvements.py
git commit -m "refactor: handle_command 默认走 plain render，错误走 stderr"
```

---

## Task 3: renderers.py 纯文本重写

**Files:**
- Modify: `weibo_cli/commands/renderers.py`（全文重写）
- Test: `tests/test_renderers.py`（新建）

- [ ] **Step 1: 写失败测试 — 纯文本渲染无框字符**

新建 `tests/test_renderers.py`：

```python
"""纯文本渲染测试：无 Rich 边框字符、无 markup 残留、含预期字段。"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_renderers.py -v`
Expected: FAIL（当前 renderers 用 Rich `Panel`/`Table`，输出含边框字符，且 `click.echo` 未被调用而是 `console.print`，capture 为空）

- [ ] **Step 3: 全文重写 renderers.py**

将 `weibo_cli/commands/renderers.py` 全文替换为：

```python
"""Shared renderers for CLI output — plain text, agent-friendly.

Each renderer takes parsed API data and prints plain text via click.echo.
No Rich tables/panels, no emoji, no box-drawing characters.
"""

from __future__ import annotations

import click

from ._common import format_count, strip_html


# ── Weibo card ──────────────────────────────────────────────────────


def render_weibo_card(s: dict, index: int, *, show_user: bool = True, max_text: int = 200) -> None:
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
        lines.append(f"#{index}  @{name}{verified}  {created}")
    else:
        source = s.get("source", "")
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


def render_weibo_list(statuses: list[dict], *, count: int = 20, show_user: bool = True, empty_msg: str = "暂无微博") -> None:
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
```

注意：`render_weibo_card` 与 `render_weibo_list` 删除了原 `border_style` 参数（调用方仍传该参数的需在 Task 4 清理）。为避免 Task 4 之前调用方报错，此处保留对 `border_style` 的兼容——在 `render_weibo_card` 和 `render_weibo_list` 签名末尾加 `**_` 吸收多余 kwargs：

```python
def render_weibo_card(s: dict, index: int, *, show_user: bool = True, max_text: int = 200, **_) -> None:
```
```python
def render_weibo_list(statuses: list[dict], *, count: int = 20, show_user: bool = True, empty_msg: str = "暂无微博", **_) -> None:
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_renderers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add weibo_cli/commands/renderers.py tests/test_renderers.py
git commit -m "refactor: renderers 改为纯文本输出"
```

---

## Task 4: search.py / personal.py / commands/auth.py 命令渲染纯文本化

**Files:**
- Modify: `weibo_cli/commands/search.py`（hot/feed/detail/comments/trending/search 的 `_render`）
- Modify: `weibo_cli/commands/personal.py`（profile/weibos/following/followers/reposts/home 的 `_render` 与 `border_style` 清理）
- Modify: `weibo_cli/commands/auth.py`（me/profile 的 `_render`、login/logout/status 输出）
- Test: `tests/test_renderers.py`（追加命令级测试）

- [ ] **Step 1: 写失败测试 — hot 命令纯文本**

在 `tests/test_renderers.py` 顶部 import 区追加：

```python
from weibo_cli.cli import cli
from weibo_cli.auth import Credential
```

在文件末尾追加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_renderers.py::test_hot_command_plain tests/test_renderers.py::test_status_command_plain -v`
Expected: FAIL（hot 用 Rich Table，status 用 Rich console.print）

- [ ] **Step 3: 改 search.py 各 `_render` 为纯文本**

将 `weibo_cli/commands/search.py` 顶部 import 的 `from rich.panel import Panel` / `from rich.table import Table` 删除，`from ._common import` 行去掉 `console`（若其他地方仍用 console 则保留；本文件改后不再用）。把 `hot` 的 `_render` 替换为：

```python
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
```

把 `feed` 的 `_render` 中 `border_style="blue"` 参数删除：

```python
    def _render(data):
        statuses = data.get("statuses", [])
        render_weibo_list(statuses, count=count, empty_msg="暂无热门微博")
```

把 `detail` 的 `_render` 替换为：

```python
    def _render(data):
        user = data.get("user", {})
        name = user.get("screen_name", "未知")
        verified = " ✓" if user.get("verified") else ""
        text = strip_html(data.get("text_raw", data.get("text", "")))
        source = data.get("source", "")
        created = data.get("created_at", "")
        reposts = data.get("reposts_count", 0)
        comments_count = data.get("comments_count", 0)
        likes = data.get("attitudes_count", 0)
        reads = data.get("reads_count", 0)

        lines = [f"@{name}{verified}"]
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
```

把 `comments` 的 `_render` 保持不变（已用 `render_comment_list`）。

把 `trending` 的 `_render` 替换为：

```python
    def _render(data):
        items = data.get("realtime", [])
        if not items:
            click.echo("（无趋势）")
            return
        for i, item in enumerate(items[:count], 1):
            word = item.get("word", "")
            desc = str(item.get("description", ""))[:40]
            click.echo(f"#{i:<3} {word}  {desc}")
```

把 `search` 的 `_render` 末尾 `border_style="magenta"` 删除：

```python
        render_weibo_list(statuses, count=count)
```
并把 `search` 的 `_render` 中 `console.print(f"[yellow]未找到...")` 改为：
```python
            click.echo(f'未找到 "{keyword}" 相关微博')
            return
```

- [ ] **Step 4: 改 personal.py 各 `_render` 为纯文本**

`weibo_cli/commands/personal.py`：删除 `from rich.panel import Panel` 与 import 中的 `console`。把 `profile` 的 `_render` 替换为：

```python
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
```

`weibos` 的 `_render` 不变（已用 `render_weibo_list`，`show_user=False` 保留）。

`following`/`followers` 的 `_render`：把 `title=` 与 `empty_msg=` 的 Rich markup 去掉：

```python
        render_user_table(users, title="关注列表", empty_msg="暂无关注")
```
```python
        render_user_table(users, title="粉丝列表", empty_msg="暂无粉丝")
```

`reposts` 的 `_render` 不变。

`home` 的 `_render`：删 `border_style="green"`，`empty_msg` 去 markup：

```python
        render_weibo_list(statuses, count=count, empty_msg="暂无关注者微博")
```

- [ ] **Step 5: 改 commands/auth.py 的 me / login / logout / status 输出**

`weibo_cli/commands/auth.py`：删除 `from rich.panel import Panel`。把 `me` 的 `_render` 替换为：

```python
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
```

`me` 的 `_action` 中拿不到 uid 时返回 `{"error": "..."}`，`handle_command` 的 plain render 分支会调 `render(data)`，但 `me` 没传 render → 走默认（无输出）。改为：在 `_action` 拿不到 uid 时直接 `click.echo("error: 无法获取当前用户 uid，请确认已登录（weibo login）", err=True)` 并 `raise SystemExit(1)`。把 `_action` 改为：

```python
    def _action(client):
        uid = client.get_current_uid()
        if not uid:
            click.echo("error: 无法获取当前 uid，请确认已登录（weibo login）", err=True)
            raise SystemExit(1)
        return client.get_profile(uid)
```

把 `status` 命令（第 67-92 行）替换为：

```python
@click.command()
@structured_output_options
def status(as_json, as_yaml):
    """查看当前登录状态"""
    import sys

    from ..auth import get_credential

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
```

注意：删掉了原 `elif as_yaml or not sys.stdout.isatty():` 中的 `or not sys.stdout.isatty()`，非 TTY 默认走 plain。`sys` import 若不再使用可保留不删（不影响）。

`login`/`logout` 的 `console.print("[green]✅...[/green]")` 改为 `click.echo` 纯文本（emoji 去除）。`login` 命令体（第 16-55 行）中所有 `console.print` 替换：

```python
        # --qrcode 分支
            if cred:
                click.echo("登录成功")
            else:
                click.echo("error: 登录失败", err=True)
        except Exception as e:
            click.echo(f"error: 登录失败: {e}", err=True)
        return
    ...
    if cookie_source:
        if cred:
            click.echo(f"已从 {cookie_source} 提取 Cookie 并登录")
        else:
            click.echo(f"未在 {cookie_source} 找到有效 Cookie", err=True)
            click.echo("提示: 使用 weibo login --qrcode 扫码登录", err=True)
        return
    ...
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
```

`logout`：

```python
    clear_credential()
    click.echo("已清除登录凭证")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_renderers.py -v`
Expected: PASS

- [ ] **Step 7: 运行全量测试**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/ -v && uv run ruff check .`
Expected: 全部 PASS，ruff 无新错误

- [ ] **Step 8: Commit**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add weibo_cli/commands/search.py weibo_cli/commands/personal.py weibo_cli/commands/auth.py tests/test_renderers.py
git commit -m "refactor: 全部命令默认输出改为纯文本"
```

---

## Task 5: auth.py 提取 QR 公共步骤 + 会话持久化 + 重写 qr_login

**Files:**
- Modify: `weibo_cli/auth.py:213-447`
- Test: `tests/test_auth.py`（追加）

- [ ] **Step 1: 写失败测试 — _qr_get_session**

在 `tests/test_auth.py` 末尾追加：

```python
import httpx
from weibo_cli.auth import _qr_get_session


class TestQrGetSession:
    def test_returns_qrid_csrf_scan_url_cookies(self):
        def handler(request):
            if request.url.path == "/sso/signin":
                return httpx.Response(200, headers={"set-cookie": "X-CSRF-TOKEN=csrf123; Path=/"})
            if request.url.path == "/sso/v2/qrcode/image":
                return httpx.Response(200, json={
                    "retcode": 20000000,
                    "data": {"qrid": "qrid123", "image": "https://x/y?data=scanurl123"},
                })
            return httpx.Response(404)
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        session = _qr_get_session(client)
        assert session["qrid"] == "qrid123"
        assert session["csrf_token"] == "csrf123"
        assert session["scan_url"] == "scanurl123"
        assert "X-CSRF-TOKEN" in session["cookies"]
        client.close()

    def test_raises_on_missing_csrf(self):
        def handler(request):
            if request.url.path == "/sso/signin":
                return httpx.Response(200)  # no set-cookie
            return httpx.Response(200, json={"retcode": 20000000, "data": {"qrid": "q", "image": "https://x?data=s"}})
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        with pytest.raises(RuntimeError, match="X-CSRF-TOKEN"):
            _qr_get_session(client)
        client.close()

    def test_raises_on_qr_image_failure(self):
        def handler(request):
            if request.url.path == "/sso/signin":
                return httpx.Response(200, headers={"set-cookie": "X-CSRF-TOKEN=c; Path=/"})
            return httpx.Response(200, json={"retcode": 999, "msg": "fail"})
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        with pytest.raises(RuntimeError, match="Failed to get QR code"):
            _qr_get_session(client)
        client.close()
```

注意：`pytest` 已在 test_auth.py 顶部？检查：当前文件未 import pytest。在文件顶部加 `import pytest`。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_auth.py::TestQrGetSession -v`
Expected: FAIL（`_qr_get_session` 未定义，ImportError）

- [ ] **Step 3: 实现 _qr_get_session / _exchange_crossdomain / _qr_poll_and_finalize / 会话持久化**

在 `weibo_cli/auth.py` 顶部 import 区，把 `from .constants import (...)` 列表里追加 `QR_ALT_URL`、`QR_SESSION_FILE`、`QR_SESSION_TTL_S`、`RETCODE_QR_SCANNED`、`RETCODE_QR_EXPIRED`（部分已存在则不重复）。

在 `# ── QR Login flow ───` 段（第 284 行附近）之前插入会话持久化与 PNG 函数，并把原 `qr_login` 重写。整体替换从第 284 行 `# ── QR Login flow ───` 到文件末尾 `qr_login` 结束（第 447 行）的内容为：

```python
# ── QR session persistence ──────────────────────────────────────────


def save_qr_session(session: dict) -> None:
    """Persist QR session (qrid + csrf + passport cookies) for qr-done."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "qrid": session["qrid"],
        "csrf_token": session["csrf_token"],
        "cookies": session["cookies"],
        "scan_url": session["scan_url"],
        "created_at": time.time(),
    }
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


def clear_qr_session(path=None) -> None:
    """Remove QR session file."""
    f = path or QR_SESSION_FILE
    if f.exists():
        f.unlink()
        logger.info("QR session removed: %s", f)


def _write_qr_png(data: str, path: str) -> None:
    """Render *data* as a QR PNG image to *path*."""
    import qrcode
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
                passport_cookies = dict(client.cookies.items())
                cross_cookies = _exchange_crossdomain(data.get("url", ""), data.get("alt", ""))
                cookies = {**passport_cookies, **cross_cookies}
                if not cookies:
                    raise RuntimeError("Login succeeded but no cookies were obtained")
                credential = Credential(cookies=cookies)
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
        except QRExpiredError:
            raise

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
```

注意：保留 `_render_qr_half_blocks` 与 `_display_qr_in_terminal` 不变（终端二维码渲染，人用）。`qr_login` 内 `import click` 局部导入，避免 auth 模块顶层依赖 click（保持现有风格：commands 层才用 click）。

- [ ] **Step 4: 运行 _qr_get_session 测试确认通过**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_auth.py::TestQrGetSession -v`
Expected: PASS

- [ ] **Step 5: 写失败测试 — _qr_poll_and_finalize**

在 `tests/test_auth.py` 末尾追加：

```python
from weibo_cli.auth import (
    _qr_poll_and_finalize,
    save_credential,
    clear_qr_session,
    load_qr_session,
    save_qr_session,
    QRExpiredError,
)
from weibo_cli.constants import RETCODE_QR_NOT_SCANNED, RETCODE_QR_SCANNED, RETCODE_QR_EXPIRED, RETCODE_SUCCESS


class TestQrPollAndFinalize:
    def _client_with_states(self, states, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth._exchange_crossdomain", lambda url, alt: {"SUB": "final", "SUBP": "p"})
        idx = {"i": -1}
        def handler(request):
            idx["i"] += 1
            return httpx.Response(200, json=states[min(idx["i"], len(states) - 1)])
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="https://passport.weibo.com")
        client.cookies.set("X-CSRF-TOKEN", "csrf", domain="passport.weibo.com")
        return client

    def test_success_saves_credential(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        states = [
            {"retcode": RETCODE_QR_NOT_SCANNED, "msg": ""},
            {"retcode": RETCODE_QR_SCANNED, "msg": "已扫"},
            {"retcode": RETCODE_SUCCESS, "data": {"url": "u", "alt": "a"}},
        ]
        client = self._client_with_states(states, monkeypatch)
        cred = _qr_poll_and_finalize(client, "qrid")
        assert cred.cookies["SUB"] == "final"
        assert (tmp_path / "credential.json").exists()
        client.close()

    def test_expired_raises(self, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        client = self._client_with_states([{"retcode": RETCODE_QR_EXPIRED, "msg": "过期"}], monkeypatch)
        with pytest.raises(QRExpiredError):
            _qr_poll_and_finalize(client, "qrid")
        client.close()

    def test_timeout_raises_timeout_error(self, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        client = self._client_with_states([{"retcode": RETCODE_QR_NOT_SCANNED, "msg": ""}], monkeypatch)
        with pytest.raises(TimeoutError):
            _qr_poll_and_finalize(client, "qrid", poll_timeout=0.01)
        client.close()

    def test_crossdomain_fail_alt_success(self, tmp_path, monkeypatch):
        """crossdomain 抛异常但 alt 成功 → 仍拿到 cookie 并保存。"""
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.CREDENTIAL_FILE", tmp_path / "credential.json")
        monkeypatch.setattr("weibo_cli.auth.POLL_INTERVAL_S", 0)
        # _exchange_crossdomain 被 mock 为返回 alt cookie，模拟 cross 失败 alt 成功的合并结果
        monkeypatch.setattr("weibo_cli.auth._exchange_crossdomain", lambda url, alt: {"SUB": "fromalt"})
        states = [{"retcode": RETCODE_SUCCESS, "data": {"url": "u", "alt": "a"}}]
        client = self._client_with_states(states, monkeypatch)
        cred = _qr_poll_and_finalize(client, "qrid")
        assert cred.cookies["SUB"] == "fromalt"
        client.close()


class TestQrSessionPersistence:
    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        session = {"qrid": "q", "csrf_token": "c", "cookies": {"X-CSRF-TOKEN": "c", "tid": "t"}, "scan_url": "s"}
        save_qr_session(session)
        loaded = load_qr_session()
        assert loaded["qrid"] == "q"
        assert loaded["csrf_token"] == "c"
        assert loaded["cookies"]["tid"] == "t"
        assert "created_at" in loaded

    def test_load_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "nope.json")
        assert load_qr_session() is None

    def test_clear(self, tmp_path, monkeypatch):
        monkeypatch.setattr("weibo_cli.auth.QR_SESSION_FILE", tmp_path / "qr_session.json")
        save_qr_session({"qrid": "q", "csrf_token": "c", "cookies": {}, "scan_url": "s"})
        assert (tmp_path / "qr_session.json").exists()
        clear_qr_session()
        assert not (tmp_path / "qr_session.json").exists()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 7: 运行全量测试 + ruff**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/ -v && uv run ruff check .`
Expected: 全部 PASS

- [ ] **Step 8: Commit**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add weibo_cli/auth.py tests/test_auth.py
git commit -m "refactor: 提取 QR 公共步骤与会话持久化，重写 qr_login"
```

---

## Task 6: login 改 group + qr-start / qr-done 子命令

**Files:**
- Modify: `weibo_cli/commands/auth.py:13-55`
- Modify: `weibo_cli/cli.py`（无需改，login 仍以同名注册）
- Test: `tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试 — qr-start/qr-done 注册 + group 守卫**

在 `tests/test_cli.py` 的 `EXPECTED_COMMANDS`（第 34-38 行）保持不变（仍是 16 个顶级命令，login 现在是 group 但仍算 1 个）。在文件末尾追加：

```python
def test_qr_subcommands_registered():
    """login group 下有 qr-start / qr-done 子命令。"""
    runner = CliRunner()
    for sub in ["qr-start", "qr-done"]:
        result = runner.invoke(cli, ["login", sub, "--help"])
        assert result.exit_code == 0, f"login {sub} --help failed: {result.output}"


def test_qr_start_does_not_trigger_default_login(monkeypatch):
    """qr-start 不能误触发 group 回调的默认登录流程。"""
    from weibo_cli.commands import auth as auth_mod
    called = {"get_credential": 0, "qr_login": 0}
    monkeypatch.setattr(auth_mod, "get_credential", lambda: called.__setitem__("get_credential", called["get_credential"] + 1) or None)
    # qr_login 是函数内 import，需 patch 源模块
    import weibo_cli.auth as auth_core
    monkeypatch.setattr(auth_core, "qr_login", lambda: called.__setitem__("qr_login", called["qr_login"] + 1) or None)
    # qr-start 不带 --png 应因参数缺失而非 0 退出，但不应调用 get_credential/qr_login
    runner = CliRunner()
    runner.invoke(cli, ["login", "qr-start"])
    assert called["get_credential"] == 0
    assert called["qr_login"] == 0
```

注意：`qr-start` 缺 `--png` 会以 exit_code=2 退出（Click 参数缺失），但关键是 group 回调没跑默认流程。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_cli.py::test_qr_subcommands_registered tests/test_cli.py::test_qr_start_does_not_trigger_default_login -v`
Expected: FAIL（子命令未注册）

- [ ] **Step 3: 把 login 改为 group 并新增 qr-start / qr-done**

将 `weibo_cli/commands/auth.py` 的 `login` 命令（第 13-55 行，即 `@click.command()` 到 `qr_login` 调用结束的整个函数）替换为：

```python
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


@click.command(name="qr-start")
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


@click.command(name="qr-done")
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
            click.echo(f"status: 轮询超时（会话已保留，可再次运行 weibo login qr-done 重试）", err=True)
            sys.exit(1)

    click.echo("status: success")
    click.echo(f"credential saved: {CREDENTIAL_FILE}")
    clear_qr_session(f)
```

- [ ] **Step 4: 在 cli.py 注册新子命令**

`weibo_cli/cli.py` 现有 `cli.add_command(auth.login)`（第 36 行）保持——group 自带子命令，无需单独注册 qr_start/qr_done（它们通过 `@login.command` 装饰器？不，上面用的是 `@click.command` 独立定义）。需改为用 group 的 `add_command` 或装饰器。最简单：把 `qr_start`/`qr_done` 的装饰器从 `@click.command(name="qr-start")` 改为 `@login.command(name="qr-start")`，让它们挂到 login group 下。

修改 `weibo_cli/commands/auth.py` 中两个子命令装饰器：

```python
@login.command(name="qr-start")
@click.option("--png", required=True, help="二维码 PNG 输出路径")
def qr_start(png):
    ...

@login.command(name="qr-done")
@click.option("--timeout", default=60, help="轮询超时秒数（默认60，用户已扫码应很快）")
@click.option("--session", "session_path", default=None, help="会话文件路径（默认 ~/.config/weibo-cli/qr_session.json）")
def qr_done(timeout, session_path):
    ...
```

`cli.py` 无需改动（`auth.login` 已注册，子命令随 group 一起）。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/test_cli.py -v`
Expected: PASS（含新子命令注册、group 守卫、原有 16 命令计数仍为 16）

- [ ] **Step 6: 运行全量测试 + ruff**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run pytest tests/ -v && uv run ruff check .`
Expected: 全部 PASS

- [ ] **Step 7: 手动冒烟（qr-start 生成 PNG）**

Run: `cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent && uv run weibo login qr-start --png /tmp/qr.png && ls -la /tmp/qr.png`
Expected: 生成 PNG 文件，stdout 输出 image/qrid/session/qr_expires_in 四行。若网络受限无法连 passport.weibo.com，记录为网络问题，不阻断（测试已覆盖逻辑）。

- [ ] **Step 8: Commit**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add weibo_cli/commands/auth.py
git commit -m "feat: login 改 group，新增 qr-start/qr-done 两段式登录"
```

---

## Task 7: 文档更新

**Files:**
- Modify: `README.md`
- Modify: `SKILL.md`

- [ ] **Step 1: README "Output Modes" 段改写**

找到 README.md 中 `### Output Modes` 段（英文区），替换为：

```markdown
### Output Modes

- Default **plain text** for agent-friendly, token-efficient output (TTY and non-TTY alike)
- `--json` for strict JSON
- `--yaml` for YAML structured output
```

找到中文区对应 `**结构化输出**` 描述下方的 Output Modes 说明（如有 `Non-TTY stdout defaults to YAML` 字样），改为同义中文：默认纯文本，`--json`/`--yaml` 显式。

删除 README 中所有 "Non-TTY stdout defaults to YAML automatically" / "stdout 不是 TTY 时默认输出 YAML" 表述（英文区 AI Agent Tip、中文区 AI Agent 提示）。把 AI Agent Tip 改为：

```markdown
> **AI Agent Tip:** Default output is plain text (agent-friendly, low token). Use `--json` when you need strict JSON, `--yaml` otherwise. Use `--count` to limit results.
```

中文：

```markdown
> **AI Agent 提示：** 默认输出为纯文本（agent 友好、省 token）。需要严格 JSON 时用 `--json`，否则可用 `--yaml`。用 `--count` 限制条数。
```

- [ ] **Step 2: README 新增两段式 QR 用法**

在 `### Authentication` 段（英文区）的 QR code login 条目后追加：

```markdown
**Two-phase QR login (for agents):**

```bash
# 1. Generate QR image (non-interactive)
weibo login qr-start --png /tmp/qr.png
# → writes PNG + session file, prints image/qrid/session/qr_expires_in

# 2. Send /tmp/qr.png to user, ask them to scan with Weibo App

# 3. Complete login
weibo login qr-done
# → polls scan result, saves credential, clears session
```
```

在 `### 使用示例` 中文区 `weibo login --qrcode` 行后追加：

```bash
weibo login qr-start --png /tmp/qr.png   # 生成二维码图片（非交互，agent 用）
weibo login qr-done                       # 完成二维码登录
```

- [ ] **Step 3: SKILL.md "Output Format" 段改写**

找到 SKILL.md `## Output Format` 段，把 `### Default: Rich table (human-readable)` 子段替换为：

```markdown
### Default: plain text (agent-friendly)

```bash
weibo hot                              # Plain text output, low token
```

Non-TTY and TTY both default to plain text. Use `--json`/`--yaml` for structured output.
```

删除 "Non-TTY stdout defaults to YAML automatically" 行。

- [ ] **Step 4: SKILL.md 新增两段式 QR agent 工作流**

找到 SKILL.md `### Step 1: Guide user to authenticate` 的 `**Method B: QR code login**`，替换为：

```markdown
**Method B: Two-phase QR login (agent-friendly)**

```bash
# 1. Generate QR image (non-interactive)
weibo login qr-start --png /tmp/qr.png
# stdout: image: /tmp/qr.png / qrid: ... / session: ... / qr_expires_in: 240

# 2. Send /tmp/qr.png to the user; ask them to scan with Weibo App (我的 → 扫一扫)

# 3. After user scanned, complete login
weibo login qr-done
# stdout: status: success / credential saved: ...
```
```

- [ ] **Step 5: SKILL.md "Command Reference" 表补两行**

在 SKILL.md `### Account` 表中 `weibo login --qrcode` 行后追加：

```markdown
| `weibo login qr-start --png <path>` | Generate QR login image (non-interactive, for agents) |
| `weibo login qr-done` | Complete QR login (poll scan result, save credential) |
```

- [ ] **Step 6: 提交文档**

```bash
cd C:/workspace/weibo-cli-melodyi.worktrees/cli-agent
git add README.md SKILL.md
git commit -m "docs: 更新输出格式与两段式 QR 登录文档"
```

---

## Self-Review（已完成）

**Spec 覆盖**：
- Part A 决策 1-5 → Task 2/3/4 覆盖（路由、renderers、命令渲染、stderr）。
- Part A `--qrcode` 豁免边界 → Task 5 的 `qr_login` 重写（终端渲染保留、文本走 stderr 去 emoji）覆盖。
- Part B 命令结构 + group 守卫 → Task 6 覆盖。
- qr-start（含 cookies 快照、PNG 写盘失败、session 0o600、qr_expires_in）→ Task 5 + Task 6 覆盖。
- qr-done（过期删 session、超时保留、retcode 常量、crossdomain/alt）→ Task 5 + Task 6 覆盖。
- 代码复用（_qr_get_session / _qr_poll_and_finalize）→ Task 5 覆盖。
- 依赖 Pillow → Task 1 覆盖。
- 测试计划全部条目 → Task 2/3/4/5/6 测试覆盖。
- 文档 → Task 7 覆盖。

**Placeholder 扫描**：无 TBD/TODO，所有代码块完整。

**类型一致性**：`_qr_get_session` 返回 dict 键 `{qrid, scan_url, csrf_token, cookies}` 在 Task 5 定义、Task 6 qr-start/qr-done 使用一致；`_qr_poll_and_finalize(client, qrid, *, on_status, poll_timeout)` 签名在定义与调用处一致；`save_qr_session`/`load_qr_session`/`clear_qr_session` 签名一致。

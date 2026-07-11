# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

`weibo-cli`（PyPI 包名 `kabi-weibo-cli`，入口命令 `weibo`）是一个只读的微博 CLI：热搜、Feed、搜索、微博详情/评论/转发、用户资料与关注关系。使用 Click + httpx，Python ≥ 3.10，依赖 `uv` 管理环境。**默认输出为纯文本**（agent 友好、低 token），`--json`/`--yaml` 切结构化输出。同时附带 `SKILL.md`，可作为 AI Agent skill 被安装。实际踩过的坑与修复记录在 `docs/troubleshooting.md`；网页端 ajax 接口调研（端点/出入参/与现状差异/新功能候选）记录在 `docs/api-investigation.md`。

## 常用命令

```bash
# 安装开发依赖（含 dev + yaml extras）
uv sync --extra dev --extra yaml

# Lint
uv run ruff check .

# 全量测试（默认排除 smoke 标记）
uv run pytest tests/ -v

# 运行单个测试文件 / 单个用例
uv run pytest tests/test_client.py -v
uv run pytest tests/test_client.py::test_get_hot_search -v

# Smoke 测试（需要真实浏览器 Cookie，会打真实接口）
uv run pytest tests/ -v -m smoke

# 本地运行 CLI（开发态）
uv run weibo hot --json
```

`pyproject.toml` 中 `addopts = "-m 'not smoke'"`，所以普通 `pytest` 不会跑需要联网的 smoke 用例。`ruff` line-length=140，target py310。

## 本机登录注意事项

本机（Windows 11，Chrome 150）下 **Chrome 浏览器 Cookie 提取不可用**：weibo 域 cookie 全是 v20 App-Bound Encryption，而 `rookiepy` 0.5.6（当前依赖）仍用旧 runassu 法派生 key，解不开 Chrome 133+ 的 v20 cookie，报 `decrypt_encrypted_value failed`（rookiepy 上游 issue #95；修复 #96 未发版，且只测到 Chrome 135）。管理员权限只让 rookiepy 过 VSS 影子拷贝那层，解不了算法/key 不匹配；关 Chrome 也无济于事。`browser-cookie3` 已于本仓库移除（永不支持 ABE），不要装回来。

开发与登录请直接用二维码扫码：

```bash
uv run weibo login --qrcode
```

或：在 Firefox 登录微博后 `weibo login --cookie-source firefox`（Firefox 走 NSS、不碰 ABE，可走通）。不要在测试或脚本里依赖 `weibo login` 的 Chrome 提取分支，也不要花时间调试 Chrome 提取路径——等 rookiepy 发含 #96 的新版后再重评。

## 架构

### 分层与调用链

```
cli.py (Click group, 注册 16 个命令)
  └── commands/*.py   每个命令 = action(client) + render(data)
        └── _common.handle_command  ← 所有命令的统一调度入口
              └── client.WeiBoClient (httpx, 上下文管理器)
                    ├── auth.Credential / get_credential
                    ├── constants.* (URL/headers)
                    └── exceptions.* (WeiboApiError 体系)
```

命令分三个模块：`commands/auth.py`（login/logout/status/me）、`commands/search.py`（hot/feed/detail/comments/trending/search）、`commands/personal.py`（profile/weibos/following/followers/reposts/home）。`cli.py` 用 `cli.add_command(...)` 显式注册，没有用 `@cli.command()` 装饰，新增命令要在 `cli.py` 里补一行注册。`commands/renderers.py` 放纯文本渲染函数（weibo 列表、用户、评论、转发），用 `click.echo` 输出，不用 Rich 表格。`login` 是一个 **Click group**（`invoke_without_command=True`），裸 `weibo login` 走默认登录链路，子命令 `qr-start`/`qr-done` 支持两段式 QR 登录（见「认证」）。

### `handle_command` 模式（`commands/_common.py`）

几乎所有命令都遵循同一模式：定义 `_action(client)` 返回 dict，定义 `_render(data)` 用纯文本输出，然后调 `handle_command(cred, action=..., render=..., as_json=..., as_yaml=...)`。该函数负责：

1. `with WeiboClient(cred) as client: data = action(client)`
2. 若抛 `SessionExpiredError`：自动调 `extract_browser_credential()` 刷新后重试一次（本机 Chrome 提取不可用，见「本机登录注意事项」）
3. 输出路由：默认（无论 TTY 与否）→ `render(data)` 纯文本；`--json` → JSON；`--yaml` → YAML（无 pyyaml 时回退 JSON）
4. 捕获 `WeiboApiError`，打印 `error [code]: msg` **到 stderr**

**新增命令时复用 `structured_output_options` 装饰器加 `--json/--yaml`，并在 `handle_command` 里走这套流程，不要自己处理输出与异常。** 读类命令按是否需要登录选择 `get_credential()`（公开接口也可用，如 hot/feed/trending/search）或 `require_auth()`（detail/comments/profile 等需要登录的接口，会打印未登录提示并抛 `AuthRequiredError`）。

### `WeiboClient`（`client.py`）

- **必须作为上下文管理器使用**（`with WeiboClient(cred) as client:`），否则 `self.client` 属性会抛 `RuntimeError`。测试里 `conftest.mock_client` 用 `__new__` 绕过并注入 `MagicMock`。
- **反风控**：`_rate_limit_delay` 在请求间加高斯抖动（均值 ~1s，σ=0.3），5% 概率插入 2–5s 长停顿模拟阅读；429/5xx 指数退避重试（默认 3 次）。**不要旁路这些延迟，不要并发打请求**——这是账号安全的设计。
- **响应处理**：`_handle_response(data, action, unwrap=...)` 处理微博统一的 `{ok, data, msg}` 信封。`ok==-100` 或 msg 含登录关键词 → 抛 `SessionExpiredError`；`ok==0` → `WeiboApiError`；`ok==1` → 返回 `data["data"]`（`unwrap=True`，默认）或整个 dict（`unwrap=False`）。**各 API 方法是否 unwrap 取决于该接口数据放在 `data` 里还是顶层**——例如 `get_hot_timeline`/`get_friends_timeline`/`get_weibo_detail`/`get_reposts`/`get_following` 都是 `unwrap=False`，因为 statuses 数组在顶层；改动接口时要核对此设置。
- **`get_friends_timeline` 必须带 `list_id=""`**：微博服务端 JS 会对 `list_id` 调 `.slice()`，缺失则抛异常返回 HTTP 500。这是真实踩过的坑（见 `docs/troubleshooting.md` 问题一），改该接口时不要去掉这个参数。
- **当前用户 uid**：`get_current_uid()` 请求 `/ajax/config/get_config` 并从响应头 `x-log-uid` 取 uid（`/ajax/profile/me` 是 404、`get_config` 的 `data` 无 uid 字段，都不可用）。`me` 命令依赖它。
- **两套 API 表面**：大部分接口走 `weibo.com/ajax/*`（Chrome 145 headers，`constants.HEADERS`）；关键词搜索 `search_weibo` 走 `m.weibo.cn/api/container/getIndex`（移动端 UA，`constants.MOBILE_*`），用 `_build_mobile_client()` 每次新建独立 client。搜索结果从 `cards` 里 `card_type==9` 的 card 提取 `mblog`。m.weibo.cn 在 `.weibo.cn` 域、与 `.weibo.com` 不同注册域，需它自己的 `SUB`，由 QR 登录的 cdurl 跨域交换补建：`_qr_poll_and_finalize` 成功后调 `_exchange_mobile_cookies(data.url, passport_cookies)` 单次探测 data.url（alt 是一次性令牌，不能二次 GET），从 302 Location 解析 `cdurl`（`passport.weibo.cn/sso/crossdomain?...&ticket=...`）直连（绕过会 403 的 `login.sina.com.cn`），把 `.weibo.cn` cookies 存入 `Credential.mobile_cookies`；`_build_mobile_client` 优先用 `mobile_cookies`、无则回退原 `cookies`（向后兼容老凭证）。详见 `docs/troubleshooting.md` 问题四。

### 认证（`auth.py`）

- 凭证持久化在 `~/.config/weibo-cli/credential.json`（0o600），`Credential.to_dict` 存 `saved_at`。TTL 7 天，`load_credential` 发现过期会自动调 `extract_browser_credential()` 刷新（本机 Chrome 提取不可用，见「本机登录注意事项」；会回退到沿用旧 cookie 并告警）。浏览器提取后端为 `rookiepy`（进程内调用，非子进程），`extract_browser_credential(cookie_source, *, errors_out=...)` 会把每个浏览器的失败原因写入 `errors_out` 并 `logger.warning`，`weibo login --cookie-source` 据此打印「原因 (浏览器): ...」。
- `get_credential()` 优先级：保存的凭证 → 浏览器提取。QR 登录（`qr_login`）是独立流程，不在 `get_credential` 链路里，只在 `weibo login` 时显式触发。
- **QR 登录有两条路径**：
  - **交互式（人用）**：`weibo login --qrcode` 或裸 `weibo login` 回退到 `qr_login()`，终端用 Unicode 半块字符渲染二维码（`_render_qr_half_blocks`，终端过窄回退 ASCII），阻塞轮询直到扫码成功或 4 分钟超时。
  - **两段式（agent 用，非阻塞）**：`weibo login qr-start --png <path>` 生成二维码 PNG 落盘 + 把会话存到 `~/.config/weibo-cli/qr_session.json`（`save_qr_session`，TTL `QR_SESSION_TTL_S=240s`）；agent 把图片发给用户扫码后，`weibo login qr-done [--timeout 60]` 轮询完成、保存凭证、清掉会话文件。两段共用 `_qr_get_session`/`_qr_poll_and_finalize`，跨域换 cookie 的逻辑一致。
  - 流程：`passport.weibo.com/sso/signin` 拿 `X-CSRF-TOKEN` → `/sso/v2/qrcode/image` 拿 qrid+图片URL → 轮询 `/sso/v2/qrcode/check` 每 2s 直到成功或超时 → 跟随 `data.url`/`data.alt` 跨域换取会话 cookie。

### 异常（`exceptions.py`）

`WeiboApiError` 为基类，派生 `SessionExpiredError`/`AuthRequiredError`/`ParamError`/`RateLimitError`/`QRExpiredError`。`error_code_for_exception` 把异常映射成稳定字符串码（`not_authenticated`/`rate_limited`/`invalid_params`/`qr_expired`/`api_error`/`unknown_error`），`handle_command` 用它在输出里打印 `[code]`。新增异常类型记得在 `error_code_for_exception` 里加分支。

## 测试约定

- `tests/conftest.py` 提供 `mock_client` fixture：用 `__new__` 跳过 `__init__`，注入 `MagicMock` 作为 `httpx.Client`，并把 `_request_delay=0`、`_max_retries=1`。测 `WeiboClient` 时优先用它，避免真实网络与延迟。
- `hot_search_response`/`profile_response`/`weibo_detail_response` 等 fixture 提供精简的真实 API 响应样本，便于写渲染与解析测试。
- smoke 测试（`tests/test_smoke.py`，`@pytest.mark.smoke`）需要真实登录凭证，会打真实接口，默认不跑。
- 按用户全局约束：**修改的每个 bug/问题都应配测试用例**，避免回归。

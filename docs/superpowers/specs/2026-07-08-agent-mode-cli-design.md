# weibo-cli Agent 化改造设计

- 日期：2026-07-08
- 分支：cli-agent
- 状态：已通过设计评审，待编写实现计划

## 背景与目标

当前 weibo-cli 的交互形态面向终端人类用户：

1. **默认输出**使用 Rich 表格/面板，包含边框、emoji、颜色码等 UI 字符。在 agent 调用场景下，这些字符既不增加语义，又浪费 token，且边框字符难以解析。
2. **二维码登录** (`qr_login()`) 是一个阻塞式交互流程：在终端渲染二维码、轮询 4 分钟、打印进度，期间进程不退出。agent 无法将其拆分为"生成图片"与"完成登录"两步独立调用。

本改造将 weibo-cli 转向 agent 友好的形态：

- 默认输出改为语义清晰、token 占用少的纯文本。
- 二维码登录拆分为两个非交互命令：生成图片 + 完成登录，agent 可在两步之间把图片发给用户扫码。

## 非目标

- 不改 API 调用逻辑、反风控策略、cookie 持久化格式。
- 不实现发文/点赞等写操作。
- 不做后台进程/守护进程方案（Windows 不友好）。
- 不保留非 TTY 自动 YAML 行为（见下，属破坏性变更）。

## Part A：默认输出纯文本化

### 决策

1. 默认输出（TTY 与非 TTY 一致）改为纯文本，移除 Rich 表格/面板/边框/emoji/颜色码。
2. **移除 "非 TTY 自动 YAML" 行为**：默认纯文本已对 agent 友好且省 token，不再需要该 hack。破坏性变更——脚本若依赖管道 YAML 需改用 `--yaml`。
3. emoji 全去，统计改用中文词：`评论12 转发3 赞45` 而非 `💬12 🔁3 ❤️45`。
4. 错误/警告走 stderr，stdout 只留正常输出，便于 agent 解析。Rich `Console` 保留，仅用于 stderr 着色错误。
5. `--json` / `--yaml` 显式输出保留不变。

### 格式规约

**列表型（hot / trending）**——对齐列，一行一条；空结果输出 `（无热搜）`：

```
#1  科技        热  1.2万
#2  娱乐八卦    沸  9.8万
#3  新规出台    新  5432
```

**微博列表（feed / home / search / weibos）**——每条多行块，块间空行，无框：

```
#1  @用户名✓  2026-07-08 12:34
    微博正文（截断200字）...
    评论12 转发3 赞45  ID:Qw06Kd98p
```

**微博详情（detail）**——键值 + 正文（`source` 为空时省略 `via` 段）：

```
@用户名✓  认证原因
2026-07-08 12:34  via 微博weibo.com

正文全文

阅读123 评论12 转发3 赞45  ID:Qw06Kd98p
```

**评论 / 转发列表（comments / reposts）**——每条：

```
@评论者  2026-07-08 12:34
  评论正文
  赞3
```

**用户列表（following / followers）**——对齐列：

```
UID           昵称        粉丝    简介
1699432410    张三✓       1.2万   简介截断40字
```

**个人资料（me / profile）**——键值：

```
昵称: 张三
简介: ...
粉丝: 1.2万  关注: 300  微博: 5000
位置: 北京
认证: ...
```

**状态（status）**——一行：`authenticated cookies=12` / `unauthenticated`

### 实现要点

- 重写 `weibo_cli/commands/renderers.py` 全部 `render_*` 函数为纯文本（`click.echo`）。
- 重写各命令文件中的 `_render` 为纯文本（覆盖 search.py、personal.py、commands/auth.py 中 me/profile 的 `_render`）。
- `weibo_cli/commands/_common.py` 的 `handle_command` 输出路由改为：`as_json`→JSON；`as_yaml`→YAML；else→plain render；**删掉 `not sys.stdout.isatty()` 分支**。
- **`status` 命令不经过 `handle_command`，自带 isatty 分支与 Rich 输出，需同步改造**：删 `elif as_yaml or not sys.stdout.isatty()` 分支，默认改纯文本输出 `authenticated cookies=12` / `unauthenticated`。
- `require_auth` 提示、`handle_command` 中 `WeiboApiError` 输出改走 stderr。
- **`login`/`logout` 回调内的 `console.print("[green]✅...[/green]")` 改为 `click.echo` 纯文本，emoji 去除**，成功信息走 stdout、提示走 stderr。
- `me` 命令拿不到 uid 时（`_action` 返回 `{"error": ...}`）的纯文本错误输出规约：stdout 输出 `error: 无法获取当前用户 uid，请确认已登录（weibo login）`，退出非零。
- Rich `console` 保留用于 stderr 错误着色；stdout 不再使用 `Table`/`Panel`。

### `--qrcode` 终端流程的豁免边界

保留的 `weibo login --qrcode`（人用）走 `qr_login()` 终端路径：

- **二维码终端渲染本身豁免**（`_display_qr_in_terminal`/`_render_qr_half_blocks` 保留，这是给人眼扫的，不是数据输出）。
- **文本消息对齐 Part A**：进度类（"等待扫码中..."、"已扫码，请确认"）走 stderr、去 emoji 改中文词；最终结果（"登录成功，凭证已保存"）走 stdout 纯文本。

## Part B：两段式 QR 登录

### 命令结构

`weibo login` 由 `@click.command` 改为 `@click.group(invoke_without_command=True)`：

- `weibo login` / `weibo login --cookie-source <browser>` → group 回调，浏览器/saved 凭证流程（行为同今天）
- `weibo login --qrcode` → 旧阻塞式终端扫码（人用，保留）
- `weibo login qr-start --png <path>` → 子命令 A：生成图片
- `weibo login qr-done [--timeout <s>] [--session <path>]` → 子命令 B：完成登录

**Click 语义坑（必须处理）**：`invoke_without_command=True` 下，group 回调即使提供了子命令也会**先于子命令执行**。若不守卫，`weibo login qr-start` 会先误跑默认登录流程。group 回调必须用 `ctx.invoked_subcommand is not None` 守卫：

```python
@click.group(invoke_without_command=True)
@click.option("--qrcode", is_flag=True)
@click.option("--cookie-source", type=str, default=None)
@click.pass_context
def login(ctx, qrcode, cookie_source):
    if ctx.invoked_subcommand is not None:
        return  # 交给子命令处理
    # 原有浏览器/saved/--qrcode 流程
```

group 级标志（`--qrcode`/`--cookie-source`）须出现在子命令名之前（`weibo login --cookie-source chrome qr-start` 有效，反序无效），这是 Click 解析顺序的硬约束，需在文档中说明。

### 子命令 A：qr-start

流程：

1. GET `/sso/signin` → 取 `X-CSRF-TOKEN` cookie 及**全部** passport 域 cookie。
2. GET `/sso/v2/qrcode/image`（带 `x-csrf-token` header）→ 取 `qrid` + `image` URL + `scan_url`。**cookies 须在本步完成后从 client.cookies 采集**（image 端点可能 set 额外 cookie），而非 step 1 之后。
3. 用 `qrcode` + Pillow 生成 PNG，写到 `--png` 指定路径。**PNG 写盘失败**（路径不可写/父目录不存在）→ stderr 报错 + 非零退出 + **不写** session 文件。
4. 持久化会话状态到 `CONFIG_DIR/qr_session.json`，文件权限 `0o600`（与 credential.json 一致；Windows 上 chmod 为 no-op，功能不受影响）：
   ```json
   {"qrid": "...", "csrf_token": "...", "cookies": {"...": "..."}, "scan_url": "...", "created_at": 1234567890.0}
   ```
5. stdout（纯文本，便于 agent 解析）：
   ```
   image: /tmp/qr.png
   qrid: abc123
   session: ~/.config/weibo-cli/qr_session.json
   qr_expires_in: 240
   ```
   （用 `qr_expires_in` 而非 `expires_in`，避免与 qr-done 的轮询超时混淆——前者是二维码本身有效期，后者是轮询等待时长。）
6. 退出码 0。

错误处理：CSRF 获取失败 / qrid 获取失败 / PNG 写盘失败 → stderr 报错 + 非零退出码，**不写** session 文件。

### 子命令 B：qr-done

流程：

1. 读 `CONFIG_DIR/qr_session.json`（`--session <path>` 可覆盖）。不存在 → stderr 报错，退出非零。
2. 检查 `created_at`，超过 240s → 输出 "qr session expired"，**删** session 文件，退出非零。
3. 重建 httpx client，载入持久化 cookies + `x-csrf-token` header。
4. 轮询 `/sso/v2/qrcode/check`，每 2s，直到成功或 `--timeout`（默认 60s，用户已扫码应很快；可调大）。
5. 成功 → follow crossdomain URL + alt 换 cookie → `save_credential` → **删** session 文件。
6. 进度输出走 stderr；stdout 只给最终结果：
   ```
   status: success
   credential saved: ~/.config/weibo-cli/credential.json
   ```
7. 超时 → 非零、**保留** session 文件以便重试；过期 → 非零、**删** session 文件。

### 会话状态持久化原理

QR 轮询依赖第一步获取的 `X-CSRF-TOKEN` cookie 与 passport 会话 cookie。httpx 的 cookie jar 是 name→value 的可序列化结构，服务端按 cookie 识别会话（无状态，不需要同一 TCP 连接）。因此：

- qr-start 把 client cookie jar 全量（不只 CSRF）+ qrid + csrf 写入 session 文件。
- qr-done 重建 client 并载入这些 cookie + header，服务端视为同一会话。

### 代码复用与去重

提取两个公共步骤函数（在 `weibo_cli/auth.py`）：

- `_qr_get_session(client)` → 执行步骤 1-2，cookies 在 step 2 完成后采集，返回 `{qrid, scan_url, csrf_token, cookies}`。
- `_qr_poll_and_finalize(client, qrid)` → 执行步骤 4-5（轮询 + 跨域换 cookie + 保存），返回 `Credential`。**crossdomain URL 与 alt token 换 cookie 的完整逻辑（含两个独立子 client、两个 try/except、硬编码 `https://login.sina.com.cn/sso/login.php?entry=miniblog&alt={alt}&returntype=TEXT`）一并迁入此函数**；建议把 alt URL 提取到 `constants.py`。

旧 `qr_login()` 改为调用两者；qr-start 用前者；qr-done 用后者。避免三处重复实现。

**轮询状态判断改用 retcode 常量**：现有 `qr_login` 用 `"已扫" in msg` 字符串匹配判断状态（脆弱）。`constants.py` 已定义 `RETCODE_QR_SCANNED=50114002`、`RETCODE_QR_EXPIRED=50114004`。`_qr_poll_and_finalize` 重写时直接用 retcode 分支（`SUCCESS`→完成、`NOT_SCANNED`→继续、`SCANNED`→继续并提示确认、`EXPIRED`→抛 `QRExpiredError`），msg 仅作日志。

### 依赖

`pyproject.toml`：`qrcode>=7.0` → `qrcode[pil]>=7.0`（引入 Pillow 用于 PNG 生成）。

## 测试计划

CLAUDE.md 要求每个修改点配测试用例：

- **qr-start**：mock httpx → 断言 PNG 生成、session 文件含 qrid+csrf+cookies、stdout 可解析、失败路径（CSRF/qrid 失败、PNG 写盘失败）不写 session 文件。
- **qr-done**：mock session 文件 + httpx 轮询 → 成功路径保存凭证并删 session 文件；过期路径报错并删；超时路径报错并保留 session 文件。
- **qr-done 轮询中间态**：retcode=`RETCODE_QR_SCANNED`(50114002) → 继续轮询不退出；retcode=`RETCODE_QR_EXPIRED`(50114004) → 抛 `QRExpiredError`。
- **crossdomain/alt 换 cookie 路径**：crossdomain 抛异常、alt 返回有效 cookie → 最终 `save_credential` 成功；两者皆失败 → 抛 `RuntimeError("no cookies")`。
- **会话序列化 round-trip**：cookies 全量保留，csrf_token 与 scan_url 完整。
- **纯文本渲染**：每个 renderer 快照断言输出不含 `│┌┐└┘─` 等边框字符、不含 Rich markup 残留（`[bold]`/`[/bold]` 等）、含预期字段。
- **handle_command 路由**：非 TTY 不再自动 YAML（默认走 plain render）。
- **status 命令**：默认输出纯文本 `authenticated cookies=12` / `unauthenticated`，无 isatty 分支。
- **login group 守卫**：`weibo login qr-start --png x` 不触发 group 回调的默认登录流程（mock 验证 `get_credential`/`qr_login` 未被调用）。
- **现有 CLI 测试不破**：`test_cli.py` 中 `len(cli.commands)==16` 与 `login --help` 仍通过；新增 qr-start/qr-done 命令注册测试。

## 文档更新

- `README.md`：
  - "Output Modes" 段（默认 Rich table、Non-TTY stdout defaults to YAML）改为：默认纯文本、`--json`/`--yaml` 显式、移除非 TTY 自动 YAML。
  - "作为 AI Agent Skill 使用" 段新增两段式 QR 工作流示例（qr-start → 发图 → qr-done）。
  - 使用示例段补充 `weibo login qr-start` / `qr-done`。
- `SKILL.md`：
  - "Output Format" 段（Default: Rich table、Non-TTY stdout defaults to YAML automatically）同步改写。
  - "Authentication" 段 Step 1 补两段式 QR agent 工作流。
  - "Command Reference" 表补 qr-start/qr-done 两行。

## 破坏性变更汇总

1. 移除非 TTY 自动 YAML 输出（默认改纯文本）。
2. 默认输出不再含 Rich 表格/面板/emoji/颜色码。
3. `weibo login` 由扁平命令改为命令组（`--qrcode`/`--cookie-source` 保留，新增 `qr-start`/`qr-done` 子命令）。
4. 新增 Pillow 依赖。

## 跨平台与并发说明

- **配置目录沿用现有 `CONFIG_DIR = ~/.config/weibo-cli`**（`constants.py`）。Windows 上为 `C:\Users\<user>\.config\weibo-cli`，非 Windows 惯例（`%APPDATA%`）但功能正常，本次不改动，仅记录。
- **session 文件为单写者假设**：qr-start 与 qr-done 不应并发执行同一会话；不加文件锁，并发写导致 JSON 损坏视为误用。

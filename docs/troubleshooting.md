# 问题修复记录

本文件记录 weibo-cli 在实际使用中遇到的问题、根因分析与修复方式，供后续维护参考。
每条修复均配有单元测试，避免回归。

---

## 2026-07-08：`weibo home` 与 `weibo me` 不可用

### 背景

QR 登录后 `weibo status` 显示 `✅ 已登录 (6 cookies)`，但：

- `weibo home` 反复报 `HTTP 500`，3 次重试后 `❌ Request failed after 3 retries`
- `weibo me` 显示 `无法获取个人资料`

### 问题一：`weibo home` 返回 HTTP 500

**现象**：`GET /ajax/feed/friendstimeline?count=20&max_id=0` 持续返回 500。

**根因**：`WeiboClient.get_friends_timeline` 只发送了 `count` 与 `max_id` 两个参数，
**缺少 `list_id`**。微博服务端在处理该接口时会对接收到的 `list_id` 调用 JavaScript 的
`.slice()`，当 `list_id` 为 `undefined` 时直接抛异常，导致网关返回 HTTP 500。

**证据**（用一次性探针脚本直接打接口拿响应体）：

```
status: 500
body:   {"ok":0,"message":"Cannot read properties of undefined (reading 'slice')"}
```

补上 `list_id=""` 后：

```
status: 200
body:   {"ok":1,"statuses":[{...真实微博...}]}
```

进一步 bisect 发现：`refresh` / `display_mode` / `fast_refresh` / `fid` 均不影响结果，
**只有 `list_id` 是必需的**。因此修复只补 `list_id`，不引入未经证实的参数。

**修复**：`weibo_cli/client.py` `get_friends_timeline` 增加 `list_id=""`。

**测试**：`tests/test_client.py::TestFriendsTimelineAPI::test_get_friends_timeline_includes_list_id`。

---

### 问题二：`weibo me` 无法获取个人资料

**现象**：`weibo me` 输出 `无法获取个人资料`。

**根因**：`me` 命令的 `_action` 有两条获取当前用户的路径，**两条都是坏的**：

1. `GET /ajax/profile/me` → **404**（该端点不存在，微博返回 `你访问的地址不存在`）。
   代码用宽 `except Exception: pass` 吞掉了异常，所以从日志看不到 404。
2. 回退到 `get_config()`（`/ajax/config/get_config`）试图从返回里取 uid，
   但该接口的 `data` 只含 `ab_test`，**没有 uid 字段** → 取不到 uid → 放弃。

而真正能拿到当前用户的是 `/ajax/profile/info?uid=<自己>`，但它需要先知道 uid。

**uid 的可靠来源**：微博在已认证的 ajax 响应头里设置 `x-log-uid`，值即当前登录 uid。
经探针验证，`get_config` 与 `friendstimeline` 两个接口的响应头都稳定返回该值
（例：`x-log-uid: 5555027006`）。

> 备注：`m.weibo.cn/api/config` 返回 `login:false`（移动端不认 weibo.com 的 cookie），
> 不能用作 uid 来源；首页 HTML 的 `$CONFIG['uid']` 在新版微博已不存在。

**修复**：

- `weibo_cli/client.py` 新增 `get_current_uid()`：请求 `get_config` 并从 `x-log-uid`
  响应头取 uid（含限速、cookie 合并、日志，与 `_request` 一致）。
- `weibo_cli/commands/auth.py` 的 `me`：改为 `get_current_uid()` → `get_profile(uid)`，
  删除 404 的 `/ajax/profile/me` 调用和失效的 `get_config` uid 提取逻辑。

**测试**：`tests/test_client.py::TestCurrentUid`
（`test_get_current_uid_from_x_log_uid_header`、`test_get_current_uid_none_when_header_missing`）。

---

### 问题三：故障原因不可见（日志不足）

**现象**：`weibo home` 报 500 时，`-v` 日志只显示
`HTTP 500, retrying in 1.3s (1/3)`，看不到服务端返回的错误体；最终失败信息也只有
`Request failed after 3 retries`，丢失了诊断线索。`weibo me` 还用宽 `except Exception`
吞掉真实异常。

**根因**：`WeiboClient._request`

- 请求日志行只含 `METHOD url → status`，不含请求参数；
- 重试 WARNING 不含响应体；
- 重试耗尽后的 `WeiboApiError` 不带最后的响应状态/体。

**修复**：`weibo_cli/client.py` `_request`

- INFO 请求行附带 `params=...`；
- 错误状态（429/5xx）的 WARNING 附带 `body=...`（取响应体前 500 字符）；
- 重试耗尽时，`WeiboApiError` 消息带上 `HTTP {status} body={...}`。

修复后，同类 500 错误会直接显示服务端原因，例如：

```
WARNING weibo_cli.client HTTP 500, retrying in 1.2s (1/3) body={"ok":0,"message":"Cannot read properties of undefined (reading 'slice')"}
```

**测试**：`tests/test_client.py::TestRetryBehavior::test_500_logs_response_body`、
`test_500_failure_message_includes_body`。

---

### 问题四：`weibo search` 暂不支持（待修复）

**结论**：`weibo search <关键词>` 当前不可用，返回的是登录重定向而非搜索结果。
本节如实记录原因（区分"实测"与"未验证"），避免误导后续维护。

**现象**：

```
$ weibo search "科技"
{ "ok": -100, "url": "https://passport.weibo.com/sso/signin?...&url=https%3A%2F%2Fm.weibo.cn%2F" }
```

**根因（实测）**：

`WeiboClient.search_weibo` 走移动端 `m.weibo.cn/api/container/getIndex`
（`constants.MOBILE_SEARCH_URL`）。但 QR 登录（passport.weibo.com → weibo.com）
**只建立了 weibo.com 域的会话，没有建立 m.weibo.cn 域的会话**——`SUB` cookie
是 `.weibo.com` 域，不覆盖 `m.weibo.cn`，m.weibo.cn 需要它自己的 SUB。

实测（一次性探针，带保存的 6 个 cookie）：

| 请求 | 结果 |
|---|---|
| `m.weibo.cn/api/container/getIndex` 匿名（不带 cookie） | HTTP 432（需登录） |
| `m.weibo.cn/api/container/getIndex` 带 weibo.com cookie | HTTP 200，但 `{"ok":-100,"url":"...登录..."}` |

尝试过的桥接（**未成功**）：用 passport 的
`/sso/login.php?entry=mweibo&...` + 访问 `m.weibo.cn/` 首页，再调 search，
仍返回登录页 HTML。简单的 SSO 桥接不可靠，未采用。

**关于 weibo.com PC 端 JSON 搜索接口（未验证）**：

> 注意：之前曾把 `weibo.com/ajax/search/all`、`weibo.com/api/container/getIndex`
> 返回 404 当作"PC 端无可用 ajax"的证据——这两个路径是**凭命名规律臆造的**，
> 404 只能说明这两个具体路径不存在，**不能**证明 weibo.com PC 端没有可用的
> 搜索 JSON 接口。weibo.com PC 是否存在可用 ajax 搜索接口，**尚未验证**。

**已验证可行的备选路径（未实现）**：

`s.weibo.com/weibo?q=<关键词>`（PC HTML 搜索页），用现有 weibo.com 会话 cookie
（`SUB` 覆盖 `s.weibo.com`）实测可用——带 cookie 返回 200、20 条结果
（`mid`、`nick-name`、正文 `p.txt` 均可正则解析），无登录重定向。

修复方向（待做，二选一或结合）：

1. **改用 `s.weibo.com` HTML 解析**：用现有会话即可，无需改登录流程。代价是
   HTML 解析较 JSON 脆，需把移动端 card 解析换成 PC HTML 解析并适配
   `render_weibo_list`。
2. **在 QR 登录流程中补建 m.weibo.cn 会话**：保留移动端 JSON API，但 SSO
   桥接脆弱，探针未跑通，实现复杂。

在修复落地前，`weibo search` 视为不支持。

---

### 验证方式

```bash
# 单测
uv run pytest tests/                  # 130 passed（1 个预先存在的 Windows 权限测试失败，与本修复无关）

# 端到端
weibo -v home --count 3               # 返回真实关注 Feed
weibo -v me                           # 返回当前用户资料
```

### 调试方法备忘

排查“接口报错但看不到原因”时，绕开 CLI 直接用 httpx 打接口拿响应体是最快的定位手段：

```python
import httpx
from weibo_cli.constants import BASE_URL, HEADERS, FRIENDS_TIMELINE_URL
# 从 ~/.config/weibo-cli/credential.json 读 cookies
with httpx.Client(base_url=BASE_URL, headers=dict(HEADERS), cookies=cookies,
                  follow_redirects=True, timeout=30) as c:
    r = c.get(FRIENDS_TIMELINE_URL, params={"count": "20", "max_id": "0"})
    print(r.status_code, r.text[:500])     # 看 5xx 的真实 body
    print(r.headers.get("x-log-uid"))      # 当前登录 uid
```

---

## 已知遗留（非本次修复范围）

- **`weibo search` 暂不支持**：移动端 `m.weibo.cn` 会话未由 QR 登录建立，详见上文
  "问题四"。待选定修复方向（s.weibo.cn HTML 解析 / 补建 m.weibo.cn 会话）后实现。
- `tests/test_auth.py::test_file_permissions` 在 Windows 上失败：断言 `chmod 0o600` 生效，
  但 Windows 不支持 Unix 权限位（实际 `0o666`）。与本次修复无关，建议后续单独处理
 （例如对该测试加 `pytest.skip(on_windows)` 或改用跨平台断言）。

# 外部微博项目调研报告

调研 4 个与微博数据获取（API / 爬虫）相关的开源项目，了解其数据获取方式、覆盖范围、权限模型与登录链路，评估对我们 `weibo-cli` 的补充与参考价值。重点是寻找**修复我们已知两个痛点**的现成方案：

- **痛点 (a) 搜索功能坏**：`weibo search` 走 `m.weibo.cn/api/container/getIndex`，但 QR 登录只建 `weibo.com` 会话、不建 `m.weibo.cn` 会话，命中登录重定向（见 `docs/troubleshooting.md` 问题四）。
- **痛点 (b) 浏览器登录不可用**：Chrome 150 v20 ABE cookie，`rookiepy` 0.5.6 解不开（见 `docs/troubleshooting.md` 与本机登录注意事项）。

调研对象（克隆于 `C:/workspace/weibo-research/`）：

| 仓库 | 路径 | 语言/框架 |
|---|---|---|
| lumiorchid-alt/weibo-scrape | `weibo-scrape/` | Python + Playwright |
| tamnd/weibo-cli | `weibo-cli-tamnd/` | Go（自研 kit 框架） |
| dataabc/weibo-search | `weibo-search/` | Python（Scrapy） |
| nghuyong/WeiboSpider | `WeiboSpider/` | Python（Scrapy） |

---

## 一、总览对比

| 维度 | weibo-scrape | weibo-cli (tamnd) | weibo-search (dataabc) | WeiboSpider (nghuyong) |
|---|---|---|---|---|
| 主数据通道 | `m.weibo.cn` API | `weibo.com` + `m.weibo.cn` 双路 | `s.weibo.com` **HTML 解析** | `weibo.com/ajax/*` 为主；**搜索走 `s.weibo.com` HTML** |
| 怎么做搜索 | `m.weibo.cn/api/container/getIndex`（与我们同 endpoint） | **没做**正文搜索，仅 `suggest` 联想词 | `s.weibo.com/weibo?q=` HTML 解析 | `s.weibo.com/weibo?q=` HTML → `ajax/statuses/show` 二次查详情 |
| cookie 获取 | **Playwright 匿名访客 cookie**（m.weibo.cn 域） | 手动粘贴 `SUB`/`SUBP` | 手动粘贴 | 手动粘贴 `cookie.txt` |
| 是否需要登录 | 全程访客态，不需登录 | 仅 `user`/`posts` 需 cookie | 强制需 cookie | 全局带 cookie，假定已登录 |
| 覆盖范围 | 用户/微博/评论/文章/搜索/搜用户 | 热搜/单条/评论/长文/用户资料/用户微博/联想词 | **仅搜索**（字段最全） | 用户/粉丝/关注/评论/转发/按ID/按用户/搜索 |

---

## 二、各项目关键发现

### 1. weibo-scrape —— Playwright 抓 m.weibo.cn 访客 cookie

- 全程匿名，**不碰用户 Chrome**，绕开所有 ABE 问题。
- 搜索走 `m.weibo.cn/api/container/getIndex`（**与我们 endpoint 完全一致**），区别只在它给 m.weibo.cn 域配了访客 cookie。
- 关键代码：`weibo_scrape/_vendor/cookie_fetcher.py`（launch chromium → goto m.weibo.cn → sleep+scroll → `context.cookies()`，91 行，MIT，源自 crawl4weibo 0.5.2）。
- 搜索 filter 映射表（comprehensive=1 / time=61 / hot=60 / video=64 / pic=63 / original=40 / media=2）在 `weibo_api.py:260-268`。
- 已知限制：访客态评论上限 ~16-20 条、长文截断（`is_long_text` 标出）。
- cookie 缓存设计（`cookies.py`）：flock 文件锁 + TTL 2h + 撞 432 force_refresh + client 整体重试，比我们当前凭证管理更健壮。

### 2. weibo-cli (tamnd, Go) —— 一个值得验证的反例

- **没做关键词正文搜索**；`suggest` 走 `weibo.com/ajax/side/search`（桌面端，**匿名可用**），只返回联想词/相关热搜。
- 它的 `user`/`posts` 也打 `m.weibo.cn/api/container/getIndex`，但**只需 weibo.com 的 `SUB` cookie 就能过 `ok:-100`**，没有额外"建 m.weibo.cn 会话"步骤——只加了 `MWeibo-Pwa:1` + `X-Requested-With:XMLHttpRequest` + 移动 UA + Referer。
- ⚠️ **这与我们 CLAUDE.md 里"QR 登录不建 m.weibo.cn 会话"的根因判断冲突**：tamnd 的证据表明 weibo.com 的 `SUB` 对 m.weibo.cn 是通用的。
- 作者在 `docs/.../troubleshooting.md:28-31` 把 `genvisitor2` 访客 token 流程明确判死（"cannot be replicated in pure HTTP without a browser"），佐证"不依赖浏览器自动化建不了匿名 m.weibo.cn 会话"，反过来支持我们走 QR 扫码而非访客 token。
- HTML 清洗（`weibo/wire.go:21-33`）先 `<br/>` 换空格再剥标签再合并空白，比手写正则稳健，可对照。
- 时间标准化（`weibo/wire.go:35-43`）：`Mon Jan 02 15:04:05 -0700 2006` → UTC `2006-01-02 15:04:05`，失败回退原串。

### 3. weibo-search (dataabc) —— s.weibo.com HTML 解析的完整蓝本

- 搜索完全走 `s.weibo.com/weibo?q=` PC 端 HTML，不调任何搜索 JSON API。
- **`s.weibo.com` 与 `weibo.com` 同根域 `.weibo.com`**，QR 登录的 `.weibo.com` cookie 直接覆盖，无需建移动端会话。
- 字段最全（含 IP 属地、VIP 等级、认证类型，这些 m.weibo.cn API 反而不一定有）。
- 分页靠 `//a[@class="next"]/@href`；按天/小时/地区递归切片以绕过单次约 50 条上限（`FURTHER_THRESHOLD=46`）。
- 关键代码：`weibo/spiders/search.py:487-689`（XPath 字段映射）、`settings.py:33-38`（cookie/headers）、`util.py`（类型/时间转换）。
- 风控：默认 `DOWNLOAD_DELAY=10`（每页 10s），比我们激进；s.weibo.com 偶有验证码中间页未处理。

### 4. WeiboSpider —— 同样走 s.weibo.com HTML，且补充 ajax 细节

- 搜索实现与 weibo-search 同路：`s.weibo.com/weibo?q=...&timescope=...` HTML → 正则提 mblogid → `weibo.com/ajax/statuses/show?id={mblogid}` 拿结构化详情。
- **还禁用了重定向中间件**（`settings.py:23`），证明带有效 weibo.com cookie 访问 s.weibo.com 不会触发登录跳转——直接印证域名结论。
- **额外可借鉴 endpoint**：
  - `weibo.com/ajax/statuses/searchProfile?uid=...&haspic=1&hasvideo=1&hasmusic=1&hasret=1`（按用户抓微博，带类型/时间过滤，可增强我们 `weibos` 命令）
  - `weibo.com/ajax/statuses/longtext?id={mblogid}`（长微博全文——WeiboSpider 走此路；我们 2026-07-11 已用 `get_weibo_detail` 加 `isGetLongText=true` 等价覆盖，未单独引入该端点）
  - `weibo.com/ajax/profile/detail?uid={uid}`（生日/教育/公司/IP属地/阳光信用，比 `profile/info` 更全）
  - `weibo.com/ajax/statuses/buildComments?...&id={mid}` 与 `repostTimeline?id={mid}` 收 **mid（数字）** 而非 mblogid
- 工具函数 `common.py:45 url_to_mid`：mblogid（base62 短串）→ mid（数字）解码，评论/转发接口要 mid 时可直接抄。
- 登录/cookie 方面毫无建树，纯手动粘贴 `cookie.txt`（含 `SUB`/`SUBP`/`SCF`/`SSOLoginState`）。

---

## 三、对我们两个痛点的结论

### 痛点 (a)：搜索功能坏

> **2026-07-11 实测更新**：以下「tamnd 假设」已证伪，详见本仓库 `scripts/verify_mweibo_search.py` / `verify_search_warmup.py` 的实测。结论：m.weibo.cn 的 search 容器（`100103type=1`）确实需要 m.weibo.cn 自己颁发的登录会话，weibo.com 的 `SUB` 不通用，CLAUDE.md 原根因判断正确。原计划 B（s.weibo.cn HTML 解析）也已过时——s.weibo.com 现在是 SPA（`weibo-pro-next`），初始 HTML 无 `card-wrap` 卡片。新 SPA 的搜索 XHR 是 `/ajax/search/all`（见 bundle），但 `weibo.com/ajax/search/all` 返回 404、`s.weibo.com/ajax/search/all` 302→sorry，且搜索 URL 带反爬 `t` token（`getTCode`/`getSearchTScene`，混淆 JS 算出），不带就 404。
>
> **✅ 已端到端确认的修复路径（`scripts/trace_qr_crossdomain.py` + `verify_search_e2e.py`，决定性）**：QR 成功后 `data.url`（`passport.weibo.com/sso/v2/login?...&alt=...`）302 时设 `.weibo.com` SUB，并重定向到 `login.sina.com.cn/sso/v2/crossdomain?...&cdurl=<passport.weibo.cn/sso/crossdomain?...&ticket=...>`。现有 `_exchange_crossdomain` 用 UA-only client 跟随，`login.sina.com.cn` 这步 **403** → 链断 → `.weibo.cn` SUB 从未拿到。**修复**：对 `data.url` 先 `follow_redirects=False` 拿 302 Location，URL-decode 出 `cdurl` 参数，直接 GET `passport.weibo.cn/sso/crossdomain?...&ticket=...&savestate=30`（绕过会 403 的 login.sina.com.cn）→ 拿到 `Domain=.weibo.cn` 的 `SUB/SUBP/SCF/ALF/SUHB/SSOLoginState`。实测：用这套 `.weibo.cn` cookie 打 m.weibo.cn search，`ok=1`、9 条 mblogs（首条 `LinglingKwong_TH 微博開通兩周年`）。**存储注意**：Credential 是扁平 dict，`.weibo.com` 与 `.weibo.cn` 同名 `SUB` 值不同，需加 `mobile_cookies` 字段分域存，`_build_mobile_client` 用之。

**共识方案（2/4 项目采用）：改走 `s.weibo.com` HTML 解析。**
- weibo-search 和 WeiboSpider 都用这条路，且 `s.weibo.com` 与 QR 登录的 `.weibo.com` 同根域，**我们现有 QR cookie 直接覆盖，无需改登录链路、无需建 m.weibo.cn 会话**。
- 实现可直接抄 `weibo-search/weibo/spiders/search.py:487-689` 的 XPath（或 WeiboSpider 的正则 + `ajax/statuses/show` 二次查详情，结构化更干净）。
- 代价：依赖 HTML DOM，微博改版会断解析。

**备选方案 A（weibo-scrape）：补一个 m.weibo.cn 访客 cookie fetcher。**
- 把 `weibo_scrape/_vendor/cookie_fetcher.py`（91 行 Playwright）移植成 weibo-cli 的一条 cookie 来源，搜索时优先用 m.weibo.cn 域访客 cookie。
- 优点：endpoint 不变、完全匿名、绕开 ABE；缺点：访客态有评论/长文限制。

**方案 B（tamnd 启示，成本最低，先做）：验证根因是否真的是"缺 m.weibo.cn 会话"。**
- tamnd 的 `user`/`posts` 同样打 `m.weibo.cn/api/container/getIndex`，只用 weibo.com 的 `SUB` 就过了。说明 **weibo.com 的 `SUB` 对 m.weibo.cn 通用**。
- 我们 `constants.MOBILE_HEADERS` 已有 `X-Requested-With`、Referer、移动 UA，但**缺 `MWeibo-Pwa: 1`**——这正是 tamnd 多出的那个 header。
- 若验证通过（补 `MWeibo-Pwa: 1` 后 m.weibo.cn getIndex 不再重定向），最小改动即可修搜索，不必引入 HTML 解析的脆弱性。
- 验证脚本见本仓库 `scripts/`（或临时脚本），实测三个对照：当前 header、补 `MWeibo-Pwa`、完全对照 tamnd。

### 痛点 (b)：浏览器登录不可用（Chrome v20 ABE）

- **4 个项目没有一个解决这个问题**：weibo-scrape 完全不碰用户 Chrome（新开 Playwright 匿名浏览器，语义是访客态，不能替代登录态）；其余三个都是手动粘贴 cookie。
- weibo-scrape 的访客 cookie fetcher 对**只读公开接口**（hot/feed/trending/search/detail/comments/profile/weibos）可作为 rookiepy 不可用时的**回退凭证**，但 `me`/`following`/`followers` 这类需要登录态的命令它替代不了——这部分我们 QR 扫码 / Firefox NSS 的现有路径仍是最优解。
- 兜底参考：WeiboSpider / tamnd 的「手动粘贴 cookie」可作我们最后退路（`weibo login --cookie 'SUB=...; SUBP=...'`）。

---

## 四、对我们能力的净补充清单（按优先级）

1. **🔴 验证 tamnd 假设（先做、成本最低）**：补 `MWeibo-Pwa: 1` header 打 m.weibo.cn getIndex，确认搜索 bug 根因到底是"域"还是"header"。若通则最小修复。
2. **🟠 搜索改走 s.weibo.com HTML**（若验证 1 不通）：抄 weibo-search 的 XPath 或 WeiboSpider 的 HTML + ajax 二次查详情。这是最稳的修法，且复用现有 QR 会话。
3. **🟡 补 `weibo suggest` 命令**：走 `weibo.com/ajax/side/search`（匿名可用），给 agent 一个"关键词→联想词/相关热搜"的降级能力。tamnd 有现成实现（`weibo/api.go:162-184`、`weibo/wire.go:89-95`）。净增量。
4. **✅ 补长微博全文**（2026-07-11 已做）：`get_weibo_detail` 加 `isGetLongText=true`，实测与 `ajax/statuses/longtext` 的 `longTextContent` 一致，故未引入该端点。见 `docs/troubleshooting.md` 2026-07-11 条。
5. **🟡 增强按用户抓微博**：用 `weibo.com/ajax/statuses/searchProfile`（带 `haspic/hasvideo/hasmusic/hasret` 类型过滤 + `starttime/endtime` 时间窗）替换/补充我们 `weibos` 命令。
6. **🟢 工具函数 `url_to_mid`**（base62→mid）：WeiboSpider `common.py:45`，评论/转发接口收 mid 时可直接抄。
7. **🟢 访客 cookie fetcher 作为只读命令的回退凭证**：weibo-scrape 的 Playwright 实现 + cookie 缓存设计（flock + TTL 2h + 撞 432 force_refresh）。
8. **🟢 手动粘贴 cookie 作为登录兜底**：QR / Firefox 都不可用时的最后退路。

---

## 五、我们独有的、它们都补不上的能力

热搜、feed/好友时间线、转发树、关注/粉丝关系列表、登录态路径（QR + Firefox NSS）——4 个项目里没有一个同时覆盖这些。我们的覆盖面在这组对比里是最宽的，无需向它们看齐。

---

## 关键文件索引（绝对路径，克隆于 `C:/workspace/weibo-research/`）

**weibo-scrape**
- `weibo-scrape/weibo_scrape/weibo_api.py`（端点 + 解析，369 行）
- `weibo-scrape/weibo_scrape/_vendor/cookie_fetcher.py`（访客 cookie 抓取，105 行，MIT）
- `weibo-scrape/weibo_scrape/cookies.py`（cookie 缓存 + flock + TTL，91 行）
- `weibo-scrape/weibo_scrape/client.py`（信封 + 432 重试，163 行）

**weibo-cli (tamnd)**
- `weibo-cli-tamnd/weibo/api.go`（6 个 API 方法，endpoint 在此）
- `weibo-cli-tamnd/weibo/weibo.go`（Client、双 host header 注入 `:124-130`、重试退避）
- `weibo-cli-tamnd/weibo/types.go` + `wire.go`（数据模型 + 转换 + HTML 清洗 + 时间标准化）
- `weibo-cli-tamnd/docs/content/reference/troubleshooting.md`（`genvisitor2` 不可行说明 `:28-31`）

**weibo-search**
- `weibo-search/weibo/spiders/search.py`（搜索 spider 全部逻辑，XPath `:487-689`）
- `weibo-search/weibo/items.py`（字段定义）
- `weibo-search/weibo/settings.py`（cookie/headers/配置）
- `weibo-search/weibo/utils/util.py`（类型转换、时间标准化）

**WeiboSpider**
- `WeiboSpider/weibospider/spiders/tweet_by_keyword.py`（搜索实现 `:36-64`）
- `WeiboSpider/weibospider/spiders/common.py`（`url_to_mid` `:45`、`parse_tweet_info` `:86`、longtext `:136`）
- `WeiboSpider/weibospider/spiders/tweet_by_user_id.py`（`searchProfile` `:34`）
- `WeiboSpider/weibospider/settings.py`（`cookie.txt` 全局 `:10-15`、禁用重定向 `:23`）

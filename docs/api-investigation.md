# 微博网页端 ajax 接口调研

调研日期：2026-07-11
调研方式：有头浏览器（playwright-cli）登录 `weibo.com` 后，遍历各页面抓取 Fetch/XHR 请求，并对关键端点用 `fetch()` 复测其响应结构。
当前账号 uid：`5555027006`（一刀流小依）

目的：核对 `weibo-cli` 现有接口是否正确/高效，发现可补充的有用功能。仅覆盖 `https://weibo.com/ajax/*` 端点；`m.weibo.cn` 移动端、`s.weibo.com` HTML 端不在本次范围。

---

## 0. 与现状对照总览

| 我们的方法 | 我们用的端点 | 网页实际端点 | 结论 |
|---|---|---|---|
| `get_hot_search` | `/ajax/side/hotSearch` | 同 | ✅ 一致，51 条 |
| `get_hot_timeline` | `/ajax/feed/hottimeline` | 同（参数完全一致） | ✅ 一致 |
| `get_friends_timeline` | `/ajax/feed/friendstimeline?list_id=` | `/ajax/feed/unreadfriendstimeline?list_id=<gid>&refresh=4&since_id=0` | ⚠️ 可用但语义弱，见 §2 |
| `get_feed_groups` | `/ajax/feed/allGroups` | 同 | ✅ 一致，见 §1 |
| `get_profile` | `/ajax/profile/info?uid=X` | `/ajax/profile/info?uid=X&scene=profile` | ⚠️ 缺 `scene`，见 §5 |
| `get_user_weibos` | `/ajax/statuses/mymblog?uid=&page=&feature=` | 同 | ✅ 一致 |
| `get_weibo_detail` | `/ajax/statuses/show?id=X` | `/ajax/statuses/show?id=X&locale=zh-CN&isGetLongText=true` | ⚠️ 长文截断，见 §4 |
| `get_comments` | `/ajax/statuses/buildComments` | 同 | ✅ 已修复（unwrap=False + `--max-id`），见 §3 |
| `get_reposts` | `/ajax/statuses/repostTimeline` | 同 | ✅ 一致 |
| `get_following` / `get_follow_content` | `/ajax/friendships/friends` + `/ajax/profile/followContent` | 同 | ✅ 一致 |
| `search_weibo` | `m.weibo.cn/api/container/getIndex` | 同（需 `.weibo.cn` 会话） | ✅ 已修复（2026-07-11）：QR cdurl 跨域补建 `mobile_cookies`，见 §8 |

未用但有用的端点：`profile/detail`（§6）、`favorites/all_fav`（§7.1）、`statuses/likelist`（§7.2）、`message/unreadHint`（§7.3）。

---

## 1. Feed 分组列表：`/ajax/feed/allGroups`

**请求**：`GET /ajax/feed/allGroups?is_new_segment=1&fetch_hot=1`（公开，无需登录）

**响应顶层字段**（注意：`groups` 在顶层，不在 `data` 下）：
```json
{ "groups": [...], "total_number": N, "fetch_hot": 1, "is_new_segment": 1,
  "feed_default": "...", "trans_param": {...}, "ok": 1 }
```

`groups` 是分类数组，每类含 `title` 与 `list`（item 有 `gid` 即 feed 用的 `list_id`）。实测分类：
- 默认分组：`100015555027006`、`110005555027006`、`3845095910683202`、`100095555027006`
- 我的分组：`3984985905924412`、`4000563387439639`、`4005281593473267`、`4009836540696959` ...
- 订阅分组：（空）
- 我的频道：`102803`、`102803600564`、`1028032222`、`102803600343` ...
- 频道推荐：...

> 注：item 的中文名（"全部关注"/"特别关注"/"好友圈"等）不在 `name` 字段，需结合页面侧栏或 `feed_default` 映射，本次未定位到字段。`gid` 即后续 feed 请求的 `list_id`。

**对我们的意义**：`get_feed_groups` 现状 `unwrap=False` 正确（顶层即 groups）。可用于"分组 Feed"功能（见 §2）。

---

## 2. Feed：`friendstimeline` vs `unreadfriendstimeline`

两端点都返回 200，`statuses` 数组在**顶层**（我们 `unwrap=False` 正确）：

```
GET /ajax/feed/friendstimeline?list_id=&count=20&max_id=0
→ { ok, statuses:[...], total_number, since_id, max_id, since_id_str, max_id_str }

GET /ajax/feed/unreadfriendstimeline?list_id=100015555027006&refresh=4&since_id=0&count=15
→ { ok, statuses:[...], since_id, max_id, since_id_str, max_id_str }
```

- `friendstimeline?list_id=`（空串）仍可用——CLAUDE.md 里"list_id 必填、空串兜底"的坑依然成立，现状不算 bug。
- `unreadfriendstimeline` 是网页首页实际调用的端点，额外带 `refresh=4`（刷新语义）与 `since_id`（未读游标），`list_id` 为真实 group id（来自 §1）。

**对我们的意义**：可新增"分组 Feed / 只看未读"。建议命令 `weibo feed --group <gid 或 名称>` 或 `--unread`，底层走 `unreadfriendstimeline`，`list_id` 由 `get_feed_groups()` 解析得到。`friendstimeline` 维持现状作为兜底。

---

## 3. 评论：`/ajax/statuses/buildComments`（✅ 已修复 2026-07-14）

**请求**：`GET /ajax/statuses/buildComments?id=<idstr>&is_show_bulletin=2&count=20&flow=0[&max_id=<cursor>]`

**实测**（微博 `5318504689435131`，104 条评论）：
```json
{
  "ok": 1,
  "max_id": 717786094288222,      // 下一页游标，与 data 同层
  "total_number": 99,             // 总数，与 data 同层
  "data": [ /* 评论数组，20 条/页 */ ],
  "rootComment": {...},
  "tip_msg": {...},
  "trendsText": "..."
}
```

`data[0]` 关键字段：`created_at, id, rootid, rootidstr, floor_number, text, source, user, mid, idstr, liked, pic_num, ...`

**问题（已修复）**：我们 `get_comments` 原用默认 `unwrap=True`，`_handle_response` 只返回 `data["data"]`（评论数组），**丢掉了 `max_id` 与 `total_number`**——导致无法翻页、拿不到总评论数。

**修复**：`get_comments` 改 `unwrap=False` 返回完整 dict；`weibo comments` 加 `--max-id` 透传游标，纯文本渲染显示「共 N 条，本页 M」+ 下页 `--max-id` 提示，`--json` 暴露完整 `{max_id,total_number,data,...}`。端到端实测：`weibo comments <mblogid>` → page1 `max_id=X`；`--max-id X` → page2 不同评论、新 `max_id`。配测试 `tests/test_client.py::TestCommentsAPI` + `tests/test_cli.py::test_comments_renders_total_and_next_max_id`。

> 注：实测 `buildComments` 的 `id` 参数同时接受数字 idstr 与 base62 mblogid（两者均 ok=1、返回评论）；CLI 仍走 `get_weibo_detail(mblogid)` 先解析数字 id，未改（属优化非 bug）。`total_number` 与 mblog 的 `comments_count` 时同/时异（被过滤评论等），实测有 267↔258 不一致情况。

---

## 4. 微博详情：`/ajax/statuses/show`（⚠️ 长文截断）

**请求**：`GET /ajax/statuses/show?id=<mblogid>&locale=zh-CN&isGetLongText=true`

**响应**：mblog 字段在**顶层**（含一个混入的 `ok`），无 `data` 包装。我们 `unwrap=False` 正确。

顶层关键字段（实测微博 `R80i7icJ4`）：
```
visible, created_at, id, idstr, mid, mblogid, user, can_edit, source,
favorited, pic_ids, pic_num, is_paid, reposts_count, comments_count,
attitudes_count, attitudes_status, isLongText, text, text_raw,
region_name, retweeted_status, ok
```

**问题**：网页带 `isGetLongText=true` 拉详情。我们未传该参数，对 `isLongText=true` 的长微博，`text` 可能是截断版（带"...全文"）。

**✅ 已修复（2026-07-11）**：`get_weibo_detail` 已加 `isGetLongText=true` 参数。实测长微博 `R8cuZ8uMW`：`text_raw` 从 155 字符（截断在「全高约1」）补全到 212 字符（完整结尾 hashtag）；与 `/ajax/statuses/longtext` 端点的 `longTextContent` 完全一致，故未引入该端点（YAGNI）。普通微博不受影响。

---

## 5. 用户资料：`/ajax/profile/info`（⚠️ 缺 `scene`）

**请求**：`GET /ajax/profile/info?uid=<uid>&scene=profile`（网页带 `scene=profile`）

**响应**：`{ ok, data: {...} }`，`data` 含 `id, screen_name, gender, followers_count, follow_count, statuses_count, description, profile_image_url, ...`。`unwrap=True` 正确。

**建议**：`get_profile` 补 `scene="profile"` 参数对齐网页行为（降低风控判异常风险）。属功能增强，无需配测试（按 CLAUDE.md 约定）。

---

## 6. 用户资料详情：`/ajax/profile/detail`（新端点）

**请求**：`GET /ajax/profile/detail?uid=<uid>`（需登录）

**响应**（实测 uid `5555027006`）：
```json
{ "ok":1, "data": {
  "sunshine_credit": { "level": "阳光信用较好" },
  "education": { "school": "南京大学" },
  "birthday": "1996-07-12",
  "created_at": "2015-05-22 00:29:25",
  "description": "暂无简介",
  "gender": "m",
  "ip_location": "IP属地：江苏",
  "real_name": { "name": "...", "career": "..." },
  "label_desc": [...], "desc_text": "...", "verified_url": "..."
}}
```

比 `profile/info` 丰富得多（学历/生日/IP属地/阳光信用/真实信息）。**我们完全没用。**

**建议**：`weibo profile` 合并展示 `profile/info` + `profile/detail`，或新增 `--detail` 开关。新增 `get_profile_detail(uid)` 方法。

---

## 7. 尚未支持但有用的端点（新功能候选）

### 7.1 收藏列表：`/ajax/favorites/all_fav`

**请求**：`GET /ajax/favorites/all_fav?uid=<uid>&page=<n>&with_total=true`

**响应**：`{ ok, data: { status:[...mblog], total_number } }`，20 条/页。`data.status[0]` 是完整 mblog（字段同 §4）。

**辅助**：`/ajax/favorites/tags?page=1&is_show_total=1`（收藏标签/分类）。

入口：`https://weibo.com/u/page/fav/<uid>`。建议命令 `weibo favorites [--page N]`（列出我收藏的微博）。

### 7.2 赞过的微博：`/ajax/statuses/likelist`

**请求**：`GET /ajax/statuses/likelist?uid=<uid>&page=<n>&with_total=true`

**响应**：`{ ok, data: { list:[...mblog], ... } }`，实测 19 条/页。入口：`https://weibo.com/u/page/like/<uid>`。建议命令 `weibo likes <uid> [--page N]`。

### 7.3 未读消息提示：`/ajax/message/unreadHint`

**请求**：`GET /ajax/message/unreadHint?group_ids=<逗号分隔的会话id>`

**响应**：各群组未读数。另有 `https://rm.api.weibo.com/2/remind/push_count.json`（未读 @我/评论/私信总数）。

建议命令 `weibo unread`（未读计数）。`group_ids` 来源待进一步确认。

### 7.4 最近访问

入口 `https://weibo.com/u/page/visit/<uid>`（"谁看过我"），页面级，本次未确认其 ajax 端点，待补抓。

---

## 8. 搜索（✅ 已修复 2026-07-11）

`weibo search` 原走 `m.weibo.cn/api/container/getIndex`（移动端 UA），但 QR 登录只建 `weibo.com` 会话、不建 `m.weibo.cn` 会话（`.weibo.cn` 与 `.weibo.com` 不同注册域），命中 `ok=-100`。

**修复**（实测端到端确认，`weibo search 微博` / `--json` 均通 `ok=1`、9 条 mblog）：QR 成功后 `data.url`（`passport.weibo.com/sso/v2/login?...&alt=ALT-...`）302 时设 `.weibo.com` cookies 并重定向到 `login.sina.com.cn/sso/v2/crossdomain?...&cdurl=<passport.weibo.cn/sso/crossdomain?...&ticket=...>`。`_exchange_mobile_cookies` 对 data.url **单次** `follow_redirects=False` 探测（alt 一次性令牌，二次 GET 返 200 不再 302），从 302 Set-Cookie 捕获 `.weibo.com` cookies、从 Location 解析 `cdurl`，再直连 `passport.weibo.cn/sso/crossdomain?...&savestate=30`（绕过会 403 的 `login.sina.com.cn`），event_hooks 解析 Set-Cookie 只收 `.weibo.cn` cookies 存入 `Credential.mobile_cookies`；`_build_mobile_client` 优先用之。详见 `docs/troubleshooting.md` 问题四。

本次调研期间另发现：新 PC 前端 `weibo-pro-next` 的搜索 XHR 是 `/ajax/search/all`（params `containerid/page/count/mark`），但 `weibo.com/ajax/search/all`→404、`s.weibo.com/ajax/search/all`→302→sorry，且搜索 URL 带反爬 `t` token（`getTCode`/`getSearchTScene`，混淆 JS 算），不带就 404——未走此路。

---

## 9. 落地优先级建议

1. **✅ §3 评论分页游标修复**（已落地 2026-07-14）→ `unwrap=False` + `--max-id` + 暴露 `max_id`/`total_number`
2. **§4 详情长文** `isGetLongText=true` + 暴露 `text_raw`/`favorited`（bug 类，配测试）
3. **§6 `profile/detail` 合并**（增强）→ `weibo profile` 更全
4. **§7.1 `weibo favorites`**（新功能）
5. **§7.2 `weibo likes`**（新功能）
6. **§2 分组/未读 Feed**（增强）
7. **§5 `profile/info` 补 `scene`**（小修，随 §3/§4 一起做即可）

调研期间抓取的原始响应样本见 `.playwright-cli/s_*.json`（临时文件，可随清理）。

"""API client for Weibo with rate limiting, retry, and anti-detection."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from .auth import Credential
from .constants import (
    BASE_URL,
    BUILD_COMMENTS_URL,
    FEED_GROUPS_URL,
    FOLLOWERS_URL,
    FOLLOW_CONTENT_URL,
    FRIENDS_TIMELINE_URL,
    FRIENDS_URL,
    GET_CONFIG_URL,
    HEADERS,
    HOT_BAND_URL,
    HOT_SEARCH_URL,
    HOT_TIMELINE_URL,
    MOBILE_BASE_URL,
    MOBILE_HEADERS,
    MOBILE_SEARCH_URL,
    MY_MBLOG_URL,
    PROFILE_INFO_URL,
    REPOST_TIMELINE_URL,
    SEARCH_BAND_URL,
    STATUSES_SHOW_URL,
)
from .exceptions import WeiboApiError, SessionExpiredError

logger = logging.getLogger(__name__)


class WeiboClient:
    """Weibo API client with Gaussian jitter, exponential backoff, and session-stable identity.

    Anti-detection strategy:
    - Gaussian jitter delay between requests (~1s mean, σ=0.3)
    - 5% chance of a random long pause (2-5s) to mimic reading behavior
    - Exponential backoff on HTTP 429/5xx (up to 3 retries)
    - Response cookies merged back into session jar
    """

    def __init__(
        self,
        credential: Credential | None = None,
        timeout: float = 30.0,
        request_delay: float = 1.0,
        max_retries: int = 3,
    ):
        self.credential = credential
        self._timeout = timeout
        self._request_delay = request_delay
        self._base_request_delay = request_delay
        self._max_retries = max_retries
        self._last_request_time = 0.0
        self._request_count = 0
        self._rate_limit_count = 0
        self._http: httpx.Client | None = None

    def _build_client(self) -> httpx.Client:
        cookies = {}
        if self.credential:
            cookies = self.credential.cookies
        return httpx.Client(
            base_url=BASE_URL,
            headers=dict(HEADERS),
            cookies=cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
        )

    @property
    def client(self) -> httpx.Client:
        if not self._http:
            raise RuntimeError("Client not initialized. Use 'with WeiboClient() as client:'")
        return self._http

    def __enter__(self) -> WeiboClient:
        self._http = self._build_client()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._http:
            self._http.close()
            self._http = None

    # ── Rate limiting ───────────────────────────────────────────────

    def _rate_limit_delay(self) -> None:
        if self._request_delay <= 0:
            return
        elapsed = time.time() - self._last_request_time
        if elapsed < self._request_delay:
            jitter = max(0, random.gauss(0.3, 0.15))
            if random.random() < 0.05:
                jitter += random.uniform(2.0, 5.0)
            sleep_time = self._request_delay - elapsed + jitter
            logger.debug("Rate-limit delay: %.2fs", sleep_time)
            time.sleep(sleep_time)

    def _mark_request(self) -> None:
        self._last_request_time = time.time()
        self._request_count += 1

    # ── Response handling ───────────────────────────────────────────

    def _merge_response_cookies(self, resp: httpx.Response) -> None:
        for name, value in resp.cookies.items():
            if value:
                self.client.cookies.set(name, value)

    def _handle_response(self, data: dict[str, Any], action: str, *, unwrap: bool = True) -> dict[str, Any]:
        """Validate API response.

        Weibo uses {ok: 1, data: {...}} format for most endpoints.
        When unwrap=True (default), extract and return data["data"].
        When unwrap=False, return the full response dict (for APIs that don't wrap data).
        """
        ok = data.get("ok")

        if ok == -100:
            raise SessionExpiredError()

        message = data.get("msg", data.get("message", "Unknown error"))

        _SESSION_EXPIRED_KEYWORDS = ("请先登录", "请登录后使用", "请登录", "用户未登录")
        if ok == 0:
            msg_str = str(message)
            if any(kw in msg_str for kw in _SESSION_EXPIRED_KEYWORDS):
                raise SessionExpiredError()
            raise WeiboApiError(f"{action}: {message} (ok={ok})", code=ok, response=data)

        if ok == 1:
            return data.get("data", data) if unwrap else data

        # ok is some other truthy value (e.g. raw APIs return full dict)
        if ok:
            return data.get("data", data) if unwrap else data

        raise WeiboApiError(f"{action}: {message} (ok={ok})", code=ok, response=data)

    # ── Request with retry ──────────────────────────────────────────

    def _request(self, method: str, url: str, *, client: httpx.Client | None = None, **kwargs) -> dict[str, Any]:
        self._rate_limit_delay()
        last_exc: Exception | None = None
        last_status: int | None = None
        last_body: str = ""
        http = client or self.client

        for attempt in range(self._max_retries):
            t0 = time.time()
            try:
                resp = http.request(method, url, **kwargs)
                elapsed = time.time() - t0
                if not client:  # only merge cookies for the main client
                    self._merge_response_cookies(resp)
                self._mark_request()

                params = kwargs.get("params")
                logger.info(
                    "[#%d] %s %s%s → %d (%.2fs)",
                    self._request_count, method, url[:60],
                    f" params={params}" if params else "",
                    resp.status_code, elapsed,
                )

                if resp.status_code in (429, 500, 502, 503, 504):
                    last_status = resp.status_code
                    last_body = resp.text[:500]
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "HTTP %d, retrying in %.1fs (%d/%d) body=%s",
                        resp.status_code, wait, attempt + 1, self._max_retries, last_body,
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                text = resp.text
                if text.startswith("<"):
                    raise WeiboApiError(f"Received HTML instead of JSON from {url}: {text[:200]}")
                return resp.json()

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                wait = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)

        if last_exc:
            raise WeiboApiError(f"Request failed after {self._max_retries} retries: {last_exc}") from last_exc
        if last_status is not None:
            raise WeiboApiError(
                f"Request failed after {self._max_retries} retries: HTTP {last_status} body={last_body}"
            )
        raise WeiboApiError(f"Request failed after {self._max_retries} retries")

    def _get(self, url: str, params: dict[str, Any] | None = None, action: str = "", *, unwrap: bool = True) -> dict[str, Any]:
        data = self._request("GET", url, params=params)
        return self._handle_response(data, action, unwrap=unwrap)

    # ── Hot Search / Trending ───────────────────────────────────────

    def get_hot_search(self) -> dict[str, Any]:
        """Get hot search list (微博热搜 sidebar, ~52 items)."""
        return self._get(HOT_SEARCH_URL, action="热搜")

    def get_hot_band(self) -> dict[str, Any]:
        """Get full hot band list (微博热搜榜)."""
        return self._get(HOT_BAND_URL, action="热搜榜")

    def get_search_band(self) -> dict[str, Any]:
        """Get search band (trending sidebar, ~16 items)."""
        return self._get(SEARCH_BAND_URL, action="搜索推荐")

    # ── Feed / Timeline ─────────────────────────────────────────────

    def get_hot_timeline(self, group_id: str = "102803", count: int = 10, max_id: str = "0") -> dict[str, Any]:
        """Get hot timeline (热门微博 feed)."""
        return self._get(HOT_TIMELINE_URL, params={
            "since_id": "0", "refresh": "0",
            "group_id": group_id, "containerid": group_id,
            "extparam": "discover|new_feed",
            "max_id": max_id, "count": str(count),
        }, action="热门Feed", unwrap=False)

    def get_friends_timeline(self, count: int = 20, max_id: str = "0") -> dict[str, Any]:
        """Get friends timeline (关注者 feed, requires auth).

        list_id is mandatory: weibo.com's server JS calls .slice() on it and
        returns HTTP 500 ("Cannot read properties of undefined") when absent.
        """
        return self._get(FRIENDS_TIMELINE_URL, params={
            "count": str(count), "max_id": max_id, "list_id": "",
        }, action="关注Feed", unwrap=False)

    def get_feed_groups(self) -> dict[str, Any]:
        """Get feed group configuration."""
        return self._get(FEED_GROUPS_URL, params={"is_new_segment": "1", "fetch_hot": "1"}, action="Feed分组", unwrap=False)

    # ── User / Profile ──────────────────────────────────────────────

    def get_profile(self, uid: str) -> dict[str, Any]:
        """Get user profile info."""
        return self._get(PROFILE_INFO_URL, params={"uid": uid}, action="用户资料")

    def get_user_weibos(self, uid: str, page: int = 1, count: int = 20, feature: int = 0) -> dict[str, Any]:
        """Get user's weibo list."""
        return self._get(MY_MBLOG_URL, params={
            "uid": uid, "page": str(page), "feature": str(feature),
        }, action="用户微博")

    # ── Weibo Detail ────────────────────────────────────────────────

    def get_weibo_detail(self, mblogid: str) -> dict[str, Any]:
        """Get single weibo detail by mblogid (e.g. 'Qw06Kd98p').

        isGetLongText=true 对齐网页行为：对 isLongText=true 的长微博，服务端
        会把 text_raw / text 补全为全文（实测：155 → 212 字符、完整结尾 hashtag），
        否则只返回带「...全文」的截断版。普通微博不受影响。
        """
        return self._get(
            STATUSES_SHOW_URL,
            params={"id": mblogid, "isGetLongText": "true"},
            action="微博详情", unwrap=False,
        )

    # ── Comments / Reposts ──────────────────────────────────────────

    def get_comments(self, weibo_id: str, count: int = 20, max_id: int = 0) -> dict[str, Any]:
        """Get comments for a weibo."""
        params: dict[str, Any] = {"id": weibo_id, "is_show_bulletin": "2", "count": str(count), "flow": "0"}
        if max_id:
            params["max_id"] = str(max_id)
        return self._get(BUILD_COMMENTS_URL, params=params, action="评论")

    def get_reposts(self, weibo_id: str, page: int = 1, count: int = 10) -> dict[str, Any]:
        """Get repost/forward list for a weibo."""
        return self._get(REPOST_TIMELINE_URL, params={
            "id": weibo_id, "page": str(page), "count": str(count),
        }, action="转发", unwrap=False)

    # ── Social ──────────────────────────────────────────────────────

    def get_following(self, uid: str, page: int = 1) -> dict[str, Any]:
        """Get user's following list."""
        return self._get(FRIENDS_URL, params={"uid": uid, "page": str(page)}, action="关注列表", unwrap=False)

    def get_followers(self, uid: str, page: int = 1) -> dict[str, Any]:
        """Get user's follower list."""
        return self._get(FOLLOWERS_URL, params={
            "uid": uid, "page": str(page), "relate": "fans",
        }, action="粉丝列表", unwrap=False)

    # ── Following list (self-aware: larger page + native search) ───────

    def get_follow_content(
        self, *, page: int = 1, next_cursor: int | None = None,
        sort_type: str = "all", q: str | None = None,
    ) -> dict[str, Any]:
        """One page of the *current user's own* follow list via followContent.

        This endpoint has no uid param — it always returns the logged-in
        user's follows, so only call it when uid == get_current_uid(). It has
        a larger page size (~48-49 vs 19 for friendships/friends) and a native
        pinyin-aware search (sortType=search&q=...).

        Page 1 is requested with no page/next_cursor; page 2+ must carry both
        page=<N> and next_cursor=<cursor from the previous page>.

        Returns the inner 'data' dict:
        {total_number, specialAttention:{users,...},
         follows:{users, next_cursor, previous_cursor, total_number, has_filtered_attentions}}.
        """
        params: dict[str, Any] = {"sortType": sort_type}
        if q:
            params["q"] = q
        if next_cursor is not None:
            params["page"] = str(page)
            params["next_cursor"] = str(next_cursor)
        data = self._request(
            "GET", FOLLOW_CONTENT_URL, params=params,
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        return self._handle_response(data, action="关注列表", unwrap=True)

    @staticmethod
    def _follow_content_users(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Merge specialAttention + follows users from a followContent page."""
        users: list[dict[str, Any]] = []
        for key in ("specialAttention", "follows"):
            block = data.get(key, {}) if isinstance(data, dict) else {}
            items = block.get("users", []) if isinstance(block, dict) else []
            users.extend(items or [])
        return users

    @staticmethod
    def _user_id_str(u: dict[str, Any]) -> str:
        return str(u.get("idstr", u.get("id", ""))) or ""

    @staticmethod
    def _filter_users(users: list[dict[str, Any]], q: str) -> list[dict[str, Any]]:
        """Simple case-insensitive contains filter on screen_name/name/description/remark."""
        needle = (q or "").lower()

        def match(u: dict[str, Any]) -> bool:
            hay = " ".join(
                str(u.get(k, "") or "")
                for k in ("screen_name", "name", "description", "remark")
            )
            return needle in hay.lower()

        return [u for u in users if match(u)]

    def _normalize_following(
        self, users: list[dict[str, Any]], *, total: int | None, source: str, q: str | None,
    ) -> dict[str, Any]:
        return {"users": users, "total": total, "fetched": len(users), "source": source, "search": q}

    def _fetch_all_friendships(self, uid: str) -> dict[str, Any]:
        """Paginate /ajax/friendships/friends for uid until users is empty."""
        users: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        for pg in range(1, 101):  # backstop: 100 pages
            data = self.get_following(uid, page=pg)
            page_users = data.get("users", []) if isinstance(data, dict) else []
            if isinstance(data, dict):
                total = data.get("total_number", total)
            if not page_users:
                break
            for u in page_users:
                uid_s = self._user_id_str(u)
                if uid_s and uid_s not in seen:
                    seen.add(uid_s)
                    users.append(u)
        logger.info("following(all, friendships) uid=%s fetched=%d total=%s", uid, len(users), total)
        return self._normalize_following(users, total=total, source="friendships", q=None)

    def _fetch_all_follow_content(self) -> dict[str, Any]:
        """Paginate followContent (cursor-based) until next_cursor == 0."""
        users: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        page = 1
        next_cursor: int | None = None
        for _ in range(100):  # backstop
            data = self.get_follow_content(page=page, next_cursor=next_cursor)
            follows = data.get("follows", {}) if isinstance(data, dict) else {}
            page_users = follows.get("users", []) if isinstance(follows, dict) else []
            total = follows.get("total_number", total) if isinstance(follows, dict) else total
            for u in self._follow_content_users(data):
                uid_s = self._user_id_str(u)
                if uid_s and uid_s not in seen:
                    seen.add(uid_s)
                    users.append(u)
            next_cursor = follows.get("next_cursor") if isinstance(follows, dict) else None
            if not page_users or not next_cursor:
                break
            page += 1
        logger.info("following(all, followContent) fetched=%d total=%s", len(users), total)
        return self._normalize_following(users, total=total, source="followContent", q=None)

    def get_following_list(
        self, uid: str, *, is_self: bool, page: int = 1,
        fetch_all: bool = False, q: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a following list, normalized to {users, total, fetched, source, search}.

        Routing:
        - is_self (uid == current user) → followContent (larger page, native
          pinyin search when q is given).
        - otherwise → /ajax/friendships/friends (page-based); q triggers a full
          fetch + local contains filter.

        Precedence: q > fetch_all > page.
        """
        if is_self:
            if q:
                data = self.get_follow_content(sort_type="search", q=q)
                users = self._follow_content_users(data)
                logger.info("following(search self) q=%s hits=%d", q, len(users))
                return self._normalize_following(users, total=None, source="followContent", q=q)
            if fetch_all:
                return self._fetch_all_follow_content()
            data = self.get_follow_content(page=1)
            follows = data.get("follows", {}) if isinstance(data, dict) else {}
            users = self._follow_content_users(data)
            total = follows.get("total_number") if isinstance(follows, dict) else None
            logger.info("following(self page=%d) users=%d total=%s", page, len(users), total)
            return self._normalize_following(users, total=total, source="followContent", q=None)

        # non-self
        if q:
            agg = self._fetch_all_friendships(uid)
            users = self._filter_users(agg["users"], q)
            logger.info("following(search non-self) q=%s hits=%d", q, len(users))
            return self._normalize_following(users, total=agg.get("total"), source="friendships", q=q)
        if fetch_all:
            return self._fetch_all_friendships(uid)
        data = self.get_following(uid, page=page)
        users = data.get("users", []) if isinstance(data, dict) else []
        total = data.get("total_number") if isinstance(data, dict) else None
        logger.info("following(non-self page=%d) users=%d total=%s", page, len(users), total)
        return self._normalize_following(users, total=total, source="friendships", q=None)

    # ── Search ──────────────────────────────────────────────────────

    def _build_mobile_client(self) -> httpx.Client:
        """Build a mobile API client for m.weibo.cn endpoints.

        Uses the `.weibo.cn` mobile_cookies from the QR cross-domain cdurl
        exchange when available — m.weibo.cn needs a .weibo.cn session that the
        .weibo.com cookies cannot cover. Falls back to the main cookies (which
        leaves search unavailable, the pre-fix behavior) when absent.
        """
        if self.credential and self.credential.mobile_cookies:
            cookies = self.credential.mobile_cookies
        elif self.credential:
            cookies = self.credential.cookies
        else:
            cookies = {}
        return httpx.Client(
            base_url=MOBILE_BASE_URL,
            headers=dict(MOBILE_HEADERS),
            cookies=cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
        )

    def search_weibo(self, keyword: str, page: int = 1) -> dict[str, Any]:
        """Search weibos by keyword using mobile API."""
        containerid = f"100103type=1&q={keyword}"
        params = {
            "containerid": containerid,
            "page_type": "searchall",
            "page": str(page),
        }
        with self._build_mobile_client() as mobile:
            data = self._request("GET", MOBILE_SEARCH_URL, params=params, client=mobile)
        return data

    # ── Config ──────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        """Get app configuration (contains current user info)."""
        return self._get(GET_CONFIG_URL, action="配置")

    def get_current_uid(self) -> str | None:
        """Get current logged-in user's uid from the x-log-uid response header.

        weibo.com sets x-log-uid on authenticated ajax responses (verified on
        /ajax/config/get_config and /ajax/feed/friendstimeline). Used by `weibo me`
        because /ajax/profile/me is 404 and get_config's data has no uid field.
        Returns None if the header is absent (not logged in / anonymous).
        """
        self._rate_limit_delay()
        resp = self.client.get(GET_CONFIG_URL)
        self._merge_response_cookies(resp)
        self._mark_request()
        logger.info("[#%d] GET %s → %d", self._request_count, GET_CONFIG_URL[:60], resp.status_code)
        uid = resp.headers.get("x-log-uid")
        logger.info("Current uid from x-log-uid: %s", uid)
        return uid

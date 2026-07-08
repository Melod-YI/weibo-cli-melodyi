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
        """Get single weibo detail by mblogid (e.g. 'Qw06Kd98p')."""
        return self._get(STATUSES_SHOW_URL, params={"id": mblogid}, action="微博详情", unwrap=False)

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

    # ── Search ──────────────────────────────────────────────────────

    def _build_mobile_client(self) -> httpx.Client:
        """Build a mobile API client for m.weibo.cn endpoints."""
        cookies = self.credential.cookies if self.credential else {}
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

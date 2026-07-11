"""Unit tests for WeiboClient — mock all API methods, verify URL/params/response handling."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from weibo_cli.client import WeiboClient
from weibo_cli.exceptions import SessionExpiredError, WeiboApiError


# ── Response handling ────────────────────────────────────────────────


class TestHandleResponse:
    def test_ok_1_with_data_key(self, mock_client):
        raw = {"ok": 1, "data": {"realtime": [{"word": "test"}]}}
        result = mock_client._handle_response(raw, "test")
        assert result == {"realtime": [{"word": "test"}]}

    def test_ok_1_without_data_key(self, mock_client):
        raw = {"ok": 1, "statuses": []}
        result = mock_client._handle_response(raw, "test")
        assert result == raw

    def test_ok_minus_100_raises_session_expired(self, mock_client):
        raw = {"ok": -100, "url": "https://weibo.com/login.php"}
        with pytest.raises(SessionExpiredError):
            mock_client._handle_response(raw, "test")

    def test_ok_0_login_message_raises_session_expired(self, mock_client):
        raw = {"ok": 0, "message": "请先登录"}
        with pytest.raises(SessionExpiredError):
            mock_client._handle_response(raw, "test")

    def test_ok_0_login_后使用_raises_session_expired(self, mock_client):
        raw = {"ok": 0, "message": "请登录后使用"}
        with pytest.raises(SessionExpiredError):
            mock_client._handle_response(raw, "test")

    def test_ok_0_generic_error(self, mock_client):
        raw = {"ok": 0, "message": "参数错误"}
        with pytest.raises(WeiboApiError, match="参数错误"):
            mock_client._handle_response(raw, "test")


# ── Context manager ─────────────────────────────────────────────────


class TestContextManager:
    def test_enter_creates_client(self, mock_credential):
        client = WeiboClient(mock_credential, request_delay=0)
        with client as c:
            assert c.client is not None
            assert c._http is not None

    def test_exit_closes_client(self, mock_credential):
        client = WeiboClient(mock_credential, request_delay=0)
        with client:
            pass
        assert client._http is None

    def test_client_without_context_raises(self, mock_credential):
        client = WeiboClient(mock_credential, request_delay=0)
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = client.client


# ── Rate limiting ────────────────────────────────────────────────────


class TestRateLimiting:
    def test_mark_request_increments_counter(self, mock_client):
        assert mock_client._request_count == 0
        mock_client._mark_request()
        assert mock_client._request_count == 1
        mock_client._mark_request()
        assert mock_client._request_count == 2


# ── API method tests (mocked HTTP) ──────────────────────────────────


class TestHotSearchAPI:
    def test_get_hot_search_calls_correct_url(self, mock_client, hot_search_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(hot_search_response)
        mock_resp.json.return_value = hot_search_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        result = mock_client.get_hot_search()
        assert "realtime" in result
        assert len(result["realtime"]) == 2
        assert result["realtime"][0]["word"] == "省考"

        # Verify correct URL was called
        call_args = mock_client._http.request.call_args
        assert call_args[0][0] == "GET"
        assert "/ajax/side/hotSearch" in call_args[0][1]


class TestProfileAPI:
    def test_get_profile_passes_uid(self, mock_client, profile_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(profile_response)
        mock_resp.json.return_value = profile_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        result = mock_client.get_profile("1699432410")
        assert result["user"]["screen_name"] == "新华社"

        call_args = mock_client._http.request.call_args
        assert "/ajax/profile/info" in call_args[0][1]
        params = call_args[1].get("params", {})
        assert params["uid"] == "1699432410"


class TestWeiboDetailAPI:
    def test_get_weibo_detail(self, mock_client, weibo_detail_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(weibo_detail_response)
        mock_resp.json.return_value = weibo_detail_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        result = mock_client.get_weibo_detail("Qw06Kd98p")
        assert result["mblogid"] == "Qw06Kd98p"
        assert result["user"]["screen_name"] == "新华社"

        call_args = mock_client._http.request.call_args
        assert "/ajax/statuses/show" in call_args[0][1]
        params = call_args[1].get("params", {})
        assert params["id"] == "Qw06Kd98p"


class TestHotTimelineAPI:
    def test_get_hot_timeline_default_params(self, mock_client):
        hot_response = {"ok": 1, "statuses": [], "max_id": "0"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(hot_response)
        mock_resp.json.return_value = hot_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        result = mock_client.get_hot_timeline()
        assert "statuses" in result

        call_args = mock_client._http.request.call_args
        params = call_args[1].get("params", {})
        assert params["group_id"] == "102803"
        assert params["count"] == "10"

    def test_get_hot_timeline_custom_count(self, mock_client):
        hot_response = {"ok": 1, "statuses": [], "max_id": "0"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(hot_response)
        mock_resp.json.return_value = hot_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        mock_client.get_hot_timeline(count=5)
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["count"] == "5"


class TestCommentsAPI:
    def test_get_comments_default_params(self, mock_client):
        comments_response = {"ok": 1, "data": [], "max_id": 0}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(comments_response)
        mock_resp.json.return_value = comments_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        mock_client.get_comments("12345")
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["id"] == "12345"
        assert params["count"] == "20"
        assert "max_id" not in params

    def test_get_comments_with_max_id(self, mock_client):
        comments_response = {"ok": 1, "data": [], "max_id": 0}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(comments_response)
        mock_resp.json.return_value = comments_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        mock_client.get_comments("12345", max_id=999)
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["max_id"] == "999"


class TestRepostsAPI:
    def test_get_reposts(self, mock_client):
        reposts_response = {"ok": 1, "data": [], "total_number": 0}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(reposts_response)
        mock_resp.json.return_value = reposts_response
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        mock_client.get_reposts("12345", page=2)
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["id"] == "12345"
        assert params["page"] == "2"


# ── Friends timeline (regression: missing list_id → HTTP 500) ───────


class TestFriendsTimelineAPI:
    def test_get_friends_timeline_includes_list_id(self, mock_client):
        """weibo.com /ajax/feed/friendstimeline 500s without list_id
        (server JS calls .slice() on undefined). The param MUST be sent."""
        resp_data = {"ok": 1, "statuses": [], "max_id": "0"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(resp_data)
        mock_resp.json.return_value = resp_data
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        mock_client.get_friends_timeline()
        params = mock_client._http.request.call_args[1].get("params", {})
        assert "list_id" in params, "list_id is required or weibo returns HTTP 500"

    def test_get_friends_timeline_passes_count_and_max_id(self, mock_client):
        resp_data = {"ok": 1, "statuses": [], "max_id": "0"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(resp_data)
        mock_resp.json.return_value = resp_data
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = mock_resp

        mock_client.get_friends_timeline(count=15, max_id="123")
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["count"] == "15"
        assert params["max_id"] == "123"


# ── Current user uid (regression: /ajax/profile/me is 404) ───────────


class TestCurrentUid:
    def test_get_current_uid_from_x_log_uid_header(self, mock_client):
        """weibo.com sets x-log-uid on authenticated ajax responses."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-log-uid": "5555027006"}
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.get.return_value = mock_resp

        assert mock_client.get_current_uid() == "5555027006"

    def test_get_current_uid_none_when_header_missing(self, mock_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.cookies = httpx.Cookies()
        mock_client._http.get.return_value = mock_resp

        assert mock_client.get_current_uid() is None


# ── Retry behavior ──────────────────────────────────────────────────


class TestRetryBehavior:
    def test_retries_on_timeout(self, mock_client):
        mock_client._max_retries = 2
        mock_client._http.request.side_effect = httpx.TimeoutException("timeout")

        with pytest.raises(WeiboApiError, match="failed after"):
            mock_client._request("GET", "/ajax/test")

        assert mock_client._http.request.call_count == 2

    def test_retries_on_server_error(self, mock_client):
        mock_client._max_retries = 2
        error_resp = MagicMock()
        error_resp.status_code = 502
        error_resp.cookies = httpx.Cookies()
        mock_client._http.request.return_value = error_resp

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.text = '{"ok": 1}'
        success_resp.json.return_value = {"ok": 1}
        success_resp.cookies = httpx.Cookies()

        mock_client._http.request.side_effect = [error_resp, success_resp]
        result = mock_client._request("GET", "/ajax/test")
        assert result == {"ok": 1}

    def test_html_response_raises_error(self, mock_client):
        html_resp = MagicMock()
        html_resp.status_code = 200
        html_resp.text = "<html>Login Required</html>"
        html_resp.cookies = httpx.Cookies()
        html_resp.raise_for_status.return_value = None
        mock_client._http.request.return_value = html_resp

        with pytest.raises(WeiboApiError, match="HTML"):
            mock_client._request("GET", "/ajax/test")

    def test_500_logs_response_body(self, mock_client, caplog):
        """HTTP 500 body must be logged so failures are diagnosable."""
        import logging
        caplog.set_level(logging.WARNING, logger="weibo_cli.client")

        mock_client._max_retries = 2
        err = MagicMock()
        err.status_code = 500
        err.text = '{"ok":0,"message":"Cannot read properties of undefined (reading \'slice\')"}'
        err.cookies = httpx.Cookies()
        mock_client._http.request.return_value = err

        with pytest.raises(WeiboApiError):
            mock_client._request("GET", "/ajax/test", params={"count": "20"})

        assert "Cannot read properties of undefined" in caplog.text

    def test_500_failure_message_includes_body(self, mock_client):
        """Final failure error must carry the last response body."""
        mock_client._max_retries = 2
        err = MagicMock()
        err.status_code = 500
        err.text = '{"ok":0,"message":"boom"}'
        err.cookies = httpx.Cookies()
        mock_client._http.request.return_value = err

        with pytest.raises(WeiboApiError, match="boom"):
            mock_client._request("GET", "/ajax/test")


# ── Cookie merging ───────────────────────────────────────────────────


class TestCookieMerging:
    def test_merge_response_cookies(self, mock_credential):
        """Verify that response cookies are merged back into the session."""
        client = WeiboClient(mock_credential, request_delay=0)
        with client:
            resp = MagicMock()
            resp.cookies = httpx.Cookies()
            resp.cookies.set("NEW_COOKIE", "new_value")
            client._merge_response_cookies(resp)
            assert client.client.cookies.get("NEW_COOKIE") == "new_value"


# ── Following list (self-aware followContent routing) ────────────────


def _mock_resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = json.dumps(payload)
    r.json.return_value = payload
    r.cookies = httpx.Cookies()
    return r


def _fc_payload(users, next_cursor=0, total=308, special=None):
    """followContent response: {ok, data:{follows, specialAttention, total_number}}."""
    return {"ok": 1, "data": {
        "total_number": total,
        "specialAttention": {"users": special or []},
        "follows": {"users": users, "next_cursor": next_cursor,
                    "previous_cursor": 0, "total_number": total,
                    "has_filtered_attentions": True},
    }}


def _user(uid, name, desc=""):
    return {"id": uid, "idstr": uid, "screen_name": name, "description": desc, "remark": ""}


class TestFollowContent:
    def test_first_page_omits_page_and_cursor(self, mock_client):
        mock_client._http.request.return_value = _mock_resp(_fc_payload([_user("1", "a")]))
        mock_client.get_follow_content(page=1)
        params = mock_client._http.request.call_args[1]["params"]
        assert params["sortType"] == "all"
        assert "page" not in params and "next_cursor" not in params

    def test_page2_carries_page_and_cursor(self, mock_client):
        mock_client._http.request.return_value = _mock_resp(_fc_payload([_user("2", "b")]))
        mock_client.get_follow_content(page=2, next_cursor=50)
        params = mock_client._http.request.call_args[1]["params"]
        assert params["page"] == "2" and params["next_cursor"] == "50"

    def test_search_params(self, mock_client):
        mock_client._http.request.return_value = _mock_resp(_fc_payload([_user("9", "x")]))
        mock_client.get_follow_content(sort_type="search", q="bl")
        params = mock_client._http.request.call_args[1]["params"]
        assert params["sortType"] == "search" and params["q"] == "bl"

    def test_merges_special_and_follows(self):
        data = {"specialAttention": {"users": [{"idstr": "s1"}]},
                "follows": {"users": [{"idstr": "f1"}, {"idstr": "f2"}]}}
        ids = [u["idstr"] for u in WeiboClient._follow_content_users(data)]
        assert ids == ["s1", "f1", "f2"]

    def test_filter_users_case_insensitive_across_fields(self):
        users = [
            {"idstr": "1", "screen_name": "Alice", "description": "", "remark": ""},
            {"idstr": "2", "screen_name": "Bob", "description": "cart", "remark": ""},
            {"idstr": "3", "screen_name": "x", "description": "", "remark": "CARrot"},
        ]
        hits = WeiboClient._filter_users(users, "car")
        assert {u["idstr"] for u in hits} == {"2", "3"}


class TestGetFollowingList:
    def test_self_single_page_routes_to_follow_content(self, mock_client):
        mock_client._http.request.return_value = _mock_resp(
            _fc_payload([_user("1", "a")], total=10, special=[_user("s", "sp")]))
        d = mock_client.get_following_list("5555", is_self=True, page=1)
        assert d["source"] == "followContent"
        assert d["fetched"] == 2  # 1 follow + 1 special
        assert d["total"] == 10
        assert "followContent" in mock_client._http.request.call_args[0][1]

    def test_self_search_uses_native_search_single_call(self, mock_client):
        mock_client._http.request.return_value = _mock_resp(
            _fc_payload([_user("1", "bl")], special=[]))
        d = mock_client.get_following_list("5555", is_self=True, q="bl")
        assert d["source"] == "followContent" and d["search"] == "bl"
        params = mock_client._http.request.call_args[1]["params"]
        assert params["sortType"] == "search" and params["q"] == "bl"
        assert mock_client._http.request.call_count == 1

    def test_self_all_paginates_until_cursor_zero(self, mock_client):
        p1 = _fc_payload([_user("1", "a")], next_cursor=50, special=[_user("s", "sp")])
        p2 = _fc_payload([_user("2", "b")], next_cursor=100)
        p3 = _fc_payload([], next_cursor=0)
        mock_client._http.request.side_effect = [_mock_resp(p1), _mock_resp(p2), _mock_resp(p3)]
        d = mock_client.get_following_list("5555", is_self=True, fetch_all=True)
        assert d["source"] == "followContent" and d["fetched"] == 3
        assert mock_client._http.request.call_count == 3

    def test_non_self_single_page_uses_friendships(self, mock_client):
        fr = {"ok": 1, "users": [_user("1", "a")], "total_number": 10, "next_cursor": 20}
        mock_client._http.request.return_value = _mock_resp(fr)
        d = mock_client.get_following_list("1699432410", is_self=False, page=1)
        assert d["source"] == "friendships" and d["fetched"] == 1 and d["total"] == 10
        assert "friendships/friends" in mock_client._http.request.call_args[0][1]

    def test_non_self_all_paginates_until_empty(self, mock_client):
        p1 = {"ok": 1, "users": [_user("1", "a")], "total_number": 10, "next_cursor": 20}
        p2 = {"ok": 1, "users": [_user("2", "b")], "next_cursor": 40}
        p3 = {"ok": 1, "users": [], "next_cursor": 0}
        mock_client._http.request.side_effect = [_mock_resp(p1), _mock_resp(p2), _mock_resp(p3)]
        d = mock_client.get_following_list("1699432410", is_self=False, fetch_all=True)
        assert d["source"] == "friendships" and d["fetched"] == 2 and d["total"] == 10
        assert mock_client._http.request.call_count == 3

    def test_non_self_search_fetches_all_then_filters(self, mock_client):
        p1 = {"ok": 1, "users": [_user("1", "Alice"), _user("2", "Bob")], "total_number": 2, "next_cursor": 20}
        p2 = {"ok": 1, "users": [_user("3", "Alicia")], "next_cursor": 0}
        p3 = {"ok": 1, "users": [], "next_cursor": 0}
        mock_client._http.request.side_effect = [_mock_resp(p1), _mock_resp(p2), _mock_resp(p3)]
        d = mock_client.get_following_list("1699432410", is_self=False, q="ali")
        assert d["source"] == "friendships" and d["search"] == "ali"
        assert {u["idstr"] for u in d["users"]} == {"1", "3"}  # Alice + Alicia, not Bob

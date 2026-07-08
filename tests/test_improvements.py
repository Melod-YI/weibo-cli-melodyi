"""Tests for _common.py utilities and new API methods."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from weibo_cli.cli import cli
from weibo_cli.commands._common import format_count, strip_html
from weibo_cli.exceptions import SessionExpiredError, WeiboApiError


# ── strip_html tests ─────────────────────────────────────────────────


class TestStripHtml:
    def test_basic_tags(self):
        assert strip_html("<b>hello</b>") == "hello"

    def test_nested_tags(self):
        assert strip_html("<a href='#'><span>link</span></a>") == "link"

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_none_input(self):
        assert strip_html(None) == ""

    def test_no_tags(self):
        assert strip_html("plain text") == "plain text"

    def test_self_closing_tags(self):
        assert strip_html("hello<br/>world") == "helloworld"

    def test_mixed_content(self):
        assert strip_html("Hello <b>world</b>! <i>Good</i>") == "Hello world! Good"


# ── format_count tests ──────────────────────────────────────────────


class TestFormatCount:
    def test_small_number(self):
        assert format_count(1000) == "1000"

    def test_exact_10000(self):
        assert format_count(10000) == "1.0万"

    def test_large_number(self):
        assert format_count(113614253) == "11361.4万"

    def test_string_input(self):
        assert format_count("5000") == "5000"

    def test_string_large(self):
        assert format_count("50000") == "5.0万"

    def test_invalid_string(self):
        assert format_count("abc") == "abc"

    def test_none_input(self):
        assert format_count(None) == "None"

    def test_zero(self):
        assert format_count(0) == "0"


# ── _handle_response unwrap tests ────────────────────────────────────


class TestHandleResponseUnwrap:
    def test_unwrap_true_extracts_data(self, mock_client):
        raw = {"ok": 1, "data": {"items": [1, 2, 3]}}
        result = mock_client._handle_response(raw, "test", unwrap=True)
        assert result == {"items": [1, 2, 3]}

    def test_unwrap_false_returns_full(self, mock_client):
        raw = {"ok": 1, "data": {"items": [1, 2, 3]}}
        result = mock_client._handle_response(raw, "test", unwrap=False)
        assert result == raw

    def test_unwrap_false_with_raw_api(self, mock_client):
        """APIs like statuses/show return data at top level, not wrapped."""
        raw = {"ok": 1, "mblogid": "abc", "text": "hello"}
        result = mock_client._handle_response(raw, "test", unwrap=False)
        assert result == raw
        assert result["mblogid"] == "abc"

    def test_session_expired_precise_match(self, mock_client):
        """Only precise keywords should trigger SessionExpiredError."""
        raw = {"ok": 0, "message": "请先登录"}
        with pytest.raises(SessionExpiredError):
            mock_client._handle_response(raw, "test")

    def test_login_related_not_falsely_matched(self, mock_client):
        """Messages containing '登录' but not matching keywords should raise WeiboApiError."""
        raw = {"ok": 0, "message": "登录设备异常"}
        with pytest.raises(WeiboApiError, match="登录设备异常"):
            mock_client._handle_response(raw, "test")


# ── New API method tests ─────────────────────────────────────────────


def _mock_response(data):
    """Create a mock httpx response for the given data."""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = json.dumps(data)
    resp.json.return_value = data
    resp.cookies = httpx.Cookies()
    return resp


class TestGetFollowersAPI:
    def test_get_followers_passes_params(self, mock_client):
        response = {"ok": 1, "users": [{"screen_name": "test"}]}
        mock_client._http.request.return_value = _mock_response(response)

        result = mock_client.get_followers("12345", page=2)
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["uid"] == "12345"
        assert params["page"] == "2"
        assert params["relate"] == "fans"
        assert "users" in result


class TestGetFriendsTimelineAPI:
    def test_get_friends_timeline_default(self, mock_client):
        response = {"ok": 1, "statuses": [], "max_id": "0"}
        mock_client._http.request.return_value = _mock_response(response)

        result = mock_client.get_friends_timeline()
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["count"] == "20"
        assert params["max_id"] == "0"
        assert "statuses" in result


class TestGetFollowingAPI:
    def test_get_following_passes_uid(self, mock_client):
        response = {"ok": 1, "users": []}
        mock_client._http.request.return_value = _mock_response(response)

        mock_client.get_following("12345")
        params = mock_client._http.request.call_args[1].get("params", {})
        assert params["uid"] == "12345"
        assert params["page"] == "1"


class TestGetConfigAPI:
    def test_get_config_returns_data(self, mock_client):
        response = {"ok": 1, "data": {"uid": "12345", "login": True}}
        mock_client._http.request.return_value = _mock_response(response)

        result = mock_client.get_config()
        assert result["uid"] == "12345"


class TestSearchWeiboAPI:
    def test_search_weibo_uses_mobile_client(self, mock_client):
        """Verify search_weibo creates a separate mobile client."""
        search_response = {"ok": 1, "data": {"cards": []}}
        mock_mobile = MagicMock()
        mock_mobile.__enter__ = MagicMock(return_value=mock_mobile)
        mock_mobile.__exit__ = MagicMock(return_value=False)
        mock_mobile.request.return_value = _mock_response(search_response)

        with patch.object(mock_client, '_build_mobile_client', return_value=mock_mobile):
            result = mock_client.search_weibo("test")

        assert result == search_response
        mock_mobile.request.assert_called_once()
        call_args = mock_mobile.request.call_args
        assert call_args[1]["params"]["containerid"] == "100103type=1&q=test"


# ── handle_command routing tests ──────────────────────────────────────


def test_non_tty_defaults_to_plain_not_yaml(monkeypatch):
    """非 TTY stdout 应走 plain render，不再自动 YAML。"""
    from weibo_cli.auth import Credential
    monkeypatch.setattr("weibo_cli.commands._common.get_credential", lambda: Credential(cookies={"SUB": "x"}))
    from weibo_cli.commands import _common

    class _Stub:
        def __init__(self, cred): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_hot_search(self): return {"realtime": [{"word": "测试热搜", "num": 12345, "icon_desc": "热"}]}

    monkeypatch.setattr(_common, "WeiboClient", _Stub)
    runner = CliRunner()
    result = runner.invoke(cli, ["hot"])
    assert result.exit_code == 0
    assert "realtime:" not in result.output
    assert "测试热搜" in result.output

"""Retry rules of the shared HTTP helper, tested with a fake urlopen."""

import io
import socket
import urllib.error

import pytest

import http_client


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(monkeypatch, outcomes):
    """Each call pops the next outcome: bytes -> response body, Exception -> raised."""
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.get_method())
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp(outcome)

    monkeypatch.setattr(http_client.urllib.request, "urlopen", urlopen)
    return calls


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"{}"))


def test_retries_503_then_succeeds(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [_http_error(503), _http_error(503), b'{"ok": 1}'])
    assert http_client.get_json("http://x", retries=2) == {"ok": 1}
    assert len(calls) == 3


def test_gives_up_after_retries(monkeypatch):
    _fake_urlopen(monkeypatch, [_http_error(503)] * 3)
    with pytest.raises(urllib.error.HTTPError):
        http_client.get_json("http://x", retries=2)


def test_4xx_is_not_retried(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [_http_error(400), b"{}"])
    with pytest.raises(urllib.error.HTTPError):
        http_client.get_json("http://x", retries=3)
    assert len(calls) == 1


def test_connection_refused_is_retried(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [urllib.error.URLError(ConnectionRefusedError()), b"[1]"])
    assert http_client.get_json("http://x") == [1]
    assert len(calls) == 2


def test_timeout_not_retried_for_non_idempotent_requests(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [socket.timeout("timed out"), b""])
    with pytest.raises(socket.timeout):
        http_client.request_raw("PUT", "http://x", retries=3)
    assert calls == ["PUT"]


def test_get_retries_timeouts_by_default(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [urllib.error.URLError(socket.timeout()), b"{}"])
    assert http_client.get_json("http://x") == {}
    assert len(calls) == 2


def test_empty_body_is_none(monkeypatch):
    _fake_urlopen(monkeypatch, [b"  "])
    assert http_client.request_json("DELETE", "http://x") is None

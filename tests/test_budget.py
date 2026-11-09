"""Gemini monthly budget: atomic reservations, settlement, and key handling."""

import io
import socket
import threading
import time
import urllib.error

import pytest

import db
import gemini_curator as gc

OK_RESPONSE = {
    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 20},
    "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
}


@pytest.fixture
def gemini(monkeypatch):
    """Gemini configured with a fake key; returns the list of captured requests."""
    with db.session() as conn:
        conn.execute("DELETE FROM gemini_usage")
        conn.commit()
    monkeypatch.setitem(gc.CONFIG, "GEMINI_API_KEY", "secret-key")
    monkeypatch.setitem(gc.CONFIG, "GEMINI_MONTHLY_BUDGET_USD", "1.50")
    monkeypatch.setitem(gc.CONFIG, "GEMINI_PRICE_IN_PER_1M", "0.50")
    monkeypatch.setitem(gc.CONFIG, "GEMINI_PRICE_OUT_PER_1M", "3.00")
    monkeypatch.setitem(gc.CONFIG, "GEMINI_MAX_OUTPUT_TOKENS", "4096")
    class Calls(list):
        """Captured requests; `.fake.outcome` sets what the next calls return or raise."""

    calls = Calls()

    def fake_request_json(method, url, headers=None, body=None, **kw):
        calls.append({"url": url, "headers": headers, "body": body})
        outcome = fake_request_json.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    fake_request_json.outcome = OK_RESPONSE
    monkeypatch.setattr(gc, "request_json", fake_request_json)
    calls.fake = fake_request_json
    return calls


def _rows():
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT status, cost_usd, prompt_tokens FROM gemini_usage")]


def test_successful_call_records_actual_cost(gemini):
    assert gc.call_gemini_raw_json("hello") == {"ok": True}
    [row] = _rows()
    assert row["status"] == "final" and row["prompt_tokens"] == 1000
    assert row["cost_usd"] == pytest.approx(1000 * 0.5e-6 + 120 * 3e-6)


def test_key_sent_as_header_not_in_url(gemini):
    gc.call_gemini_raw_json("hello")
    assert "secret-key" not in gemini[0]["url"]
    assert gemini[0]["headers"] == {"x-goog-api-key": "secret-key"}
    assert gemini[0]["body"]["generationConfig"]["maxOutputTokens"] == 4096


def test_call_blocked_when_worst_case_would_exceed_budget(gemini, monkeypatch):
    monkeypatch.setitem(gc.CONFIG, "GEMINI_MONTHLY_BUDGET_USD", "0.01")  # < one worst-case call (~$0.012)
    assert gc.call_gemini_raw_json("hello") is None
    assert gemini == [] and _rows() == []


def test_spend_this_month_counts_towards_cap(gemini):
    with db.session() as conn:
        conn.execute("INSERT INTO gemini_usage (prompt_tokens, completion_tokens, cost_usd, purpose) VALUES (0, 0, 1.495, 'old')")
        conn.commit()
    assert gc.call_gemini_raw_json("hello") is None
    assert gemini == []


def test_last_month_spend_does_not_count(gemini):
    with db.session() as conn:
        conn.execute(
            "INSERT INTO gemini_usage (timestamp, prompt_tokens, completion_tokens, cost_usd, purpose) "
            "VALUES (datetime('now', 'start of month', '-1 day'), 0, 0, 5.0, 'old')"
        )
        conn.commit()
    assert gc.call_gemini_raw_json("hello") == {"ok": True}


def test_http_error_releases_reservation(gemini):
    gemini.fake.outcome = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b""))
    assert gc.call_gemini_raw_json("hello") is None
    assert _rows() == [{"status": "failed", "cost_usd": 0.0, "prompt_tokens": 0}]


def test_timeout_keeps_reservation(gemini):
    gemini.fake.outcome = socket.timeout("timed out")
    gc.call_gemini_raw_json("hello")
    [row] = _rows()
    assert row["status"] == "unknown" and row["cost_usd"] == pytest.approx(gc.estimate_max_cost_usd("hello"))


def test_concurrent_calls_cannot_overshoot_budget(gemini, monkeypatch):
    one_call = gc.estimate_max_cost_usd("hello")
    monkeypatch.setitem(gc.CONFIG, "GEMINI_MONTHLY_BUDGET_USD", str(one_call * 2.5))  # room for 2 reservations

    def slow():
        time.sleep(0.2)  # keep reservations outstanding while the other threads check
        return OK_RESPONSE
    gemini.fake.outcome = slow

    threads = [threading.Thread(target=gc.call_gemini_raw_json, args=("hello",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(gemini) == 2

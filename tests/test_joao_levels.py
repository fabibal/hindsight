"""Offline tests for joao_levels. No network, no real data/ directory."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from google.genai import errors as genai_errors
from google.genai import types as genai_types

import joao_levels as jl
import monitor

TEXT = "$57,000 and below still concerns me. ETF avg cost $76,909 and 1.29M BTC."


def lv(**kw):
    base = dict(asset="BTC", kind="support", direction="none", low=57000, high=None,
                source="text", evidence="$57,000 and below still concerns me", note="Szint.")
    base.update(kw)
    return base


def test_verify_text_level_needs_quote_and_number():
    assert jl.verify_text_level(lv(), TEXT)
    assert not jl.verify_text_level(lv(evidence="not in the post"), TEXT)
    assert not jl.verify_text_level(lv(low=58000), TEXT)             # number not in text
    assert jl.verify_text_level(lv(low=1290000, evidence="1.29M BTC"), TEXT)   # K/M expansion


def test_verify_handles_negative_and_spelled_zero():
    nupl = "NUPL: -0.91 today, MVRV 0.52"
    assert jl.verify_text_level(lv(asset="DOGE", low=-0.91, evidence="NUPL: -0.91", kind="onchain_threshold"), nupl)
    assert not jl.verify_text_level(lv(low=-0.52, evidence="MVRV 0.52"), nupl)
    zero = "Impulse is now testing the zero line."
    assert jl.verify_text_level(lv(asset="TOTAL", low=0, evidence="testing the zero line"), zero)


def _resp(levels):
    return SimpleNamespace(text=json.dumps({"levels": levels}),
                           usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5,
                                                          thoughts_token_count=0, tool_use_prompt_token_count=0))


def _run(client, media=()):
    return jl.extract_levels(client, TEXT, "2026-08-08", list(media), model="m", thinking=None,
                             tally=monitor.GeminiTally(), types=genai_types, errors=genai_errors)


def test_extract_keeps_verified_and_retries_then_drops_bad(monkeypatch):
    client = Mock()
    client.models.generate_content.return_value = _resp([lv(), lv(low=99999, evidence="made up")])
    good, bad, i, o = _run(client)
    assert [x["low"] for x in good] == [57000] and len(bad) == 1
    assert client.models.generate_content.call_count == jl.ATTEMPTS
    assert (i, o) == (20, 10)


def test_chart_level_without_images_is_rejected():
    client = Mock()
    client.models.generate_content.return_value = _resp(
        [lv(source="chart", evidence="Image 1, Key Levels panel")])
    good, bad, *_ = _run(client)
    assert good == [] and len(bad) == 1


def test_chart_level_with_image_is_kept(monkeypatch):
    monkeypatch.setattr(monitor, "_image_block",
                        lambda url, size="small": genai_types.Part.from_bytes(data=b"x", mime_type="image/jpeg"))
    client = Mock()
    client.models.generate_content.return_value = _resp(
        [lv(source="chart", low=78011, evidence="Image 1, Key Levels panel")])
    good, bad, *_ = _run(client, media=["https://pbs.twimg.com/media/a.jpg"])
    assert len(good) == 1 and bad == []


def test_api_error_returns_none():
    client = Mock()
    client.models.generate_content.side_effect = genai_errors.APIError(
        500, {"error": {"message": "boom"}})
    assert _run(client) is None


def test_flatten_orders_and_carries_post_fields():
    recs = [{"tweet_id": 2, "created_at": "2026-02-01", "url": "u2", "levels": [lv()]},
            {"tweet_id": 1, "created_at": "2026-01-01", "url": "u1", "levels": [lv(low=1)]},
            {"tweet_id": 3, "created_at": "2026-03-01"}]
    rows = jl.flatten(recs)
    assert [r["tweet_id"] for r in rows] == ["1", "2"] and rows[0]["url"] == "u1"


def test_scope_is_own_positions_and_btc_only():
    assert jl.in_scope(lv())                                              # BTC support
    assert jl.in_scope(lv(asset="TAKE", kind="own_entry", direction="short"))
    assert not jl.in_scope(lv(asset="ETH"))                               # other asset's level
    assert not jl.in_scope(lv(kind="price_ref"))
    assert not jl.in_scope(lv(kind="positioning_avg_entry"))
    recs = [{"tweet_id": 1, "created_at": "2026-01-01", "levels": [lv(), lv(asset="ETH")]}]
    assert [r["asset"] for r in jl.flatten(recs)] == ["BTC"]


def test_extract_drops_out_of_scope_levels_silently():
    client = Mock()
    client.models.generate_content.return_value = _resp([lv(), lv(asset="ETH", low=57000)])
    good, bad, *_ = _run(client)
    assert [x["asset"] for x in good] == ["BTC"] and bad == []
    assert client.models.generate_content.call_count == 1

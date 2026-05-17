"""Tests for the universal Stage 0 pre-filter (`src.prefilter`).

The two gates must be:
  - Language-agnostic (no hardcoded Arabic / English / Russian tokens)
  - Default-to-keep when config is empty
  - Driven entirely by profile-supplied `symbol_aliases` /
    `other_instruments` lists
"""
from __future__ import annotations

from src.prefilter import looks_like_ad, should_drop_by_symbol


# --- should_drop_by_symbol ------------------------------------------------


def test_drop_when_other_instrument_only_and_no_alias():
    profile = {
        "symbol_aliases": ["gold", "XAUUSD"],
        "other_instruments": ["BTC", "BITCOIN"],
    }
    drop, reason = should_drop_by_symbol("BITCOIN SELL @ 80,700", profile)
    assert drop is True
    assert reason == "symbol-mismatch"


def test_keep_when_alias_present_even_with_other_instrument():
    profile = {
        "symbol_aliases": ["gold", "XAUUSD"],
        "other_instruments": ["BTC", "BITCOIN"],
    }
    drop, _ = should_drop_by_symbol(
        "Gold strong vs bitcoin weakness — buy XAUUSD",
        profile,
    )
    assert drop is False


def test_keep_when_no_other_instrument_mentioned():
    profile = {
        "symbol_aliases": ["gold"],
        "other_instruments": ["BTC", "EUR"],
    }
    drop, _ = should_drop_by_symbol("Generic market commentary", profile)
    assert drop is False


def test_empty_other_instruments_is_no_op():
    profile = {"symbol_aliases": ["gold"], "other_instruments": []}
    drop, _ = should_drop_by_symbol("BITCOIN SELL", profile)
    assert drop is False


def test_missing_profile_keys_is_safe_no_op():
    drop, _ = should_drop_by_symbol("anything at all", {})
    assert drop is False


def test_case_insensitive_alias_match():
    profile = {"symbol_aliases": ["XAUUSD"], "other_instruments": ["btc"]}
    drop, _ = should_drop_by_symbol("buy xauusd @ 4500", profile)
    assert drop is False


def test_arabic_aliases_drive_drop_no_hardcoded_keywords():
    """Channel-specific vocab in profile drives the decision; the function
    has no Arabic/English/Russian baked in."""
    profile = {
        "symbol_aliases": ["ذهب", "gold"],
        "other_instruments": ["نفط", "بيتكوين"],
    }
    drop, _ = should_drop_by_symbol("نفط - فريم 4 ساعات", profile)
    assert drop is True
    drop2, _ = should_drop_by_symbol("الذهب وصل 4720", profile)
    assert drop2 is False


# --- looks_like_ad --------------------------------------------------------


def test_ad_three_urls_and_length():
    text = (
        "🔥 Cashback offer — sign up via the link below: "
        "https://example.com/promo "
        "Contact: https://t.me/somesupport "
        "More: https://whatsapp.com/channel/0029Vb6xuCYJ "
        + ("Long padding text to push us past 200 chars. " * 5)
    )
    assert looks_like_ad(text) is True


def test_ad_long_with_two_urls_and_percent_currency():
    text = (
        "Bonus 30% on your first deposit, up to $2000 — "
        "claim now https://example.com/bonus and "
        "join our VIP channel https://t.me/something "
        + ("Filler text. " * 100)
    )
    assert looks_like_ad(text) is True


def test_not_ad_short_signal_with_one_url():
    text = (
        "https://www.tradingview.com/x/abc123\n"
        "XAUUSD buy now 4593 SL 4590 TP 4613"
    )
    assert looks_like_ad(text) is False


def test_not_ad_long_analysis_with_no_urls():
    text = (
        "Gold analysis: I see support at 4500, resistance at 4700. "
        + ("Detailed paragraph of market analysis. " * 30)
    )
    assert looks_like_ad(text) is False


def test_not_ad_empty():
    assert looks_like_ad("") is False

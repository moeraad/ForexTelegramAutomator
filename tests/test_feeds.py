"""Tests for the COT, ETF flows, and news-scan feed modules.

Pure-function parsers are tested directly with synthetic fixtures.
The async fetch wrappers are not exercised against the real network in
the hermetic suite — they're thin (urllib + asyncio.to_thread) and the
parsers are where the logic lives.
"""
from __future__ import annotations

from src.feeds.cot import parse_cot_rows, _percentile_rank
from src.feeds.etf_flows import compute_flow_features
from src.feeds.news_scan import parse_articles, _saturating_score


# ---- COT parser ------------------------------------------------------

def _cot_row(date: str, longs: int, shorts: int) -> dict:
    """Build a Socrata-shaped row. Real responses have ~50 columns;
    parser only reads three, so we keep fixtures minimal."""
    return {
        "report_date_as_yyyy_mm_dd": date,
        "m_money_positions_long_all": str(longs),
        "m_money_positions_short_all": str(shorts),
    }


def test_parse_cot_extracts_net_position():
    rows = [_cot_row("2026-05-13", 220_000, 80_000)]
    features = parse_cot_rows(rows)
    assert features is not None
    assert features["cot_managed_money"] == 140_000
    assert features["cot_managed_money_long"] == 220_000
    assert features["cot_managed_money_short"] == 80_000
    assert features["cot_report_date"] == "2026-05-13"


def test_parse_cot_returns_none_when_columns_missing():
    """The CFTC column names occasionally drift across report types
    (futures-only vs combined). A row that doesn't have managed-money
    longs/shorts means we hit the wrong dataset; bail."""
    rows = [{"report_date_as_yyyy_mm_dd": "2026-05-13"}]
    assert parse_cot_rows(rows) is None


def test_parse_cot_handles_float_string_values():
    """Socrata's JSON renders all numbers as strings, sometimes as
    floats like '220000.00'. Parser must accept those."""
    rows = [{
        "report_date_as_yyyy_mm_dd": "2026-05-13",
        "m_money_positions_long_all": "220000.00",
        "m_money_positions_short_all": "80000.00",
    }]
    features = parse_cot_rows(rows)
    assert features is not None
    assert features["cot_managed_money"] == 140_000


def test_parse_cot_percentile_extreme_long():
    """Latest net is the highest in the 52-week window -> percentile
    near 100. This is the swing/position trader's 'stretched' signal."""
    rows = [_cot_row("2026-05-13", 250_000, 50_000)]  # net 200k, highest
    rows += [_cot_row(f"2026-{m:02d}-01", 100_000, 100_000) for m in range(1, 12)]
    features = parse_cot_rows(rows)
    assert features is not None
    assert features["cot_extremes_percentile"] >= 90.0


def test_parse_cot_percentile_extreme_short():
    rows = [_cot_row("2026-05-13", 50_000, 250_000)]  # net -200k, lowest
    rows += [_cot_row(f"2026-{m:02d}-01", 100_000, 100_000) for m in range(1, 12)]
    features = parse_cot_rows(rows)
    assert features is not None
    assert features["cot_extremes_percentile"] <= 10.0


def test_parse_cot_percentile_returns_none_with_too_few_rows():
    """At n<4 the percentile rank is statistical noise — better None
    than a misleading 50.0."""
    rows = [_cot_row("2026-05-13", 100_000, 100_000)]
    features = parse_cot_rows(rows)
    assert features is not None
    assert features["cot_extremes_percentile"] is None


def test_parse_cot_skips_malformed_rows_in_series():
    """One bad row mid-series must not poison the percentile — it's
    skipped, the rest of the series carries on."""
    rows = [
        _cot_row("2026-05-13", 200_000, 50_000),
        {"m_money_positions_long_all": "garbage"},  # malformed
        _cot_row("2026-04-29", 100_000, 100_000),
        _cot_row("2026-04-22", 100_000, 100_000),
        _cot_row("2026-04-15", 100_000, 100_000),
    ]
    features = parse_cot_rows(rows)
    assert features is not None
    # 4 valid rows -> percentile computable.
    assert features["cot_window_size"] == 4
    assert features["cot_extremes_percentile"] is not None


def test_parse_cot_empty_rows_returns_none():
    assert parse_cot_rows([]) is None


def test_percentile_rank_below_threshold():
    assert _percentile_rank([1, 2, 3], 0) is None  # n<4


# ---- ETF flows ------------------------------------------------------

def _gld_bar(date: str, o: float, c: float, vol: float) -> dict:
    return {"date": date, "open": o, "close": c, "volume": vol}


def test_compute_flow_features_signs_bullish_day_positive():
    """Up day on heavy volume -> positive signed flow."""
    hist = [_gld_bar("2026-05-19", 180.0, 182.0, 8_000_000)]
    features = compute_flow_features(hist)
    assert features is not None
    assert features["etf_flows_gld"] > 0


def test_compute_flow_features_signs_bearish_day_negative():
    hist = [_gld_bar("2026-05-19", 182.0, 180.0, 8_000_000)]
    features = compute_flow_features(hist)
    assert features is not None
    assert features["etf_flows_gld"] < 0


def test_compute_flow_features_zero_volume_skipped():
    """A bar with zero volume is a halt; treat it as missing data
    rather than a zero-flow day."""
    hist = [
        _gld_bar("2026-05-18", 180.0, 182.0, 0),
        _gld_bar("2026-05-19", 180.0, 182.0, 8_000_000),
    ]
    features = compute_flow_features(hist)
    assert features is not None
    # Only the second bar contributes; trend == latest.
    assert features["etf_flows_gld"] == features["etf_flows_30d_trend"]
    assert features["etf_flows_window_days"] == 1


def test_compute_flow_features_trend_sums_signed_window():
    """30d trend is the signed sum, NOT the absolute sum. Three bullish
    days and one big bearish day can leave the trend slightly positive
    or net negative depending on magnitudes."""
    hist = [
        _gld_bar("2026-05-15", 180.0, 181.0, 1_000_000),
        _gld_bar("2026-05-16", 181.0, 182.0, 1_000_000),
        _gld_bar("2026-05-17", 182.0, 183.0, 1_000_000),
        _gld_bar("2026-05-18", 183.0, 180.0, 5_000_000),  # big down
    ]
    features = compute_flow_features(hist)
    assert features is not None
    # Three up days vs one heavy down day -> net negative trend.
    assert features["etf_flows_30d_trend"] < 0


def test_compute_flow_features_empty_returns_none():
    assert compute_flow_features([]) is None


def test_compute_flow_features_single_bar_yields_latest_equal_to_trend():
    """One bar is enough to publish today's flow — the trend window
    just collapses to that single value. Better than None for fresh
    deployments."""
    features = compute_flow_features([_gld_bar("2026-05-19", 180.0, 182.0, 1_000_000)])
    assert features is not None
    assert features["etf_flows_gld"] == features["etf_flows_30d_trend"]
    assert features["etf_flows_window_days"] == 1


# ---- News scan ------------------------------------------------------

def test_parse_articles_calm_quiet_returns_zero():
    """No articles = no risk. The feature is the score itself, not the
    absence of the row."""
    features = parse_articles({"articles": []})
    assert features is not None
    assert features["geopolitical_index"] == 0.0
    assert features["geopolitical_article_count"] == 0


def test_parse_articles_high_count_negative_tone_high_score():
    """Saturation count + crisis-toned coverage -> score near 1.0."""
    articles = [
        {"title": f"crisis story {i}", "tone": "-5.5", "url": "x", "domain": "reuters.com"}
        for i in range(50)
    ]
    features = parse_articles({"articles": articles})
    assert features is not None
    assert features["geopolitical_index"] >= 0.8


def test_parse_articles_high_count_neutral_tone_moderate_score():
    """Lots of articles but neutral tone (procedural coverage) -> mid
    score, not maxed out. Pure count saturation would be wrong."""
    articles = [
        {"title": f"procedural story {i}", "tone": "0.0", "url": "x", "domain": "x"}
        for i in range(50)
    ]
    features = parse_articles({"articles": articles})
    assert features is not None
    assert 0.5 < features["geopolitical_index"] < 0.9


def test_parse_articles_captures_top_headlines():
    """The dashboard rendering needs the top 5 headlines for context."""
    articles = [
        {"title": f"headline {i}", "tone": "-3.0", "url": f"u{i}", "domain": "ap.org"}
        for i in range(10)
    ]
    features = parse_articles({"articles": articles})
    assert features is not None
    assert len(features["geopolitical_recent_headlines"]) == 5


def test_parse_articles_skips_titleless_entries():
    """GDELT occasionally returns articles with empty titles. Don't
    let them eat headline slots."""
    articles = [
        {"title": "", "tone": "-3.0", "url": "u", "domain": "x"},
        {"title": "real headline", "tone": "-3.0", "url": "u", "domain": "x"},
    ]
    features = parse_articles({"articles": articles})
    assert features is not None
    assert len(features["geopolitical_recent_headlines"]) == 1
    assert features["geopolitical_recent_headlines"][0]["title"] == "real headline"


def test_parse_articles_handles_malformed_payload():
    """Missing `articles` key is treated as "no articles" — that's
    legal GDELT behavior on quiet windows. Non-dict payloads return
    None so the loop can log + retry."""
    features = parse_articles({"not_articles": []})
    assert features is not None
    assert features["geopolitical_index"] == 0.0
    assert features["geopolitical_article_count"] == 0
    assert features["geopolitical_recent_headlines"] == []
    # Non-dict is a real parse failure (response didn't return JSON
    # object) — loop should retry.
    assert parse_articles("not a dict") is None  # type: ignore[arg-type]


def test_saturating_score_zero_articles_zero():
    assert _saturating_score(0, -5.0) == 0.0


def test_saturating_score_monotonic_in_count():
    """More articles at the same tone => higher score, up to saturation."""
    low = _saturating_score(5, -3.0)
    high = _saturating_score(40, -3.0)
    assert high > low

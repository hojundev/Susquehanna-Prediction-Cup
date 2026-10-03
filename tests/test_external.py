"""External feeds, matching, mapping file and the external fair-value model."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest

from supermarket_bot.config import Settings
from supermarket_bot.external import KalshiFeed, PolymarketFeed, Quote
from supermarket_bot.fair_value import (
    CompositeFairValueModel,
    ExternalFairValueModel,
    ExternalParams,
    FairValue,
)
from supermarket_bot.mapping import Mapping, SourceRef, load_mappings, save_mappings, similarity
from supermarket_bot.models import Market, MarketOrderbook, OrderbookExchange, OrderbookLevel
from supermarket_bot.signals import SignalEngine


# -- feeds ---------------------------------------------------------------
def test_kalshi_quotes_parse_and_filter() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/markets")
        assert req.url.params["tickers"] == "A,B,C"
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "ticker": "A",
                        "status": "active",
                        "yes_bid_dollars": "0.9150",
                        "yes_ask_dollars": "0.9160",
                        "volume_24h_fp": "264556.71",
                        "title": "House D",
                    },
                    {
                        "ticker": "B",
                        "status": "active",
                        "yes_bid_dollars": "0.0000",
                        "yes_ask_dollars": "0.0200",
                    },  # no bid -> unusable
                    {
                        "ticker": "C",
                        "status": "closed",
                        "yes_bid_dollars": "0.5",
                        "yes_ask_dollars": "0.6",
                    },
                ]
            },
        )

    feed = KalshiFeed(base_url="https://k.test", transport=httpx.MockTransport(handler))
    quotes = feed.quotes(["C", "A", "B", "A"])
    assert list(quotes) == ["A"]
    assert quotes["A"].mid == pytest.approx(0.9155)
    assert quotes["A"].volume_24h == pytest.approx(264556.71)


def test_polymarket_quotes_use_repeated_id_params() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get_list("id") == ["562802", "562803"]
        return httpx.Response(
            200,
            json=[
                {
                    "id": "562802",
                    "active": True,
                    "closed": False,
                    "bestBid": 0.92,
                    "bestAsk": 0.93,
                    "volume24hr": 29849.4,
                    "question": "Dems House",
                },
                {"id": "562803", "active": True, "closed": True, "bestBid": 0.07, "bestAsk": 0.08},
            ],
        )

    feed = PolymarketFeed(base_url="https://p.test", transport=httpx.MockTransport(handler))
    quotes = feed.quotes(["562803", "562802"])
    assert list(quotes) == ["562802"]
    assert quotes["562802"].spread == pytest.approx(0.01)


def test_quote_inversion() -> None:
    q = Quote("kalshi", "A", 0.30, 0.34).inverted()
    assert (q.bid, q.ask) == pytest.approx((0.66, 0.70))


# -- matching ------------------------------------------------------------
def test_similarity_prefers_same_race_and_blocks_wrong_state_or_year() -> None:
    a = "Which party will win the Texas Senate race? Republican Party"
    assert similarity(a, "Texas Senate election 2026 Republican") > similarity(
        a, "Texas House district 23 Democratic"
    )
    assert similarity(a, "Ohio Senate Republican") == 0.0
    assert similarity("Senate 2026 winner Democrats", "Senate 2028 winner Democrats") == 0.0
    assert similarity("Virginia governor", "West Virginia governor") == 0.0


def test_state_race_beats_national_and_state_legislature() -> None:
    from supermarket_bot.external import Candidate
    from supermarket_bot.mapping import rank_candidates

    pool = [
        Candidate(
            "kalshi",
            "CONTROLS-2026-R",
            "Which party will win the U.S. Senate? Republican Party",
            "national",
        ),
        Candidate(
            "kalshi",
            "KXSTATELEG-TXSENA26-R",
            "Texas State Senate winner? Republican party",
            "state leg",
        ),
        Candidate(
            "kalshi",
            "SENATETX-26-R",
            "Texas Senate winner? Will Republican win Texas Senate Republican",
            "race",
        ),
    ]
    [(_, best), *_] = rank_candidates(
        "Who will win the Texas Senate race in 2026? Republican Party", pool
    )
    assert best.ref == "SENATETX-26-R"


def test_target_year_prefers_right_cycle() -> None:
    from supermarket_bot.external import Candidate
    from supermarket_bot.mapping import rank_candidates

    pool = [
        Candidate("kalshi", "GOVPARTYVT-28-D", "Vermont Governor Winner? (2028) Democratic", "x"),
        Candidate("kalshi", "GOVPARTYVT-26-D", "Vermont Governor winner? Amanda Janoo 2026", "y"),
    ]
    q = "Will the Democratic Party win the Vermont Governor?"
    [(_, best), *_] = rank_candidates(q, pool, target_year="2026")
    assert best.ref == "GOVPARTYVT-26-D"


def test_house_district_is_a_hard_gate() -> None:
    from supermarket_bot.external import Candidate
    from supermarket_bot.mapping import rank_candidates, similarity

    q = "Will the Democratic Party win the AL-02 House race?"
    # Wrong state (MO-02) and wrong district (FL-10 / AL-07) must score zero,
    # even though the ref text otherwise overlaps heavily.
    assert similarity(q, "House race", b_ref="KXHOUSERACE-MO02-26-D") == 0.0
    assert similarity(q, "House race", b_ref="KXHOUSERACE-AL07-26-D") == 0.0
    assert similarity(q, "Alabama 02 House winner Democratic", b_ref="KXHOUSERACE-AL02-26-D") > 0.0

    pool = [
        Candidate("kalshi", "KXHOUSERACE-MO02-26-D", "House race Democratic", "x"),
        Candidate("kalshi", "KXHOUSERACE-AL02-26-D", "Alabama 02 House Democratic", "y"),
    ]
    [(_, best), *_] = rank_candidates(q, pool, target_year="2026")
    assert best.ref == "KXHOUSERACE-AL02-26-D"


def test_combo_contract_rejected_for_single_race() -> None:
    from supermarket_bot.mapping import similarity

    q = "Will the Democratic Party win the Michigan Senate?"
    assert similarity(q, "Five state senate combo", b_ref="KXFIVESTATESEN-26NOV03-FTFFF") == 0.0
    assert similarity(q, "Michigan Senate winner Democratic", b_ref="SENATEMI-26-D") > 0.0


def test_margin_bracket_penalised_against_plain_winner() -> None:
    plain = "Will the Republican Party win the Tennessee Governor?"
    bracket = "Republican wins 2026 Tennessee gubernatorial election by 0%-5%"
    whole = "Republican wins 2026 Tennessee gubernatorial election"
    assert similarity(plain, whole, target_year="2026") > similarity(
        plain, bracket, target_year="2026"
    )


def test_mapping_roundtrip_keeps_extras(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    m = Mapping(
        "12",
        "House — D",
        True,
        "D",
        {"kalshi": SourceRef("CONTROLH-2026-D")},
        {"suggestions": {"kalshi": ["x"]}},
    )
    save_mappings(p, {"12": m})
    loaded = load_mappings(p)["12"]
    assert loaded.approved and loaded.lean == "D"
    assert loaded.refs == {"kalshi": SourceRef("CONTROLH-2026-D", False)}
    assert loaded.extra["suggestions"] == {"kalshi": ["x"]}
    assert "_help" in json.loads(p.read_text())


def test_lean_from_title_and_top_score() -> None:
    from supermarket_bot.mapping import Mapping, lean_from_title, top_suggestion_score

    assert lean_from_title("Will the Republican Party win Ohio?") == "R"
    assert lean_from_title("Will the Democratic Party win Ohio?") == "D"
    assert lean_from_title("Who controls the chamber?") is None
    m = Mapping("1", extra={"suggestions": {"kalshi": ["0.71  REF  Title  [0.9/0.9]", "0.52  X"]}})
    assert top_suggestion_score(m, "kalshi") == 0.71
    assert top_suggestion_score(m, "polymarket") == 0.0


def test_unapproved_or_string_true_is_not_approved(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"1": {"approved": "true", "kalshi": {"ref": "A"}}}))
    assert not load_mappings(p)["1"].approved


# -- external fair value -------------------------------------------------
class FakeFeed:
    def __init__(self, source: str, quotes: dict[str, Quote] | Exception) -> None:
        self.source = source
        self._q = quotes
        self.asked: set[str] = set()

    def quotes(self, refs: Iterable[str]) -> dict[str, Quote]:
        self.asked = set(refs)
        if isinstance(self._q, Exception):
            raise self._q
        return self._q


def write_maps(tmp_path: Path, entries: dict[str, dict[str, object]]) -> Path:
    p = tmp_path / "mappings.json"
    p.write_text(json.dumps(entries))
    return p


def model(
    tmp_path: Path, entries: dict[str, dict[str, object]], kq: object, pq: object, **params: object
) -> ExternalFairValueModel:
    feeds = [FakeFeed("kalshi", kq), FakeFeed("polymarket", pq)]  # type: ignore[arg-type]
    m = ExternalFairValueModel(
        write_maps(tmp_path, entries),
        feeds,  # type: ignore[arg-type]
        ExternalParams(**params),
    )  # type: ignore[arg-type]
    m.reload()
    return m


BOTH = {"approved": True, "lean": "D", "kalshi": {"ref": "K"}, "polymarket": {"ref": "P"}}


def test_two_agreeing_sources_volume_weighted(tmp_path: Path) -> None:
    m = model(
        tmp_path,
        {"1": BOTH},
        {"K": Quote("kalshi", "K", 0.60, 0.62, 300)},
        {"P": Quote("polymarket", "P", 0.62, 0.64, 100)},
    )
    fv = m.estimate("1")
    assert fv is not None
    assert fv.probability == pytest.approx(0.75 * 0.61 + 0.25 * 0.63)
    assert fv.lean == "D"
    assert fv.confidence == pytest.approx(0.8 * (1 - 0.02 / 0.16), abs=1e-3)


def test_disagreeing_sources_give_no_value(tmp_path: Path) -> None:
    m = model(
        tmp_path,
        {"1": BOTH},
        {"K": Quote("kalshi", "K", 0.50, 0.52)},
        {"P": Quote("polymarket", "P", 0.60, 0.62)},
    )
    assert m.estimate("1") is None
    assert "disagree" in m.views["1"].note


def test_wide_spread_ignored_and_single_source_lower_confidence(tmp_path: Path) -> None:
    m = model(
        tmp_path,
        {"1": BOTH},
        {"K": Quote("kalshi", "K", 0.40, 0.60)},  # 0.20 spread: ignored
        {"P": Quote("polymarket", "P", 0.55, 0.57)},
    )
    fv = m.estimate("1")
    assert fv is not None and fv.probability == pytest.approx(0.56)
    assert fv.confidence < 0.5


def test_require_both_blocks_single_source(tmp_path: Path) -> None:
    m = model(
        tmp_path, {"1": BOTH}, {}, {"P": Quote("polymarket", "P", 0.55, 0.57)}, require_both=True
    )
    assert m.estimate("1") is None


def test_invert_and_unapproved_and_dead_feed(tmp_path: Path) -> None:
    entries = {
        "1": {"approved": True, "kalshi": {"ref": "K", "invert": True}},
        "2": {"approved": False, "kalshi": {"ref": "K2"}},
    }
    m = model(tmp_path, entries, {"K": Quote("kalshi", "K", 0.90, 0.92)}, RuntimeError("down"))
    fv = m.estimate("1")
    assert fv is not None and fv.probability == pytest.approx(0.09)
    assert m.estimate("2") is None
    assert m._feeds[0].asked == {"K"}  # type: ignore[attr-defined]  # unapproved never fetched


def test_composite_prefers_first_model() -> None:
    class One:
        def __init__(self, p: float | None) -> None:
            self.p = p

        def estimate(self, exchange_id: str) -> FairValue | None:
            return FairValue(self.p) if self.p is not None else None

        def reload(self) -> None:
            return None

    assert CompositeFairValueModel([One(None), One(0.4)]).estimate("x") == FairValue(0.4)
    assert CompositeFairValueModel([One(0.7), One(0.4)]).estimate("x") == FairValue(0.7)


# -- max edge guard ------------------------------------------------------
def test_max_edge_blocks_suspicious_gap() -> None:
    s = Settings(api_key="k", _env_file=None, max_edge=0.25)  # type: ignore[call-arg]

    class V:
        def estimate(self, exchange_id: str) -> FairValue | None:
            return FairValue(0.80, 1.0)

        def reload(self) -> None:
            return None

    mk = Market(id="M", title="t", status="open", exchanges=[{"id": "E"}])
    far = MarketOrderbook(
        exchanges=[
            OrderbookExchange(exchangeId="E", asks=[OrderbookLevel(price=0.40, quantity=100)])
        ]
    )
    near = MarketOrderbook(
        exchanges=[
            OrderbookExchange(exchangeId="E", asks=[OrderbookLevel(price=0.65, quantity=100)])
        ]
    )
    eng = SignalEngine(s, V())
    assert eng.evaluate(mk, far, {}) == []
    assert len(eng.evaluate(mk, near, {})) == 1


# -- office, ticker-state and ticker-year gates ---------------------------
def test_office_must_match() -> None:
    q = "Will the Democratic Party win the South Dakota Senate?"
    assert (
        similarity(q, "South Dakota Attorney General winner? Democratic", b_ref="KXATTYGENSD-26-D")
        == 0.0
    )
    assert similarity(q, "South Dakota Senate winner? Democratic", b_ref="SENATESD-26-D") > 0.0
    gov = "Will the Democratic Party win the Vermont Governor?"
    assert similarity(gov, "2026 Vermont Lieutenant Governor election Democratic") == 0.0
    tx = "Will the Democratic Party win the Texas Senate?"
    assert (
        similarity(tx, "Texas House of Representatives winner Democratic", b_ref="KXTXHOUSE-26-D")
        == 0.0
    )


def test_ticker_state_must_match_query_state() -> None:
    q = "Will the Republican Party win the Kentucky Senate?"
    # Title alone wouldn't catch this: the state is only in the ticker.
    assert similarity(q, "Senate winner? Republican party", b_ref="SENATELA-26-R") == 0.0
    assert similarity(q, "Kentucky Senate winner? Republican party", b_ref="SENATEKY-26-R") > 0.0


def test_two_digit_ticker_year_is_penalised() -> None:
    q = "Will the Democratic Party win the New Hampshire Governor?"
    right = similarity(
        q, "New Hampshire Governor winner? Democratic", "2026", b_ref="GOVPARTYNH-26-D"
    )
    wrong = similarity(
        q, "New Hampshire Governor winner? Democratic", "2026", b_ref="GOVPARTYNH-28-D"
    )
    assert right > 0.65 > wrong


def test_rejected_roundtrip_and_never_approved(tmp_path: Path) -> None:
    import argparse

    from supermarket_bot.__main__ import cmd_approve, cmd_reject

    p = tmp_path / "m.json"
    entry = Mapping(
        "956",
        "Will the Republican Party win the Kentucky Senate?",
        False,
        "R",
        {"kalshi": SourceRef("SENATELA-26-R")},
        {"suggestions": {"kalshi": ["0.72  SENATELA-26-R  x"]}},
    )
    save_mappings(p, {"956": entry})

    assert cmd_reject(argparse.Namespace(ids=["956"], undo=False, out=str(p))) == 0
    m = load_mappings(p)["956"]
    assert m.rejected and not m.approved and not m.refs
    assert json.loads(p.read_text())["956"]["rejected"] is True

    # Even with a ref put back by hand and a high score, approve skips it.
    m.refs = {"kalshi": SourceRef("SENATELA-26-R")}
    save_mappings(p, {"956": m})
    cmd_approve(argparse.Namespace(out=str(p), min_score=0.65, require_lean=False))
    assert not load_mappings(p)["956"].approved

    # A rejected entry can't be approved by editing the file either.
    raw = json.loads(p.read_text())
    raw["956"]["approved"] = True
    p.write_text(json.dumps(raw))
    assert not load_mappings(p)["956"].approved

"""Unit tests for pricing, signals and risk sizing (no network)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from supermarket_bot.config import Settings
from supermarket_bot.fair_value import FairValue, NullFairValueModel
from supermarket_bot.models import (
    Action,
    Market,
    MarketOrderbook,
    OrderbookExchange,
    OrderbookLevel,
    OrderIntent,
    Position,
    Side,
    TournamentSummary,
    snap_price,
)
from supermarket_bot.risk import ExposureState, RiskManager
from supermarket_bot.signals import SignalEngine


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {"api_key": "test", "_env_file": None}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


class Views:
    def __init__(self, views: dict[str, FairValue]) -> None:
        self.views = views

    def estimate(self, exchange_id: str) -> FairValue | None:
        return self.views.get(exchange_id)

    def reload(self) -> None:
        return None


def ex(
    eid: str, bids: list[tuple[float, float]] = (), asks: list[tuple[float, float]] = ()
) -> OrderbookExchange:  # type: ignore[assignment]
    return OrderbookExchange(
        exchangeId=eid,
        bids=[OrderbookLevel(price=p, quantity=q) for p, q in bids],
        asks=[OrderbookLevel(price=p, quantity=q) for p, q in asks],
    )


def market(mid: str = "M1", n: int = 1, composite: bool = False) -> Market:
    return Market(
        id=mid,
        title="t",
        status="open",
        isComposite=composite,
        isMultiOutcome=n > 1,
        exchanges=[{"id": f"E{i}"} for i in range(n)],
    )


def no_lean(_: str) -> str | None:
    return None


def position(eid: str, mid: str, qty: float, cost: float, value: float) -> Position:
    return Position(
        exchangeId=eid,
        marketId=mid,
        marketTitle="x",
        quantity=qty,
        costBasis=cost,
        marketValue=value,
    )


# -- price tick ----------------------------------------------------------
def test_snap_price_handles_float_complement() -> None:
    assert snap_price(1 - 0.555, up=False) == 0.445
    assert snap_price(0.4523, up=False) == 0.45
    assert snap_price(0.4523, up=True) == 0.455
    assert snap_price(0.0001, up=False) == 0.005
    assert snap_price(0.9999, up=True) == 0.995


def test_payload_is_on_tick_with_expiry() -> None:
    intent = OrderIntent(
        exchange_id="1", side=Side.NO, action=Action.BUY, quantity=5, price=1 - 0.555
    )
    body = intent.to_payload("T", ttl_seconds=60)
    assert body["price"] == 0.445
    assert body["tournamentId"] == "T"
    assert "expirationDate" in body


# -- arbitrage -----------------------------------------------------------
def test_binary_market_never_arbitrage() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.6, 100)], asks=[(0.62, 100)])])
    eng = SignalEngine(settings(), NullFairValueModel())
    assert eng.find_arbitrage(market(n=1), book) == []


def test_no_basket_when_bids_sum_above_one() -> None:
    book = MarketOrderbook(
        exchanges=[
            ex("E0", bids=[(0.5, 100)]),
            ex("E1", bids=[(0.4, 50)]),
            ex("E2", bids=[(0.2, 80)]),
        ]
    )
    eng = SignalEngine(settings(), NullFairValueModel())
    [cand] = eng.find_arbitrage(market(n=3), book)
    assert cand.edge == pytest.approx(0.1)
    assert [leg.side for leg in cand.legs] == [Side.NO] * 3
    assert [round(leg.price, 3) for leg in cand.legs] == [0.5, 0.6, 0.8]
    assert cand.basket_cost == pytest.approx(1.9)  # pays >= 2 -> profit >= 0.1


def test_no_arbitrage_when_bids_sum_below_one() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.5, 100)]), ex("E1", bids=[(0.45, 100)])])
    eng = SignalEngine(settings(), NullFairValueModel())
    assert eng.find_arbitrage(market(n=2), book) == []


def test_yes_basket_is_opt_in() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", asks=[(0.4, 100)]), ex("E1", asks=[(0.5, 100)])])
    assert SignalEngine(settings(), NullFairValueModel()).find_arbitrage(market(n=2), book) == []
    [cand] = SignalEngine(settings(allow_yes_basket_arb=True), NullFairValueModel()).find_arbitrage(
        market(n=2), book
    )
    assert cand.edge == pytest.approx(0.1)


def test_composite_markets_skipped() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.7, 100)]), ex("E1", bids=[(0.7, 100)])])
    eng = SignalEngine(settings(), NullFairValueModel())
    assert eng.find_arbitrage(market(n=2, composite=True), book) == []


# -- mispricing ----------------------------------------------------------
def test_open_yes_walks_book_while_edge_holds() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", asks=[(0.45, 100), (0.50, 100), (0.58, 100)])])
    eng = SignalEngine(settings(min_edge=0.07), Views({"E0": FairValue(0.60)}))
    [cand] = eng.evaluate(market(), book, {})
    leg = cand.legs[0]
    assert (leg.side, leg.action, leg.price, leg.available) == (Side.YES, Action.BUY, 0.50, 200)
    assert cand.edge == pytest.approx(0.10)


def test_open_no_when_yes_rich() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.70, 40)], asks=[(0.75, 10)])])
    eng = SignalEngine(settings(), Views({"E0": FairValue(0.50)}))
    [cand] = eng.evaluate(market(), book, {})
    assert cand.legs[0].side is Side.NO
    assert cand.legs[0].price == pytest.approx(0.30)
    assert cand.edge == pytest.approx(0.20)


def test_no_trade_inside_edge() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.48, 10)], asks=[(0.52, 10)])])
    eng = SignalEngine(settings(min_edge=0.07), Views({"E0": FairValue(0.55)}))
    assert eng.evaluate(market(), book, {}) == []


def test_close_yes_when_bid_reaches_fair_and_suppresses_open() -> None:
    # Fair 0.30 also implies a big NO edge; the close must win, not an open.
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.62, 30), (0.55, 50)], asks=[(0.64, 10)])])
    eng = SignalEngine(settings(), Views({"E0": FairValue(0.30)}))
    [cand] = eng.evaluate(market(), book, {"E0": 100})
    assert cand.kind == "close"
    assert (cand.legs[0].side, cand.legs[0].action) == (Side.YES, Action.SELL)
    assert cand.legs[0].available == 80 and cand.held == 100


def test_close_no_position() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.38, 30)], asks=[(0.40, 25)])])
    eng = SignalEngine(settings(), Views({"E0": FairValue(0.45)}))
    [cand] = eng.evaluate(market(), book, {"E0": -60})
    leg = cand.legs[0]
    assert (cand.kind, leg.side, leg.action) == ("close", Side.NO, Action.SELL)
    assert leg.price == pytest.approx(0.60)


# -- risk ----------------------------------------------------------------
def open_candidate(
    eid: str = "E0",
    price: float = 0.50,
    fair: float = 0.70,
    avail: int = 10**6,
    side: Side = Side.YES,
    lean: str | None = None,
):  # type: ignore[no-untyped-def]
    eng = SignalEngine(settings(min_edge=0.01), Views({eid: FairValue(fair, 1.0, lean)}))
    if side is Side.YES:
        book = MarketOrderbook(exchanges=[ex(eid, asks=[(price, avail)])])
    else:
        book = MarketOrderbook(exchanges=[ex(eid, bids=[(1 - price, avail)])])
    [cand] = eng.evaluate(market(), book, {})
    return cand


def test_kelly_target_counts_existing_holdings() -> None:
    s = settings(
        kelly_fraction=0.25, max_market_exposure=1.0, max_total_exposure=1.0, cash_reserve=0
    )
    rm = RiskManager(s, no_lean)
    cand = open_candidate()  # edge .2 @ .5 -> full kelly .4 -> quarter .1
    fresh = ExposureState(cash=100_000, equity=100_000)
    sized = rm.size(cand, fresh)
    assert sized is not None and sized.intents[0].quantity == 20_000  # 10k / 0.5

    held = ExposureState.from_positions(
        90_000, [position("E0", "M1", 20_000, 10_000, 10_000)], no_lean
    )
    assert rm.size(cand, held) is None  # already at target


def test_market_cap_and_depth_limit_quantity() -> None:
    rm = RiskManager(
        settings(kelly_fraction=1.0, max_market_exposure=0.01, cash_reserve=0), no_lean
    )
    sized = rm.size(open_candidate(), ExposureState(cash=100_000, equity=100_000))
    assert sized is not None and sized.intents[0].quantity == 2_000  # 1k / 0.5
    sized = rm.size(open_candidate(avail=300), ExposureState(cash=100_000, equity=100_000))
    assert sized is not None and sized.intents[0].quantity == 300


def test_cash_reserve_blocks_spending() -> None:
    rm = RiskManager(settings(cash_reserve=0.5), no_lean)
    state = ExposureState(cash=40_000, equity=100_000)  # below 50% reserve
    assert rm.size(open_candidate(), state) is None


def test_correlation_cap_by_lean() -> None:
    s = settings(
        kelly_fraction=1.0,
        max_market_exposure=1.0,
        max_total_exposure=1.0,
        max_correlated_exposure=0.02,
        cash_reserve=0,
    )
    leans = {"D1": "D", "R1": "R"}
    rm = RiskManager(s, leans.get)
    state = ExposureState(cash=100_000, equity=100_000)
    d = rm.size(open_candidate("D1", lean="D"), state)
    assert d is not None and d.intents[0].quantity == 4_000  # 2k cap / 0.5
    assert rm.size(open_candidate("D1", lean="D"), state) is None  # D cap reached
    # YES on an R-leaning race offsets D exposure, so it is allowed (up to 2x cap).
    r = rm.size(open_candidate("R1", lean="R"), state)
    assert r is not None and r.intents[0].quantity == 8_000


def test_arb_sizing_equal_quantity_across_legs() -> None:
    book = MarketOrderbook(
        exchanges=[
            ex("E0", bids=[(0.5, 100)]),
            ex("E1", bids=[(0.4, 50)]),
            ex("E2", bids=[(0.2, 80)]),
        ]
    )
    [cand] = SignalEngine(settings(), NullFairValueModel()).find_arbitrage(market(n=3), book)
    rm = RiskManager(settings(cash_reserve=0), no_lean)
    sized = rm.size(cand, ExposureState(cash=100_000, equity=100_000))
    assert sized is not None and sized.atomic
    assert {i.quantity for i in sized.intents} == {50}
    assert all(i.side is Side.NO and i.action is Action.BUY for i in sized.intents)


def test_close_bypasses_caps() -> None:
    book = MarketOrderbook(exchanges=[ex("E0", bids=[(0.8, 500)])])
    [cand] = SignalEngine(settings(), Views({"E0": FairValue(0.7)})).evaluate(
        market(), book, {"E0": 120}
    )
    rm = RiskManager(settings(), no_lean)
    sized = rm.size(cand, ExposureState(cash=0, equity=1))
    assert sized is not None
    assert (sized.intents[0].action, sized.intents[0].quantity) == (Action.SELL, 120)


def test_exposure_from_signed_positions() -> None:
    state = ExposureState.from_positions(
        1000.0,
        [position("A", "M1", 10, 5, 6), position("B", "M2", -10, 3, 4)],
        {"A": "D", "B": "D"}.get,
    )
    assert state.total_cost == 8.0
    assert state.equity == 1010.0
    assert state.per_exchange_cost == {"A": 5.0, "B": -3.0}
    assert state.net_d == 2.0  # +5 D (YES on D), -3 (NO on D)


# -- trading window ------------------------------------------------------
def test_trading_window() -> None:
    t = TournamentSummary(
        id="T",
        slug="s",
        name="n",
        status="active",
        startDate="2026-10-01T16:00:00Z",
        endDate="2026-11-04T17:00:00Z",
        initialBalance=100000,
        currencyName="SUSQies",
        myBalance=1,
    )
    assert not t.trading_open(datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc))[0]
    assert t.trading_open(datetime(2026, 10, 15, tzinfo=timezone.utc))[0]
    assert not t.trading_open(datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc))[0]


# -- per-race grouping -----------------------------------------------------
def test_race_key_groups_party_sides() -> None:
    from supermarket_bot.risk import race_key

    d = race_key("Will the Democratic Party win the U.S. House?")
    r = race_key("Will the Republican Party win the U.S. House?")
    i = race_key("Will the Independent Party win the U.S. House?")
    assert d == r == i
    assert race_key("Will the Republican Party win the CA-22 House race?") != race_key(
        "Will the Republican Party win the CA-48 House race?"
    )


def test_both_sides_of_one_race_share_the_cap() -> None:
    s = settings(
        kelly_fraction=1.0,
        max_market_exposure=0.05,
        max_total_exposure=1.0,
        max_correlated_exposure=1.0,
        cash_reserve=0,
    )
    groups = {"M1": "us house", "M2": "us house"}
    rm = RiskManager(s, no_lean, lambda m: groups.get(m, m))
    state = ExposureState(cash=100_000, equity=100_000)

    yes_d = open_candidate("E1")  # YES on "D wins the House"
    yes_d.market_id = "M1"
    no_r = open_candidate("E2", fair=0.30, side=Side.NO)  # NO on "R wins the House"
    assert no_r.legs[0].side is Side.NO
    no_r.market_id = "M2"

    first = rm.size(yes_d, state)
    assert first is not None and first.intents[0].quantity == 10_000  # 5k cap / 0.5
    # Same race, same outcome: the cap is already used up.
    assert rm.size(no_r, state) is None


def test_positions_count_toward_their_race() -> None:
    groups = {"M1": "race", "M2": "race"}
    state = ExposureState.from_positions(
        1000.0,
        [position("A", "M1", 10, 5, 6), position("B", "M2", -10, 3, 4)],
        no_lean,
        lambda m: groups.get(m, m),
    )
    assert state.per_market_cost == {"race": 8.0}

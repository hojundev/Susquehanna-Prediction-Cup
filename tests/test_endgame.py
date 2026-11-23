"""High-confidence lane and negative-edge position closing."""

from __future__ import annotations

from supermarket_bot.config import Settings
from supermarket_bot.fair_value import FairValue
from supermarket_bot.models import (
    Action,
    Market,
    MarketOrderbook,
    OrderbookExchange,
    OrderbookLevel,
    Side,
)
from supermarket_bot.risk import ExposureState, RiskManager
from supermarket_bot.signals import SignalEngine


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {"api_key": "k", "_env_file": None}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def no_lean(_: str) -> str | None:
    return None


class Views:
    def __init__(self, v: dict[str, FairValue]) -> None:
        self.v = v

    def estimate(self, exchange_id: str) -> FairValue | None:
        return self.v.get(exchange_id)

    def reload(self) -> None:
        return None


def ex(eid, bids=(), asks=()):  # type: ignore[no-untyped-def]
    return OrderbookExchange(
        exchangeId=eid,
        bids=[OrderbookLevel(price=p, quantity=q) for p, q in bids],
        asks=[OrderbookLevel(price=p, quantity=q) for p, q in asks],
    )


def market(mid="M", eid="E"):  # type: ignore[no-untyped-def]
    return Market(id=mid, title="t", status="open", exchanges=[{"id": eid}])


# -- high-confidence lane --------------------------------------------------
def test_high_conf_side_gets_larger_cap() -> None:
    s = settings(
        kelly_fraction=1.0,
        max_market_exposure=0.05,
        high_conf_threshold=0.95,
        high_conf_market_cap=0.15,
        max_total_exposure=1.0,
        cash_reserve=0.0,
        min_edge=0.01,
    )
    # Buy YES at 0.955, fair 0.985 -> side fair >= 0.95, high-conf lane.
    book = MarketOrderbook(exchanges=[ex("E", asks=[(0.955, 10**6)])])
    [cand] = SignalEngine(s, Views({"E": FairValue(0.985, 1.0)})).evaluate(market(), book, {})
    rm = RiskManager(s, no_lean)
    sized = rm.size(cand, ExposureState(cash=100_000, equity=100_000))
    assert sized is not None
    # 15% cap, not 5%: cost up to ~15000 at 0.955.
    cost = sized.intents[0].quantity * 0.955
    assert 14_000 <= cost <= 15_000


def test_normal_market_keeps_small_cap() -> None:
    s = settings(
        kelly_fraction=1.0,
        max_market_exposure=0.05,
        high_conf_threshold=0.95,
        high_conf_market_cap=0.15,
        max_total_exposure=1.0,
        cash_reserve=0.0,
        min_edge=0.01,
    )
    # Fair 0.60: ordinary market, 5% cap applies.
    book = MarketOrderbook(exchanges=[ex("E", asks=[(0.55, 10**6)])])
    [cand] = SignalEngine(s, Views({"E": FairValue(0.60, 1.0)})).evaluate(market(), book, {})
    rm = RiskManager(s, no_lean)
    sized = rm.size(cand, ExposureState(cash=100_000, equity=100_000))
    assert sized is not None
    assert sized.intents[0].quantity * 0.55 <= 5_000 + 1


def test_high_conf_no_side() -> None:
    s = settings(
        kelly_fraction=1.0,
        max_market_exposure=0.05,
        high_conf_market_cap=0.15,
        max_total_exposure=1.0,
        cash_reserve=0.0,
        min_edge=0.01,
    )
    # NO buy whose NO-side fair (1 - 0.03 = 0.97) clears the threshold gets the
    # bigger cap. Build the candidate directly to isolate cap selection.
    from supermarket_bot.signals import Leg, TradeCandidate

    def make() -> TradeCandidate:
        return TradeCandidate(
            kind="open",
            market_id="M",
            legs=[Leg("E", Side.NO, Action.BUY, 0.90, 10**6)],
            edge=0.05,
            reason="x",
            confidence=1.0,
            fair=FairValue(0.03, 1.0),  # NO-side fair 0.97 -> high-conf
        )

    # High-conf NO sizes strictly larger than the same candidate would under the
    # normal 5% cap (fair just below threshold).
    hi = RiskManager(s, no_lean).size(make(), ExposureState(cash=100_000, equity=100_000))
    low_cand = make()
    low_cand.fair = FairValue(0.10, 1.0)  # NO-side fair 0.90 -> below 0.95 threshold
    lo = RiskManager(s, no_lean).size(low_cand, ExposureState(cash=100_000, equity=100_000))
    assert hi is not None and lo is not None
    assert hi.intents[0].quantity > lo.intents[0].quantity
    # High-conf uses the 15% cap: cost near 15000 at price 0.90.
    assert 14_000 <= hi.intents[0].quantity * 0.90 <= 15_000


# -- negative-edge close ---------------------------------------------------
def test_cut_yes_position_with_negative_edge() -> None:
    s = settings(close_negative_edge=True, close_edge_threshold=0.0, take_profit=False)
    # Hold YES. Best bid 0.40 > fair 0.35, so held edge = 0.35 - 0.40 = -0.05 <= 0 -> cut.
    book = MarketOrderbook(exchanges=[ex("E", bids=[(0.40, 500)], asks=[(0.42, 10)])])
    [c] = SignalEngine(s, Views({"E": FairValue(0.35, 1.0)})).evaluate(market(), book, {"E": 300})
    assert c.kind == "close"
    assert (c.legs[0].side, c.legs[0].action) == (Side.YES, Action.SELL)
    assert c.held == 300


def test_cut_no_position_with_negative_edge() -> None:
    s = settings(close_negative_edge=True, close_edge_threshold=0.0, take_profit=False)
    # Hold NO. Best ask 0.28 -> NO sells at 0.72. Fair 0.75 -> NO fair 0.25.
    # Held edge = 0.72 - 0.25... that's NO sell price minus NO fair = 0.72-0.25>0, keep.
    # Make it negative: ask 0.20 -> NO sells 0.80, NO fair 0.25 -> edge 0.80-0.25>0 still.
    # Edge on held NO = (1-ask) - (1-fair) = fair - ask. ask 0.80, fair 0.75 -> -0.05 cut.
    book = MarketOrderbook(exchanges=[ex("E", bids=[(0.78, 10)], asks=[(0.80, 400)])])
    [c] = SignalEngine(s, Views({"E": FairValue(0.75, 1.0)})).evaluate(market(), book, {"E": -200})
    assert c.kind == "close"
    assert (c.legs[0].side, c.legs[0].action) == (Side.NO, Action.SELL)


def test_positive_edge_paper_loss_is_held_not_cut() -> None:
    # The 1065 case: deeply underwater but strong positive edge -> must hold.
    s = settings(close_negative_edge=True, close_edge_threshold=0.0, take_profit=False)
    # Hold NO, ask 0.85 -> NO sells 0.15... use YES-terms: NO position means held<0.
    # Best ask 0.86 -> held NO edge = fair - ask = 0.95 - 0.86 = +0.09 -> keep.
    book = MarketOrderbook(exchanges=[ex("E", bids=[(0.84, 10)], asks=[(0.86, 400)])])
    out = SignalEngine(s, Views({"E": FairValue(0.95, 1.0)})).evaluate(market(), book, {"E": -200})
    assert out == [] or all(c.kind != "close" for c in out)


def test_close_disabled_by_default() -> None:
    s = settings(take_profit=False)  # close_negative_edge defaults False
    book = MarketOrderbook(exchanges=[ex("E", bids=[(0.40, 500)])])
    out = SignalEngine(s, Views({"E": FairValue(0.35, 1.0)})).evaluate(market(), book, {"E": 300})
    assert out == []


# -- pre-screen fetches held positions when closing ------------------------
class _PriceClient:
    """Minimal client stub exposing only what _markets_to_fetch touches."""

    def __init__(self, quotes: dict[str, object]) -> None:
        self._quotes = quotes

    def bulk_prices(self, ids):  # type: ignore[no-untyped-def]
        got = {e: self._quotes[e] for e in ids if e in self._quotes}
        missing = {e for e in ids if e not in self._quotes}
        return got, missing


def _engine(s: Settings, views: Views, quotes: dict[str, object]):  # type: ignore[no-untyped-def]
    from supermarket_bot.engine import TradingEngine

    return TradingEngine(s, _PriceClient(quotes), views)  # type: ignore[arg-type]


def _bulk(eid: str, bid: float | None, ask: float | None):  # type: ignore[no-untyped-def]
    from supermarket_bot.models import BulkExchangePrice

    return BulkExchangePrice(exchangeId=eid, bestBid=bid, bestAsk=ask)


def test_prescreen_keeps_held_market_when_closing_at_cap() -> None:
    # Buy-and-hold at cap (allow_open=False). We hold YES at E, and its top of
    # book shows no take-profit (bid 0.40 < fair 0.60), so without the close
    # flag the market is skipped. With close_negative_edge on it must be kept
    # so evaluate() can later cut it.
    s = settings(close_negative_edge=True, bulk_price_scan=True)
    eng = _engine(s, Views({"E": FairValue(0.60, 1.0)}), {"E": _bulk("E", 0.40, 0.42)})
    kept = eng._markets_to_fetch([market("M", "E")], {"E": 300}, allow_open=False)
    assert kept == ["M"]


def test_prescreen_skips_held_market_when_close_disabled() -> None:
    # Same book, close disabled, no take-profit -> market is pre-screened out.
    s = settings(close_negative_edge=False, bulk_price_scan=True)
    eng = _engine(s, Views({"E": FairValue(0.60, 1.0)}), {"E": _bulk("E", 0.40, 0.42)})
    kept = eng._markets_to_fetch([market("M", "E")], {"E": 300}, allow_open=False)
    assert kept == []

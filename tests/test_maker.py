"""Market maker: pricing, sizing, inventory control and engine integration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from supermarket_bot.client import SuperMarketClient
from supermarket_bot.config import Settings
from supermarket_bot.engine import TradingEngine
from supermarket_bot.fair_value import FairValue
from supermarket_bot.maker import (
    MakerInput,
    MarketMaker,
    Top,
    load_owned,
    quote_prices,
    save_owned,
)
from supermarket_bot.models import Position, Side
from supermarket_bot.risk import ExposureState, RiskManager

TID = "11111111-2222-3333-4444-555555555555"


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {"api_key": "k", "_env_file": None, "maker_enabled": True}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def no_lean(_: str) -> str | None:
    return None


def maker(s: Settings) -> MarketMaker:
    return MarketMaker(s, RiskManager(s, no_lean), no_lean, lambda m: m)


def inp(
    eid: str = "E1",
    fair: float = 0.60,
    bid: float | None = 0.55,
    ask: float | None = 0.66,
    held: int = 0,
    conf: float = 0.8,
) -> MakerInput:
    return MakerInput(eid, f"M{eid}", FairValue(fair, conf), Top(bid, ask), held)


# -- pricing ---------------------------------------------------------------
def test_quotes_lead_a_wide_book_by_one_tick() -> None:
    assert quote_prices(0.60, Top(0.55, 0.66), 0.02, 0.01, 0) == (0.555, 0.655)


def test_quotes_stay_half_spread_from_fair_in_a_tight_book() -> None:
    assert quote_prices(0.60, Top(0.59, 0.61), 0.02, 0.01, 0) == (0.58, 0.62)


def test_quotes_never_cross_the_book() -> None:
    # Book sits below fair: the bid must stay under the best ask (0.52).
    bid, ask = quote_prices(0.60, Top(0.50, 0.52), 0.02, 0.01, 0)
    assert bid == 0.505 and ask == 0.62
    # Book above fair: the ask must stay over the best bid (0.70).
    bid, ask = quote_prices(0.60, Top(0.70, 0.72), 0.02, 0.01, 0)
    assert bid == 0.58 and ask == 0.715


def test_empty_book_quotes_around_fair() -> None:
    assert quote_prices(0.60, Top(None, None), 0.02, 0.01, 0) == (0.58, 0.62)


def test_inventory_skews_both_quotes_away_from_held_side() -> None:
    long_yes = quote_prices(0.60, Top(None, None), 0.02, 0.01, 1.0)
    long_no = quote_prices(0.60, Top(None, None), 0.02, 0.01, -1.0)
    assert long_yes == (0.57, 0.61)  # less eager to buy YES, more eager to sell
    assert long_no == (0.59, 0.63)
    # Skew never pushes a quote to or through fair value.
    assert quote_prices(0.60, Top(None, None), 0.02, 0.10, 1.0)[1] == 0.605


# -- sizing ----------------------------------------------------------------
def test_plan_posts_yes_bid_and_no_buy_for_the_ask() -> None:
    s = settings(maker_quote_size=200)
    state = ExposureState(cash=100_000, equity=100_000)
    trades, quoted = maker(s).plan([inp()], state, set())
    assert quoted == {"E1"}
    [t] = trades
    yes, no = t.intents
    assert (yes.side, yes.price, yes.quantity) == (Side.YES, 0.555, int(200 / 0.555))
    assert (no.side, no.price, no.quantity) == (Side.NO, 0.345, int(200 / 0.345))
    # Resting quotes are reserved against the maker budget, not buy-and-hold's.
    assert state.total_cost == 0
    assert state.maker_cost == pytest.approx(yes.quantity * 0.555 + no.quantity * 0.345)


def test_inventory_cap_stops_the_side_that_adds() -> None:
    s = settings(maker_max_inventory=1000)
    state = ExposureState(cash=100_000, equity=100_000)
    state.per_exchange_cost["E1"] = 1000.0  # already long 1000 of YES
    [t], _ = maker(s).plan([inp(held=1700)], state, {"E1"})
    assert [o.side for o in t.intents] == [Side.NO]


def test_reducing_side_needs_no_budget() -> None:
    s = settings(maker_budget=0.0, maker_quote_size=100)
    state = ExposureState(cash=100_000, equity=100_000)
    state.per_exchange_cost["E1"] = -150.0  # long NO
    [t], _ = maker(s).plan([inp(held=-400)], state, {"E1"})
    # The YES bid nets against held NO, so it quotes without budget; the NO
    # side would add inventory and the maker budget is zero.
    assert [o.side for o in t.intents] == [Side.YES]
    assert t.intents[0].quantity == int(100 / 0.555)


def test_budget_shrinks_quotes() -> None:
    s = settings(maker_budget=0.001, maker_quote_size=1000)  # 100 SUSQies
    state = ExposureState(cash=100_000, equity=100_000)
    [t], _ = maker(s).plan([inp()], state, set())
    assert sum(o.quantity * o.price for o in t.intents) <= 100 + 1e-6


def test_eligibility_band_and_confidence() -> None:
    s = settings()
    state = ExposureState(cash=100_000, equity=100_000)
    picks = [inp("A", fair=0.97), inp("B", conf=0.2), inp("C")]
    _, quoted = maker(s).plan(picks, state, set())
    assert quoted == {"C"}


def test_inventory_first_then_max_markets() -> None:
    s = settings(maker_max_markets=2)
    state = ExposureState(cash=100_000, equity=100_000)
    picks = [inp("A"), inp("B"), inp("C", held=50)]
    _, quoted = maker(s).plan(picks, state, set())
    assert "C" in quoted and len(quoted) == 2


def test_maker_positions_counted_outside_buy_and_hold_cap() -> None:
    pos = [
        Position(
            exchangeId="MM1",
            marketId="M1",
            marketTitle="x",
            quantity=100,
            costBasis=60,
            marketValue=60,
        ),
        Position(
            exchangeId="BH1",
            marketId="M2",
            marketTitle="y",
            quantity=100,
            costBasis=40,
            marketValue=40,
        ),
    ]
    state = ExposureState.from_positions(1000, pos, no_lean, maker_exchanges={"MM1"})
    assert state.total_cost == 40 and state.maker_cost == 60
    assert state.per_market_cost == {"M1": 60, "M2": 40}  # race caps still see both


def test_state_file_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state" / "maker.json"
    assert load_owned(p) == set()
    save_owned(p, {"12", "7"})
    assert load_owned(p) == {"12", "7"}
    assert json.loads(p.read_text()) == {"exchanges": ["12", "7"]}


# -- engine ----------------------------------------------------------------
class Views:
    def __init__(self, views: dict[str, FairValue]) -> None:
        self.views = views

    def estimate(self, exchange_id: str) -> FairValue | None:
        return self.views.get(exchange_id)

    def reload(self) -> None:
        return None


def api(
    *,
    tops: dict[str, tuple[float, float]],
    positions: list[dict[str, Any]] | None = None,
    end: str | None = None,
) -> tuple[httpx.MockTransport, list[tuple[str, str, Any]]]:
    calls: list[tuple[str, str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, path, body))
        if path == "/tournaments/midterm-elections":
            return httpx.Response(
                200,
                json={
                    "id": TID,
                    "slug": "midterm-elections",
                    "name": "M",
                    "status": "active",
                    "startDate": None,
                    "endDate": end,
                    "initialBalance": 100000,
                    "currencyName": "S",
                    "myBalance": 100000,
                    "isPendingEnrolment": False,
                },
            )
        if path == "/tournaments/midterm-elections/markets":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": f"M{e}",
                            "title": f"Will the Democratic Party win race {e}?",
                            "status": "open",
                            "exchanges": [{"id": e}],
                        }
                        for e in tops
                    ],
                    "pagination": {"hasMore": False, "nextCursor": None},
                },
            )
        if path == "/exchanges/prices":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"exchangeId": e, "marketId": f"M{e}", "bestBid": b, "bestAsk": a}
                        for e, (b, a) in tops.items()
                    ],
                    "missingIds": [],
                },
            )
        if path.endswith("/orderbook"):
            e = path.split("/")[2].removeprefix("M")
            b, a = tops[e]
            return httpx.Response(
                200,
                json={
                    "exchanges": [
                        {
                            "exchangeId": e,
                            "bids": [{"price": b, "quantity": 100}],
                            "asks": [{"price": a, "quantity": 100}],
                        }
                    ]
                },
            )
        if path == "/relationships":
            return httpx.Response(200, json={"data": [], "pagination": {"hasMore": False}})
        if path == "/tournaments/midterm-elections/portfolio/positions":
            return httpx.Response(200, json={"positions": positions or []})
        if path == "/tournaments/midterm-elections/leaderboard":
            return httpx.Response(200, json={"myRank": None})
        if path == "/orders/cancel-all":
            return httpx.Response(200, json={"cancelled": 0})
        if path == "/orders/batch":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": i, "ok": True, "status": 200, "data": {}}
                        for i in range(len(body["orders"]))
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": path}})

    return httpx.MockTransport(handler), calls


def orders(calls: list[tuple[str, str, Any]]) -> list[dict[str, Any]]:
    return [o for m, p, b in calls if p == "/orders/batch" for o in b["orders"]]


def test_engine_quotes_and_records_owned(tmp_path: Path) -> None:
    state_file = tmp_path / "maker.json"
    transport, calls = api(tops={"10": (0.40, 0.50)})
    s = settings(dry_run=False, maker_state_path=str(state_file), maker_quote_size=100)
    client = SuperMarketClient(s, transport=transport)
    # Fair 0.45 sits inside the 0.40/0.50 book: no buy-and-hold edge.
    report = TradingEngine(s, client, Views({"10": FairValue(0.45, 0.8)})).run_cycle()

    assert report.maker_exchanges == 1
    got = {(o["side"], o["action"], o["price"]) for o in orders(calls)}
    assert got == {("yes", "buy", 0.405), ("no", "buy", 0.505)}
    assert all(o["tournamentId"] == TID and "expirationDate" in o for o in orders(calls))
    assert load_owned(state_file) == {"10"}


def test_buy_and_hold_skips_maker_exchanges(tmp_path: Path) -> None:
    state_file = tmp_path / "maker.json"
    save_owned(state_file, {"10"})
    transport, calls = api(tops={"10": (0.40, 0.50)})
    s = settings(dry_run=False, maker_state_path=str(state_file), min_edge=0.07)
    client = SuperMarketClient(s, transport=transport)
    # Fair 0.60 vs ask 0.50 would be a buy-and-hold open, but "10" is the maker's.
    TradingEngine(s, client, Views({"10": FairValue(0.60, 0.8)})).run_cycle()

    prices = sorted(o["price"] for o in orders(calls))
    assert prices == [0.38, 0.405]  # maker only: YES bid 0.405, NO at 1 - 0.62


def test_maker_stays_out_of_buy_and_hold_races(tmp_path: Path) -> None:
    transport, calls = api(
        tops={"10": (0.40, 0.50)},
        positions=[
            {
                "exchangeId": "99",
                "marketId": "M10x",
                "marketTitle": "Will the Republican Party win race 10?",
                "quantity": 10,
                "costBasis": 5,
                "marketValue": 5,
            }
        ],
    )
    s = settings(dry_run=False, maker_state_path=str(tmp_path / "m.json"))
    client = SuperMarketClient(s, transport=transport)
    report = TradingEngine(s, client, Views({"10": FairValue(0.45, 0.8)})).run_cycle()
    assert report.maker_exchanges == 0 and not orders(calls)


def test_maker_pauses_before_tournament_end(tmp_path: Path) -> None:
    soon = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    transport, calls = api(tops={"10": (0.40, 0.50)}, end=soon)
    s = settings(dry_run=False, maker_state_path=str(tmp_path / "m.json"))
    client = SuperMarketClient(s, transport=transport)
    report = TradingEngine(s, client, Views({"10": FairValue(0.45, 0.8)})).run_cycle()
    assert report.maker_exchanges == 0 and not orders(calls)


def test_maker_off_by_default(tmp_path: Path) -> None:
    transport, calls = api(tops={"10": (0.40, 0.50)})
    s = settings(dry_run=False, maker_enabled=False, maker_state_path=str(tmp_path / "m.json"))
    client = SuperMarketClient(s, transport=transport)
    TradingEngine(s, client, Views({"10": FairValue(0.45, 0.8)})).run_cycle()
    assert not orders(calls)
    assert Settings(api_key="k", _env_file=None).maker_enabled is False  # type: ignore[call-arg]


def test_dry_run_does_not_write_state(tmp_path: Path) -> None:
    state_file = tmp_path / "maker.json"
    transport, calls = api(tops={"10": (0.40, 0.50)})
    s = settings(dry_run=True, maker_state_path=str(state_file))
    client = SuperMarketClient(s, transport=transport)
    report = TradingEngine(s, client, Views({"10": FairValue(0.45, 0.8)})).run_cycle()
    assert report.maker_exchanges == 1
    assert not state_file.exists() and not orders(calls)

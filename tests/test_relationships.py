"""Cross-market relationship arbitrage."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from supermarket_bot.models import OrderbookExchange, OrderbookLevel, Side
from supermarket_bot.relationships import (
    Relationship,
    RelationshipArbitrage,
    SuggestedPair,
    parse_hints,
)


def book(
    eid: str, bid: float | None = None, ask: float | None = None, qty: float = 100
) -> OrderbookExchange:
    return OrderbookExchange(
        exchangeId=eid,
        bids=[OrderbookLevel(price=bid, quantity=qty)] if bid is not None else [],
        asks=[OrderbookLevel(price=ask, quantity=qty)] if ask is not None else [],
    )


def rel(
    rtype: str,
    ids: list[str],
    *,
    frm: list[str] | None = None,
    to: list[str] | None = None,
    exhaustive: bool = False,
    status: str = "active",
    roles: dict[str, str] | None = None,
) -> Relationship:
    raw: dict[str, Any] = {
        "id": f"r-{rtype}",
        "type": rtype,
        "status": status,
        "isExhaustive": exhaustive,
        "nodes": [
            {
                "exchangeId": e,
                "marketId": f"M{e}",
                "marketTitle": f"T{e}",
                "outcome": None,
                "role": (roles or {}).get(e, "member"),
            }
            for e in ids
        ],
        "direction": {"kind": "x", "fromExchangeIds": frm or [], "toExchangeIds": to or []},
    }
    return Relationship.from_json(raw)


ARB = RelationshipArbitrage(min_profit=0.01)


def test_implication_trades_when_antecedent_bid_exceeds_consequent_ask() -> None:
    # A (wins by 10+) implies B (wins). bid(A)=0.55 > ask(B)=0.50.
    r = rel("implication", ["1", "2"], frm=["1"], to=["2"])
    [c] = ARB.find([r], {"1": book("1", bid=0.55, qty=30), "2": book("2", ask=0.50)}, {})
    assert c.edge == pytest.approx(0.05)
    legs = {(leg.exchange_id, leg.side): leg for leg in c.legs}
    assert legs[("2", Side.YES)].price == pytest.approx(0.50)
    assert legs[("1", Side.NO)].price == pytest.approx(0.45)
    assert c.basket_cost == pytest.approx(0.95)  # pays >= 1 in every outcome
    assert legs[("1", Side.NO)].available == 30


def test_implication_consistent_prices_no_trade() -> None:
    r = rel("implication", ["1", "2"], frm=["1"], to=["2"])
    assert ARB.find([r], {"1": book("1", bid=0.40), "2": book("2", ask=0.50)}, {}) == []


def test_implication_uses_roles_when_direction_missing() -> None:
    r = rel("implication", ["1", "2"], roles={"1": "antecedent", "2": "consequent"})
    assert len(ARB.find([r], {"1": book("1", bid=0.6), "2": book("2", ask=0.5)}, {})) == 1


def test_complementary_baskets() -> None:
    r = rel("complementary", ["1", "2"])
    [yes] = ARB.find([r], {"1": book("1", ask=0.40), "2": book("2", ask=0.55)}, {})
    assert yes.edge == pytest.approx(0.05) and {leg.side for leg in yes.legs} == {Side.YES}
    [no] = ARB.find([r], {"1": book("1", bid=0.50), "2": book("2", bid=0.56)}, {})
    assert no.edge == pytest.approx(0.06) and {leg.side for leg in no.legs} == {Side.NO}


def test_mutually_exclusive_yes_basket_needs_exhaustive() -> None:
    books = {e: book(e, ask=0.3) for e in ("1", "2", "3")}
    assert ARB.find([rel("mutually_exclusive", ["1", "2", "3"])], books, {}) == []
    [c] = ARB.find([rel("mutually_exclusive", ["1", "2", "3"], exhaustive=True)], books, {})
    assert c.edge == pytest.approx(0.1)


def test_monotonic_needs_engine_hint() -> None:
    r = rel("monotonic", ["1", "2"])
    books = {"1": book("1", bid=0.60), "2": book("2", ask=0.50)}
    assert ARB.find([r], books, {}) == []
    [c] = ARB.find([r], books, {r.id: SuggestedPair(sell_id="1", buy_id="2")})
    assert c.edge == pytest.approx(0.10)


def test_skips_inactive_untradable_missing_books_and_one_sided() -> None:
    books = {"1": book("1", bid=0.6), "2": book("2", ask=0.5)}
    assert (
        ARB.find([rel("implication", ["1", "2"], frm=["1"], to=["2"], status="stale")], books, {})
        == []
    )
    assert ARB.find([rel("boolean_and", ["1", "2"])], books, {}) == []
    assert ARB.find([rel("implication", ["1", "9"], frm=["1"], to=["9"])], books, {}) == []
    assert (
        ARB.find(
            [rel("implication", ["1", "2"], frm=["1"], to=["2"])],
            {"1": book("1", bid=0.6), "2": book("2", bid=0.5)},
            {},
        )
        == []
    )


def test_parse_hints() -> None:
    constraints = [
        {
            "relationshipId": "a",
            "type": "monotonic",
            "evaluationStatus": "violated",
            "suggestedCorrectiveTrades": [
                {"exchangeId": "1", "outcomeSide": "Yes", "action": "Sell"},
                {"exchangeId": "2", "outcomeSide": "Yes", "action": "Buy"},
            ],
        },
        {
            "relationshipId": "b",
            "type": "monotonic",
            "evaluationStatus": "violated",
            "suggestedCorrectiveTrades": [
                {"exchangeId": "3", "outcomeSide": "No", "action": "Buy"},
                {"exchangeId": "4", "outcomeSide": "No", "action": "Sell"},
            ],
        },
        {
            "relationshipId": "c",
            "type": "monotonic",
            "evaluationStatus": "violated",
            "suggestedCorrectiveTrades": [
                {"exchangeId": "5", "outcomeSide": "Yes", "action": "Buy"}
            ],
        },
        {
            "relationshipId": "d",
            "type": "implication",
            "evaluationStatus": "violated",
            "suggestedCorrectiveTrades": [],
        },
    ]
    assert parse_hints(constraints) == {
        "a": SuggestedPair(sell_id="1", buy_id="2"),
        "b": SuggestedPair(sell_id="3", buy_id="4"),
    }


def test_engine_cycle_places_relationship_basket_atomically() -> None:
    from supermarket_bot.client import SuperMarketClient
    from supermarket_bot.config import Settings
    from supermarket_bot.engine import TradingEngine
    from supermarket_bot.fair_value import NullFairValueModel

    tid = "T-1"
    calls: list[tuple[str, str, Any]] = []

    def lv(p: float, q: float) -> list[dict[str, float]]:
        return [{"price": p, "quantity": q}]

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(req.content) if req.content else None
        path = req.url.path.removeprefix("/api/v1")
        calls.append((req.method, path, body))
        if path == "/tournaments/midterm-elections":
            return httpx.Response(
                200,
                json={
                    "id": tid,
                    "slug": "midterm-elections",
                    "name": "M",
                    "status": "active",
                    "startDate": None,
                    "endDate": None,
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
                        {"id": m, "title": m, "status": "open", "exchanges": [{"id": e}]}
                        for m, e in (("MA", "1"), ("MB", "2"))
                    ],
                    "pagination": {"hasMore": False, "nextCursor": None},
                },
            )
        if path == "/markets/MA/orderbook":
            return httpx.Response(
                200,
                json={
                    "exchanges": [{"exchangeId": "1", "bids": lv(0.58, 40), "asks": lv(0.60, 40)}],
                    "contexts": [],
                },
            )
        if path == "/markets/MB/orderbook":
            return httpx.Response(
                200,
                json={
                    "exchanges": [{"exchangeId": "2", "bids": lv(0.48, 70), "asks": lv(0.50, 70)}],
                    "contexts": [],
                },
            )
        if path == "/relationships":
            assert req.url.params["tournamentId"] == tid
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "rel-1",
                            "type": "implication",
                            "status": "active",
                            "isExhaustive": False,
                            "nodes": [
                                {"exchangeId": "1", "marketId": "MA", "role": "antecedent"},
                                {"exchangeId": "2", "marketId": "MB", "role": "consequent"},
                            ],
                            "direction": {"fromExchangeIds": ["1"], "toExchangeIds": ["2"]},
                        }
                    ],
                    "pagination": {"hasMore": False},
                },
            )
        if path == "/tournaments/midterm-elections/portfolio/positions":
            return httpx.Response(200, json={"positions": []})
        if path == "/tournaments/midterm-elections/leaderboard":
            return httpx.Response(200, json={"myRank": None})
        if path == "/orders/cancel-all":
            return httpx.Response(200, json={"cancelled": 0})
        if path == "/orders/multi-leg":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "index": i,
                            "data": {"orderId": i, "exchangeId": leg["exchangeId"], "open": False},
                        }
                        for i, leg in enumerate(body["legs"])
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": path}})

    s = Settings(
        api_key="k",
        _env_file=None,
        dry_run=False,
        cash_reserve=0.0,  # type: ignore[call-arg]
        use_kalshi=False,
        use_polymarket=False,
    )
    client = SuperMarketClient(s, transport=httpx.MockTransport(handler))
    report = TradingEngine(s, client, NullFairValueModel()).run_cycle()

    [legs_body] = [b for m, p, b in calls if p == "/orders/multi-leg"]
    legs = {(leg["exchangeId"], leg["side"]): leg for leg in legs_body["legs"]}
    assert legs[("2", "yes")]["price"] == 0.5
    assert legs[("1", "no")]["price"] == 0.42
    assert {leg["quantity"] for leg in legs.values()} == {40}  # thinner leg
    assert report.orders_submitted == 2

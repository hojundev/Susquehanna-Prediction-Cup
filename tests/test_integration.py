"""Client + engine against a mocked Super Market API (httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from supermarket_bot.client import SuperMarketClient
from supermarket_bot.config import Settings
from supermarket_bot.engine import TradingEngine
from supermarket_bot.fair_value import FairValue

TID = "11111111-2222-3333-4444-555555555555"


class Views:
    def __init__(self, views: dict[str, FairValue]) -> None:
        self.views = views

    def estimate(self, exchange_id: str) -> FairValue | None:
        return self.views.get(exchange_id)

    def reload(self) -> None:
        return None


class FakeApi:
    def __init__(
        self, start: str = "2026-01-01T00:00:00Z", end: str = "2030-01-01T00:00:00Z"
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.start, self.end = start, end

    def book(self, eid: str, bids: list[list[float]], asks: list[list[float]]) -> dict[str, Any]:
        lv = lambda xs: [{"price": p, "quantity": q} for p, q in xs]  # noqa: E731
        return {
            "exchangeId": eid,
            "option": None,
            "asOf": None,
            "impliedProbability": None,
            "bids": lv(bids),
            "asks": lv(asks),
        }

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        path = req.url.path.removeprefix("/api/v1")
        self.calls.append((req.method, path, body))
        q = dict(req.url.params)

        if path == "/tournaments/midterm-elections":
            return httpx.Response(
                200,
                json={
                    "id": TID,
                    "slug": "midterm-elections",
                    "name": "Midterms",
                    "description": None,
                    "status": "active",
                    "startDate": self.start,
                    "endDate": self.end,
                    "initialBalance": 100000,
                    "currencyName": "SUSQies",
                    "memberCount": 5,
                    "marketCount": 3,
                    "myBalance": 100000,
                    "isPendingEnrolment": False,
                },
            )
        if path == "/tournaments/midterm-elections/markets":
            assert q["limit"] == "100"
            if "cursor" not in q:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "M1",
                                "title": "Binary",
                                "status": "open",
                                "isComposite": False,
                                "isMultiOutcome": False,
                                "exchanges": [{"id": "10"}],
                            }
                        ],
                        "pagination": {
                            "total": 2,
                            "limit": 100,
                            "hasMore": True,
                            "nextCursor": "c2",
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "M2",
                            "title": "Multi",
                            "status": "open",
                            "isComposite": False,
                            "isMultiOutcome": True,
                            "exchanges": [{"id": "20"}, {"id": "21"}],
                        }
                    ],
                    "pagination": {"total": 2, "limit": 100, "hasMore": False, "nextCursor": None},
                },
            )
        if path == "/markets/M1/orderbook":
            assert q["tournamentId"] == TID
            wrong = {
                "exchanges": [self.book("10", [[0.9, 999]], [[0.91, 999]])],
                "overround": 0.91,
                "hasArbitrageOpportunity": False,
            }
            right = {
                "exchanges": [self.book("10", [[0.40, 100]], [[0.45, 100_000]])],
                "overround": 0.45,
                "hasArbitrageOpportunity": False,
            }
            return httpx.Response(
                200,
                json={
                    **wrong,
                    "contexts": [
                        {"type": "tournament", "tournament": {"id": "other"}, "orderbook": wrong},
                        {"type": "tournament", "tournament": {"id": TID}, "orderbook": right},
                    ],
                },
            )
        if path == "/markets/M2/orderbook":
            return httpx.Response(
                200,
                json={
                    "exchanges": [
                        self.book("20", [[0.60, 40]], []),
                        self.book("21", [[0.45, 70]], []),
                    ],
                    "overround": None,
                    "hasArbitrageOpportunity": True,
                    "contexts": [],
                },
            )
        if path == "/tournaments/midterm-elections/portfolio/positions":
            return httpx.Response(
                200,
                json={
                    "positions": [],
                    "summary": {
                        "totalMarketValue": 0,
                        "totalCostBasis": 0,
                        "totalUnrealizedPnl": 0,
                    },
                },
            )
        if path == "/tournaments/midterm-elections/leaderboard":
            return httpx.Response(200, json={"leaderboard": [], "myRank": None})
        if path == "/orders/cancel-all":
            return httpx.Response(200, json={"cancelled": 0})
        if path == "/orders/multi-leg":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "index": i,
                            "data": {
                                "orderId": 100 + i,
                                "exchangeId": leg["exchangeId"],
                                "open": False,
                                "quantityTraded": leg["quantity"],
                                "totalCost": 1,
                                "fillPrice": leg["price"],
                                "all": None,
                            },
                        }
                        for i, leg in enumerate(body["legs"])
                    ]
                },
            )
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


def make(api: FakeApi, **kw: object) -> tuple[SuperMarketClient, Settings]:
    base: dict[str, object] = {"api_key": "k", "_env_file": None, "cash_reserve": 0.0}
    base.update(kw)
    s = Settings(**base)  # type: ignore[arg-type]
    return SuperMarketClient(s, transport=httpx.MockTransport(api)), s


def test_client_paginates_and_picks_tournament_context() -> None:
    api = FakeApi()
    client, _ = make(api)
    markets = client.list_markets()
    assert [m.id for m in markets] == ["M1", "M2"]
    book = client.get_orderbook("M1")
    assert book.exchanges[0].best_ask().price == 0.45  # type: ignore[union-attr]


def test_get_orderbooks_concurrent_skips_failures_and_reports_progress() -> None:
    api = FakeApi()
    client, _ = make(api)
    seen: list[tuple[int, int]] = []
    books = client.get_orderbooks(
        ["M1", "M2", "MX"], depth=10, on_progress=lambda d, t: seen.append((d, t))
    )
    assert set(books) == {"M1", "M2"}  # MX 404s and is skipped, not fatal
    assert seen and seen[-1] == (3, 3)


def test_live_cycle_places_arb_atomically_and_opens_in_batch() -> None:
    api = FakeApi()
    client, s = make(api, dry_run=False)
    engine = TradingEngine(s, client, Views({"10": FairValue(0.60, 1.0)}))
    report = engine.run_cycle()
    assert report.traded and report.orders_submitted == 3

    methods = [(m, p) for m, p, _ in api.calls]
    assert ("POST", "/orders/cancel-all") in methods
    [legs_body] = [b for m, p, b in api.calls if p == "/orders/multi-leg"]
    legs = legs_body["legs"]  # type: ignore[index]
    # sum(bids) = 1.05 -> NO basket, qty limited by the thinner leg (40).
    assert [(leg["side"], leg["price"], leg["quantity"]) for leg in legs] == [
        ("no", 0.4, 40),
        ("no", 0.55, 40),
    ]
    assert all(leg["tournamentId"] == TID for leg in legs)
    assert legs_body["idempotencyKey"]  # type: ignore[index]

    [batch] = [b for m, p, b in api.calls if p == "/orders/batch"]
    [order] = batch["orders"]  # type: ignore[index]
    assert (order["exchangeId"], order["side"], order["action"], order["price"]) == (
        "10",
        "yes",
        "buy",
        0.45,
    )
    # Quarter Kelly: (0.15/0.55)*0.25 = 6.8% of equity, capped at the 5% market cap.
    assert order["quantity"] == int(5000 // 0.45)
    assert "expirationDate" in order


def test_dry_run_submits_nothing() -> None:
    api = FakeApi()
    client, s = make(api, dry_run=True)
    report = TradingEngine(s, client, Views({"10": FairValue(0.60, 1.0)})).run_cycle()
    assert report.orders_submitted == 3
    assert not [p for m, p, _ in api.calls if m in ("POST", "DELETE")]


def test_live_cycle_idle_outside_window() -> None:
    api = FakeApi(start="2029-01-01T00:00:00Z")
    client, s = make(api, dry_run=False)
    report = TradingEngine(s, client, Views({})).run_cycle()
    assert not report.traded and "opens" in report.skipped_reason
    assert not [p for m, p, _ in api.calls if m == "POST"]


def test_bulk_prescreen_fetches_only_markets_that_could_trade() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def book(eid: str, bid: float, ask: float) -> dict[str, Any]:
        return {
            "exchangeId": eid,
            "bids": [{"price": bid, "quantity": 100}],
            "asks": [{"price": ask, "quantity": 100}],
        }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        calls.append((path, dict(req.url.params)))
        if path == "/tournaments/midterm-elections":
            return httpx.Response(
                200,
                json={
                    "id": TID,
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
                        for m, e in (("M1", "10"), ("M2", "20"), ("M3", "30"))
                    ],
                    "pagination": {"hasMore": False, "nextCursor": None},
                },
            )
        if path == "/exchanges/prices":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"exchangeId": "10", "marketId": "M1", "bestBid": 0.40, "bestAsk": 0.45},
                        {"exchangeId": "20", "marketId": "M2", "bestBid": 0.48, "bestAsk": 0.50},
                    ],
                    "missingIds": ["30"],
                },
            )
        if path == "/markets/M1/orderbook":
            return httpx.Response(200, json={"exchanges": [book("10", 0.40, 0.45)]})
        if path == "/markets/M3/orderbook":
            return httpx.Response(200, json={"exchanges": [book("30", 0.50, 0.52)]})
        if path == "/relationships":
            return httpx.Response(200, json={"data": [], "pagination": {"hasMore": False}})
        if path == "/tournaments/midterm-elections/portfolio/positions":
            return httpx.Response(200, json={"positions": []})
        if path == "/tournaments/midterm-elections/leaderboard":
            return httpx.Response(200, json={"myRank": None})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": path}})

    s = Settings(api_key="k", _env_file=None, dry_run=True, cash_reserve=0.0)  # type: ignore[call-arg]
    client = SuperMarketClient(s, transport=httpx.MockTransport(handler))
    # 10: fair 0.60 vs ask 0.45 -> edge, fetched. 20: fair 0.49 sits inside
    # 0.48/0.50 -> no edge, skipped. 30: missing from bulk -> fetched to be safe.
    views = Views({"10": FairValue(0.60, 1.0), "20": FairValue(0.49, 1.0)})
    report = TradingEngine(s, client, views).run_cycle()

    fetched = {p for p, _ in calls if p.endswith("/orderbook")}
    assert fetched == {"/markets/M1/orderbook", "/markets/M3/orderbook"}
    [(_, prices)] = [(p, q) for p, q in calls if p == "/exchanges/prices"]
    assert prices["ids"] == "10,20,30" and prices["tournamentId"] == TID
    assert report.markets_scanned == 3 and report.orders_submitted == 1


def test_run_forever_cancels_orders_on_exit() -> None:
    api = FakeApi()
    client, s = make(api, dry_run=False, cancel_orders_on_exit=True, poll_interval=0.01)
    engine = TradingEngine(s, client, Views({}))

    cycles = 0
    orig = engine.run_cycle

    def one_cycle():  # type: ignore[no-untyped-def]
        nonlocal cycles
        cycles += 1
        result = orig()
        engine.stop()  # ask the loop to finish after this cycle
        return result

    engine.run_cycle = one_cycle  # type: ignore[method-assign]
    engine.run_forever()

    assert cycles == 1
    # cancel-all appears both at cycle start (stale orders) and on shutdown.
    cancels = [1 for m, p, _ in api.calls if p == "/orders/cancel-all"]
    assert len(cancels) >= 2


def test_run_forever_cancels_even_after_fatal_error() -> None:
    api = FakeApi()
    client, s = make(api, dry_run=False, cancel_orders_on_exit=True)
    engine = TradingEngine(s, client, Views({}))

    def boom():  # type: ignore[no-untyped-def]
        from supermarket_bot.client import AuthError

        raise AuthError(401, "INVALID_API_KEY", "bad key")

    engine.run_cycle = boom  # type: ignore[method-assign]
    engine.run_forever()  # returns on AuthError, then finally runs

    assert any(p == "/orders/cancel-all" for _, p, _ in api.calls)


def test_dry_run_shutdown_places_no_cancel() -> None:
    api = FakeApi()
    client, s = make(api, dry_run=True, cancel_orders_on_exit=True)
    engine = TradingEngine(s, client, Views({}))
    engine._on_shutdown()
    assert not [p for m, p, _ in api.calls if p == "/orders/cancel-all"]

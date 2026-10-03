"""Cross-market arbitrage from the platform's relationship graph (ALL).

``GET /relationships`` says how markets are logically linked. The engine's
``/relationships/constraints`` flags violations against *last traded*
prices, which you usually cannot trade at. So the bot uses the graph only for
structure and checks every basket against live order books: it trades only
when every leg is executable and the payoff is locked in whichever way the
election goes.

Supported relationship types (books are YES-perspective):

``implication`` (A => B, so P(A) <= P(B))
    When bid(A) > ask(B): buy YES B at ask(B) and NO A at 1 - bid(A).
    If A happens, B happens, so YES B pays 1. If A doesn't, NO A pays 1.
    Cost below 1, profit at least bid(A) - ask(B).

``monotonic`` (ordered thresholds such as "wins >= 3" => "wins >= 2")
    Each pair is an implication, but the order isn't stated in a form the
    bot can rely on. It is oriented by the engine's own suggested trades: the
    leg it says to sell is the antecedent, the leg it says to buy is the
    consequent. Then the implication check above is applied to the books.

``complementary`` (exactly one of A, B is true)
    YES basket when ask(A) + ask(B) < 1. NO basket when bid(A) + bid(B) > 1.

``mutually_exclusive`` (at most one member is true)
    NO basket when the bids sum above 1 (pays at least n - 1). A YES basket
    when the asks sum below 1, only if the engine marks it exhaustive.

Boolean and conditional types are reported by ``relationships`` but never
traded automatically.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import Action, OrderbookExchange, Side
from .signals import Leg, TradeCandidate

log = logging.getLogger(__name__)

TRADABLE_TYPES = {"implication", "monotonic", "complementary", "mutually_exclusive"}
MAX_LEGS = 10  # /orders/multi-leg limit


@dataclass
class Relationship:
    id: str
    type: str
    status: str
    is_exhaustive: bool
    exchange_ids: list[str]
    market_of: dict[str, str | None]
    roles: dict[str, str]
    titles: dict[str, str]
    from_ids: list[str] = field(default_factory=list)
    to_ids: list[str] = field(default_factory=list)
    label: str = ""

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Relationship:
        nodes = raw.get("nodes") or []
        direction = raw.get("direction") or {}
        ids = [str(n["exchangeId"]) for n in nodes if n.get("exchangeId")]
        return cls(
            id=str(raw["id"]),
            type=str(raw.get("type", "")),
            status=str(raw.get("status", "")),
            is_exhaustive=bool(raw.get("isExhaustive")),
            exchange_ids=ids,
            market_of={str(n["exchangeId"]): n.get("marketId") for n in nodes},
            roles={str(n["exchangeId"]): str(n.get("role", "")) for n in nodes},
            titles={
                str(n["exchangeId"]): " — ".join(
                    x for x in (n.get("marketTitle"), n.get("outcome")) if x
                )
                for n in nodes
            },
            from_ids=[str(x) for x in direction.get("fromExchangeIds") or []],
            to_ids=[str(x) for x in direction.get("toExchangeIds") or []],
            label=str(raw.get("label") or ""),
        )

    def describe(self) -> str:
        return self.label or " / ".join(self.titles.get(e, e) for e in self.exchange_ids)


@dataclass(frozen=True)
class SuggestedPair:
    """Engine hint for a monotonic violation: sell ``sell_id``, buy ``buy_id``."""

    sell_id: str
    buy_id: str


def parse_hints(constraints: Iterable[dict[str, Any]]) -> dict[str, SuggestedPair]:
    """Turn violated monotonic constraints into (antecedent, consequent) pairs.

    Only unambiguous hints are kept: exactly one leg to buy YES (or sell NO) and
    exactly one to sell YES (or buy NO).
    """
    out: dict[str, SuggestedPair] = {}
    for c in constraints:
        if c.get("type") != "monotonic" or c.get("evaluationStatus") != "violated":
            continue
        up, down = [], []
        for t in c.get("suggestedCorrectiveTrades") or []:
            side = str(t.get("outcomeSide", "")).lower()
            action = str(t.get("action", "")).lower()
            # Raising a YES price = buy YES or sell NO; lowering it = the reverse.
            raises = (side, action) in (("yes", "buy"), ("no", "sell"))
            lowers = (side, action) in (("yes", "sell"), ("no", "buy"))
            if raises:
                up.append(str(t["exchangeId"]))
            elif lowers:
                down.append(str(t["exchangeId"]))
        if len(up) == 1 and len(down) == 1 and up[0] != down[0]:
            out[str(c["relationshipId"])] = SuggestedPair(sell_id=down[0], buy_id=up[0])
    return out


class RelationshipArbitrage:
    def __init__(self, min_profit: float) -> None:
        self._min = min_profit

    def find(
        self,
        relationships: Iterable[Relationship],
        books: dict[str, OrderbookExchange],
        hints: dict[str, SuggestedPair],
    ) -> list[TradeCandidate]:
        out: list[TradeCandidate] = []
        for rel in relationships:
            if rel.status != "active" or rel.type not in TRADABLE_TYPES:
                continue
            ids = rel.exchange_ids
            if len(set(ids)) != len(ids) or not 2 <= len(ids) <= MAX_LEGS:
                continue
            if any(e not in books for e in ids):
                continue  # a member market is closed or not in this tournament
            cand = self._evaluate(rel, books, hints.get(rel.id))
            if cand is not None:
                log.info("REL-ARB %s %s: %s", rel.type, rel.id, cand.reason)
                out.append(cand)
        return out

    def _evaluate(
        self, rel: Relationship, books: dict[str, OrderbookExchange], hint: SuggestedPair | None
    ) -> TradeCandidate | None:
        if rel.type == "implication":
            if len(rel.from_ids) == 1 and len(rel.to_ids) == 1:
                return self._implication(rel, rel.from_ids[0], rel.to_ids[0], books)
            ante = [e for e, r in rel.roles.items() if r == "antecedent"]
            cons = [e for e, r in rel.roles.items() if r == "consequent"]
            if len(ante) == 1 and len(cons) == 1:
                return self._implication(rel, ante[0], cons[0], books)
            return None
        if rel.type == "monotonic":
            if hint and {hint.sell_id, hint.buy_id} <= set(rel.exchange_ids):
                return self._implication(rel, hint.sell_id, hint.buy_id, books)
            return None
        if rel.type == "complementary":
            if len(rel.exchange_ids) != 2:
                return None
            return self._basket(rel, books, yes_allowed=True)
        if rel.type == "mutually_exclusive":
            return self._basket(rel, books, yes_allowed=rel.is_exhaustive)
        return None

    def _candidate(
        self, rel: Relationship, legs: list[Leg], profit: float, why: str
    ) -> TradeCandidate | None:
        if profit < self._min or any(leg.available <= 0 for leg in legs):
            return None
        return TradeCandidate(
            kind="arbitrage",
            market_id=f"rel:{rel.id}",
            legs=legs,
            edge=profit,
            reason=f"{rel.type} {why} [{rel.describe()[:80]}]",
        )

    def _implication(
        self, rel: Relationship, ante: str, cons: str, books: dict[str, OrderbookExchange]
    ) -> TradeCandidate | None:
        bid_a, ask_b = books[ante].best_bid(), books[cons].best_ask()
        if bid_a is None or ask_b is None:
            return None
        legs = [
            Leg(cons, Side.YES, Action.BUY, ask_b.price, int(ask_b.quantity)),
            Leg(ante, Side.NO, Action.BUY, 1.0 - bid_a.price, int(bid_a.quantity)),
        ]
        return self._candidate(
            rel,
            legs,
            bid_a.price - ask_b.price,
            f"bid({ante})={bid_a.price:.3f} > ask({cons})={ask_b.price:.3f}",
        )

    def _basket(
        self, rel: Relationship, books: dict[str, OrderbookExchange], yes_allowed: bool
    ) -> TradeCandidate | None:
        ids = rel.exchange_ids
        best: TradeCandidate | None = None
        bids = [books[e].best_bid() for e in ids]
        if all(b is not None for b in bids):
            lv = [b for b in bids if b is not None]
            legs = [
                Leg(e, Side.NO, Action.BUY, 1.0 - b.price, int(b.quantity))
                for e, b in zip(ids, lv, strict=True)
            ]
            total = sum(b.price for b in lv)
            # Complementary: NO+NO pays exactly 1. Mutually exclusive: pays >= n-1.
            best = self._candidate(rel, legs, total - 1.0, f"NO basket sum(bids)={total:.3f}")
        if yes_allowed:
            asks = [books[e].best_ask() for e in ids]
            if all(a is not None for a in asks):
                lv = [a for a in asks if a is not None]
                legs = [
                    Leg(e, Side.YES, Action.BUY, a.price, int(a.quantity))
                    for e, a in zip(ids, lv, strict=True)
                ]
                total = sum(a.price for a in lv)
                yes = self._candidate(rel, legs, 1.0 - total, f"YES basket sum(asks)={total:.3f}")
                if yes and (best is None or yes.edge > best.edge):
                    best = yes
        return best

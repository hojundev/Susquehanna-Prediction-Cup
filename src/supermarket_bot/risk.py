"""Risk and position sizing.

Turns :class:`TradeCandidate` objects into sized :class:`OrderIntent` lists
while enforcing the safe-top-N guardrails. All fractions are of *equity*
(cash + market value of open positions):

* **Cash** — never spend below ``cash_reserve`` of equity.
* **Kelly target** — a mispricing position is sized to a fractional-Kelly
  *target*; existing holdings count toward it, so repeated cycles do not keep
  adding to the same view.
* **Per-race cap** — ``max_market_exposure`` (``max_arb_market_exposure`` for
  locked-profit baskets). Super Market lists each party's side of a race as a
  separate market ("Will the Democratic Party win X?" / "...Republican..."),
  so markets are grouped by :func:`race_key` and share one cap. Otherwise YES
  on one side plus NO on the other would double the bet on a single outcome.
* **Total cap** — ``max_total_exposure`` across all positions.
* **Correlation cap** — net D-vs-R exposure across exchanges tagged with a
  ``lean`` is bounded by ``max_correlated_exposure``.

Closing trades reduce risk, so they bypass every cap.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Settings
from .models import Action, OrderIntent, Position, Side
from .signals import TradeCandidate

log = logging.getLogger(__name__)

LeanLookup = Callable[[str], str | None]
GroupLookup = Callable[[str], str]

_PARTY = re.compile(
    r"\b(the\s+)?(democratic|republican|independent|libertarian|green)\s+party\b"
    r"|\b(democrats?|republicans?|gop)\b",
    re.IGNORECASE,
)


def race_key(title: str) -> str:
    """The race a market belongs to, with the party removed.

    "Will the Democratic Party win the U.S. House?" and "Will the Republican
    Party win the U.S. House?" both become "will win the u s house".
    """
    t = _PARTY.sub(" ", title.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _same(market_id: str) -> str:
    return market_id


def _shares(budget: float, unit_cost: float) -> int:
    """Whole shares affordable with ``budget`` (tolerant of float noise)."""
    if budget <= 0 or unit_cost <= 0:
        return 0
    return int(budget / unit_cost + 1e-9)


def _lean_sign(lean: str | None) -> int:
    """+1 if YES on this exchange favours D, -1 if R, 0 if untagged."""
    return {"D": 1, "R": -1}.get(lean or "", 0)


@dataclass
class ExposureState:
    """Capital and exposure snapshot, updated as trades are approved."""

    cash: float
    equity: float
    per_market_cost: dict[str, float] = field(default_factory=dict)
    #: Signed cost per exchange: +YES cost, -NO cost.
    per_exchange_cost: dict[str, float] = field(default_factory=dict)
    #: Buy-and-hold cost only; checked against ``max_total_exposure``.
    total_cost: float = 0.0
    #: Maker inventory plus resting maker quotes; checked against ``maker_budget``.
    maker_cost: float = 0.0
    #: Net exposure to a Democratic-favourable environment (negative = R).
    net_d: float = 0.0
    spent: float = 0.0

    @classmethod
    def from_positions(
        cls,
        cash: float,
        positions: list[Position],
        lean_of: LeanLookup,
        group_of: GroupLookup = _same,
        maker_exchanges: set[str] | frozenset[str] = frozenset(),
    ) -> ExposureState:
        """Build from open positions.

        Positions in ``maker_exchanges`` count toward ``maker_cost`` rather
        than ``total_cost``. They still count toward the per-race and D-vs-R
        caps, because the risk is the same whichever strategy holds them.
        """
        live = [p for p in positions if not p.settled and p.side is not None]
        equity = cash + sum(max(p.market_value, 0.0) for p in live)
        state = cls(cash=cash, equity=equity)
        for p in live:
            assert p.side is not None
            maker = p.exchange_id in maker_exchanges
            cost = abs(p.cost_basis)
            state._record(
                group_of(p.market_id), p.exchange_id, p.side, cost, lean_of, count_total=not maker
            )
            if maker:
                state.maker_cost += cost
        return state

    def _record(
        self,
        market_id: str,
        exchange_id: str,
        side: Side,
        cost: float,
        lean_of: LeanLookup,
        *,
        count_total: bool = True,
    ) -> None:
        self.per_market_cost[market_id] = self.per_market_cost.get(market_id, 0.0) + cost
        self.per_exchange_cost[exchange_id] = (
            self.per_exchange_cost.get(exchange_id, 0.0) + side.sign * cost
        )
        if count_total:
            self.total_cost += cost
        self.net_d += side.sign * _lean_sign(lean_of(exchange_id)) * cost

    def reserve(
        self, market_id: str, exchange_id: str, side: Side, cost: float, lean_of: LeanLookup
    ) -> None:
        self._record(market_id, exchange_id, side, cost, lean_of)
        self.spent += cost

    def reserve_maker(
        self, market_id: str, exchange_id: str, side: Side, cost: float, lean_of: LeanLookup
    ) -> None:
        """Reserve capital for a resting maker quote as if it had filled."""
        self._record(market_id, exchange_id, side, cost, lean_of, count_total=False)
        self.maker_cost += cost
        self.spent += cost


@dataclass
class SizedTrade:
    kind: str
    intents: list[OrderIntent]
    #: True when all legs must be placed atomically (arbitrage baskets).
    atomic: bool = False


class RiskManager:
    def __init__(
        self, settings: Settings, lean_of: LeanLookup, group_of: GroupLookup = _same
    ) -> None:
        self._s = settings
        self._lean_of = lean_of
        self._group = group_of

    # -- headroom --------------------------------------------------------
    def _spendable(self, state: ExposureState) -> float:
        return state.cash - state.spent - self._s.cash_reserve * state.equity

    def _headroom(self, market_id: str, state: ExposureState, market_cap: float) -> float:
        eq = state.equity
        return min(
            market_cap * eq - state.per_market_cost.get(self._group(market_id), 0.0),
            self._s.max_total_exposure * eq - state.total_cost,
            self._spendable(state),
        )

    def _correlation_headroom(self, exchange_id: str, side: Side, state: ExposureState) -> float:
        direction = side.sign * _lean_sign(self._lean_of(exchange_id))
        if direction == 0:
            return float("inf")
        limit = self._s.max_correlated_exposure * state.equity
        return limit - direction * state.net_d

    # Public views used by the market maker, which has its own budget but
    # shares the cash reserve and the per-race and D-vs-R caps.
    def spendable(self, state: ExposureState) -> float:
        return self._spendable(state)

    def race_headroom(self, market_id: str, state: ExposureState) -> float:
        cap = self._s.max_market_exposure * state.equity
        return cap - state.per_market_cost.get(self._group(market_id), 0.0)

    def correlation_headroom(self, exchange_id: str, side: Side, state: ExposureState) -> float:
        return self._correlation_headroom(exchange_id, side, state)

    # -- sizing ----------------------------------------------------------
    def kelly_fraction(self, cand: TradeCandidate, scale: float = 1.0) -> float:
        """Fractional Kelly for buying a binary contract.

        Buying at side price ``p`` a contract that pays 1 with probability
        ``q``: f* = (q - p) / (1 - p) = edge / (1 - p).
        """
        p = cand.legs[0].price
        if p >= 1.0 or cand.edge <= 0:
            return 0.0
        f = cand.edge / (1.0 - p) * self._s.kelly_fraction * cand.confidence * scale
        return max(0.0, min(f, 1.0))

    def size(
        self, cand: TradeCandidate, state: ExposureState, kelly_scale: float = 1.0
    ) -> SizedTrade | None:
        """Size a candidate, reserving its capital in ``state``. None if blocked."""
        if cand.kind == "close":
            return self._size_close(cand)
        if cand.kind == "arbitrage":
            return self._size_arb(cand, state)
        return self._size_open(cand, state, kelly_scale)

    def _size_close(self, cand: TradeCandidate) -> SizedTrade | None:
        leg = cand.legs[0]
        qty = min(cand.held, leg.available)
        if qty <= 0:
            return None
        intent = OrderIntent(
            exchange_id=leg.exchange_id,
            side=leg.side,
            action=Action.SELL,
            quantity=qty,
            price=leg.price,
            reason=f"close: {cand.reason}",
        )
        log.info(
            "SIZE close sell %s qty=%d @%.3f (%s)", leg.side.value, qty, leg.price, cand.reason
        )
        return SizedTrade("close", [intent])

    def _size_arb(self, cand: TradeCandidate, state: ExposureState) -> SizedTrade | None:
        cost = cand.basket_cost
        if cost <= 0:
            return None
        budget = self._headroom(cand.market_id, state, self._s.max_arb_market_exposure)
        qty = min(min(leg.available for leg in cand.legs), _shares(budget, cost))
        if qty <= 0:
            log.debug("Skip arb %s: budget %.2f, basket %.3f", cand.market_id, budget, cost)
            return None
        intents = []
        for leg in cand.legs:
            state.reserve(
                self._group(cand.market_id),
                leg.exchange_id,
                leg.side,
                qty * leg.price,
                self._lean_of,
            )
            intents.append(
                OrderIntent(
                    exchange_id=leg.exchange_id,
                    side=leg.side,
                    action=Action.BUY,
                    quantity=qty,
                    price=leg.price,
                    reason=f"arbitrage: {cand.reason}",
                )
            )
        log.info(
            "SIZE arb %s baskets=%d cost=%.2f locked_profit=%.2f",
            cand.market_id,
            qty,
            qty * cost,
            qty * cand.edge,
        )
        return SizedTrade("arbitrage", intents, atomic=True)

    def _size_open(
        self, cand: TradeCandidate, state: ExposureState, kelly_scale: float
    ) -> SizedTrade | None:
        leg = cand.legs[0]
        f = self.kelly_fraction(cand, kelly_scale)
        if f <= 0:
            return None
        # Near-certain markets get a larger per-race cap: the side being bought
        # has a fair value >= the high-confidence threshold.
        side_fair = cand.fair.probability if cand.fair else 0.0
        if leg.side is Side.NO and cand.fair:
            side_fair = 1.0 - cand.fair.probability
        market_cap = self._s.max_market_exposure
        if cand.fair and side_fair >= self._s.high_conf_threshold:
            market_cap = max(market_cap, self._s.high_conf_market_cap)
        held_same_side = max(0.0, leg.side.sign * state.per_exchange_cost.get(leg.exchange_id, 0.0))
        budget = min(
            f * state.equity - held_same_side,
            self._headroom(cand.market_id, state, market_cap),
            self._correlation_headroom(leg.exchange_id, leg.side, state),
        )
        qty = min(leg.available, _shares(budget, leg.price))
        if qty <= 0:
            log.debug("Skip open %s: budget %.2f", leg.exchange_id, budget)
            return None
        cost = qty * leg.price
        state.reserve(self._group(cand.market_id), leg.exchange_id, leg.side, cost, self._lean_of)
        log.info(
            "SIZE open buy %s %s qty=%d @%.3f cost=%.2f edge=%.3f kelly=%.4f (%s)",
            leg.side.value,
            leg.exchange_id,
            qty,
            leg.price,
            cost,
            cand.edge,
            f,
            cand.reason,
        )
        return SizedTrade(
            "open",
            [
                OrderIntent(
                    exchange_id=leg.exchange_id,
                    side=leg.side,
                    action=Action.BUY,
                    quantity=qty,
                    price=leg.price,
                    reason=f"open: {cand.reason}",
                )
            ],
        )

"""Typed models mirroring the Super Market API v1 response shapes.

Only the fields the bot uses are modelled; ``extra="ignore"`` keeps us
resilient to additional fields. Aliases map the API's camelCase to snake_case.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

#: Limit prices must sit on this tick, within [MIN_PRICE, MAX_PRICE].
TICK = 0.005
MIN_PRICE = 0.005
MAX_PRICE = 0.995


def snap_price(price: float, *, up: bool) -> float:
    """Snap a side-relative limit price onto the 0.005 tick.

    Buys round *down* (never pay more than intended); sells round *up* (never
    accept less). The result is clamped to the valid limit range.
    """
    steps = price / TICK
    steps = math.floor(steps + 1e-9) if not up else math.ceil(steps - 1e-9)
    return round(min(MAX_PRICE, max(MIN_PRICE, steps * TICK)), 3)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class _ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Side(str, Enum):
    YES = "yes"
    NO = "no"

    @property
    def sign(self) -> int:
        return 1 if self is Side.YES else -1

    @property
    def opposite(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class MarketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"


# --------------------------------------------------------------------------
# Tournament
# --------------------------------------------------------------------------
class TournamentSummary(_ApiModel):
    id: str
    slug: str
    name: str
    status: str
    start_date: str | None = Field(default=None, alias="startDate")
    end_date: str | None = Field(default=None, alias="endDate")
    initial_balance: float = Field(alias="initialBalance")
    currency_name: str = Field(alias="currencyName")
    my_balance: float = Field(alias="myBalance")
    is_pending_enrolment: bool = Field(default=False, alias="isPendingEnrolment")

    def trading_open(self, now: datetime) -> tuple[bool, str]:
        """Whether orders can be placed right now, with a reason if not."""
        if self.status.lower() in ("draft", "ended"):
            return False, f"tournament status is {self.status}"
        start, end = parse_ts(self.start_date), parse_ts(self.end_date)
        if start and now < start:
            return False, f"trading opens at {start.isoformat()}"
        if end and now >= end:
            return False, f"trading closed at {end.isoformat()}"
        return True, ""


# --------------------------------------------------------------------------
# Markets / orderbook
# --------------------------------------------------------------------------
class Exchange(_ApiModel):
    id: str
    option: str | None = None
    latest_price: float | None = Field(default=None, alias="latestPrice")


class Market(_ApiModel):
    id: str
    title: str
    status: MarketStatus
    settlement_date: str | None = Field(default=None, alias="settlementDate")
    categories: list[str] = Field(default_factory=list)
    is_composite: bool = Field(default=False, alias="isComposite")
    is_multi_outcome: bool = Field(default=False, alias="isMultiOutcome")
    exchanges: list[Exchange] = Field(default_factory=list)


class BulkExchangePrice(_ApiModel):
    """Top of book for one exchange from ``GET /exchanges/prices`` (YES terms)."""

    exchange_id: str = Field(alias="exchangeId")
    market_id: str | None = Field(default=None, alias="marketId")
    best_bid: float | None = Field(default=None, alias="bestBid")
    best_ask: float | None = Field(default=None, alias="bestAsk")


class OrderbookLevel(_ApiModel):
    price: float
    quantity: float


class OrderbookExchange(_ApiModel):
    """YES-perspective book: bids sorted descending, asks ascending."""

    exchange_id: str = Field(alias="exchangeId")
    option: str | None = None
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)

    def best_bid(self) -> OrderbookLevel | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> OrderbookLevel | None:
        return self.asks[0] if self.asks else None


class MarketOrderbook(_ApiModel):
    exchanges: list[OrderbookExchange] = Field(default_factory=list)
    #: Sum of best asks across outcomes (null if any outcome has no price).
    overround: float | None = None
    has_arbitrage_opportunity: bool = Field(default=False, alias="hasArbitrageOpportunity")


# --------------------------------------------------------------------------
# Positions / leaderboard
# --------------------------------------------------------------------------
class Position(_ApiModel):
    exchange_id: str = Field(alias="exchangeId")
    market_id: str = Field(alias="marketId")
    market_title: str = Field(alias="marketTitle")
    option: str | None = None
    settled: bool = False
    #: Signed: positive = YES shares, negative = NO shares.
    quantity: float
    avg_cost: float = Field(default=0.0, alias="avgCost")
    current_price: float | None = Field(default=None, alias="currentPrice")
    market_value: float = Field(default=0.0, alias="marketValue")
    cost_basis: float = Field(default=0.0, alias="costBasis")
    unrealized_pnl: float = Field(default=0.0, alias="unrealizedPnl")

    @property
    def side(self) -> Side | None:
        if self.quantity > 0:
            return Side.YES
        if self.quantity < 0:
            return Side.NO
        return None


class LeaderboardEntry(_ApiModel):
    rank: int
    profile_id: str = Field(alias="profileId")
    username: str | None = None
    pnl: float = 0.0
    trades_count: int = Field(default=0, alias="tradesCount")


# --------------------------------------------------------------------------
# Order placement
# --------------------------------------------------------------------------
class OrderIntent(BaseModel):
    """A sized order ready to submit.

    ``price`` is a **side-relative** limit price (a NO buy at 0.30 means
    paying 0.30 per NO share), matching the API. It is always a limit order;
    the bot never sends market orders.
    """

    exchange_id: str
    side: Side
    action: Action
    quantity: int = Field(gt=0, le=2_147_483_647)
    price: float = Field(ge=MIN_PRICE, le=MAX_PRICE)
    reason: str = ""

    def to_payload(self, tournament_id: str, ttl_seconds: int = 0) -> dict[str, object]:
        """Build an ``OrderInput`` body (idempotency key is added by the client)."""
        payload: dict[str, object] = {
            "exchangeId": self.exchange_id,
            "side": self.side.value,
            "action": self.action.value,
            "quantity": self.quantity,
            "price": snap_price(self.price, up=self.action is Action.SELL),
            "tournamentId": tournament_id,
        }
        if ttl_seconds > 0:
            expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            payload["expirationDate"] = expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return payload

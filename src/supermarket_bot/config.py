"""Runtime configuration, loaded from environment / .env.

All tunables live here so strategy behaviour can be adjusted without code
changes. Defaults are tuned for a *safe top-N finish*: modest edges, small
fractional-Kelly sizing, a cash reserve, and hard exposure caps.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bot configuration.

    Values are read from environment variables prefixed with ``SUPERMARKET_``
    (see ``.env.example``). Fractions are relative to *equity* (cash plus the
    market value of open positions).
    """

    model_config = SettingsConfigDict(
        env_prefix="SUPERMARKET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API ---
    api_key: str = Field(default="", description="Super Market API key.")
    base_url: str = Field(default="https://sig.thesuper.market/api/v1")
    tournament_slug: str = Field(default="midterm-elections")

    # --- Safety ---
    dry_run: bool = Field(
        default=True,
        description="When true, orders are computed and logged but never submitted.",
    )
    cancel_orders_on_exit: bool = Field(
        default=True,
        description=(
            "When `run` stops (Ctrl-C, error, or clean stop), cancel all resting orders so "
            "no unmatched bids/asks are left on the book while the bot is off. Positions are kept."
        ),
    )
    cancel_stale_orders: bool = Field(
        default=True,
        description=(
            "Cancel all open orders in the tournament at the start of each live cycle so "
            "unfilled remainders never accumulate. This also cancels orders you placed by hand."
        ),
    )
    order_ttl_seconds: int = Field(
        default=300,
        ge=0,
        description="Limit orders expire after this many seconds (0 = no expiration).",
    )

    # --- Mispricing strategy ---
    min_edge: float = Field(
        default=0.07,
        ge=0.0,
        le=1.0,
        description="Minimum fair-value edge per share to open a mispricing trade.",
    )
    max_edge: float = Field(
        default=0.25,
        gt=0.0,
        le=1.0,
        description=(
            "Skip an open when fair value and the best price differ by more than this. "
            "A gap that large usually means a bad mapping or stale data, not an opportunity."
        ),
    )
    take_profit: bool = Field(
        default=True,
        description="Close a position once the market reaches your fair value.",
    )
    kelly_fraction: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Fraction of full-Kelly stake to use (lower = safer).",
    )

    # --- External fair values (Kalshi / Polymarket, read-only) ---
    mappings_path: str = Field(default="mappings.json")
    use_kalshi: bool = Field(default=True)
    use_polymarket: bool = Field(default=True)
    external_max_spread: float = Field(
        default=0.08,
        gt=0.0,
        le=1.0,
        description="Ignore an external quote whose bid-ask spread is wider than this.",
    )
    external_max_disagreement: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Skip an exchange when Kalshi and Polymarket mids differ by more than this.",
    )
    external_require_both: bool = Field(
        default=False,
        description="Only trade exchanges where every mapped source has a usable quote.",
    )
    external_single_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    external_dual_confidence: float = Field(default=0.8, ge=0.0, le=1.0)

    # --- Arbitrage strategy ---
    min_arb_profit: float = Field(
        default=0.01,
        ge=0.0,
        description="Minimum locked profit per basket (SUSQies) to take an arbitrage.",
    )
    allow_yes_basket_arb: bool = Field(
        default=False,
        description=(
            "Buy YES on every outcome when the asks sum below 1. Only safe when the "
            "outcomes are exhaustive (exactly one must win), so it is off by default."
        ),
    )

    use_relationship_arb: bool = Field(
        default=True,
        description="Look for locked-profit baskets across logically linked markets.",
    )
    relationship_refresh_cycles: int = Field(
        default=10, ge=1, description="Re-read the relationship graph every N cycles."
    )

    # --- Risk caps (fractions of equity) ---
    max_market_exposure: float = Field(default=0.05, gt=0.0, le=1.0)
    max_arb_market_exposure: float = Field(default=0.10, gt=0.0, le=1.0)
    max_total_exposure: float = Field(default=0.60, gt=0.0, le=1.0)
    max_correlated_exposure: float = Field(
        default=0.30,
        gt=0.0,
        le=1.0,
        description="Max net D-vs-R exposure across positions tagged with a `lean`.",
    )
    cash_reserve: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Fraction of equity always kept as uncommitted cash.",
    )

    # --- Leaderboard pacing ---
    defend_rank: int = Field(
        default=0,
        ge=0,
        description="When your rank is at or better than this, scale sizing down (0 = off).",
    )
    defend_kelly_scale: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- Loop ---
    poll_interval: float = Field(default=300.0, gt=0.0)
    http_timeout: float = Field(default=20.0, gt=0.0)
    orderbook_depth: int = Field(default=20, ge=1, le=200)
    bulk_price_scan: bool = Field(
        default=True,
        description=(
            "Read best bid/ask for every market in a few bulk requests, then fetch full "
            "orderbooks only for markets that could trade. Off = fetch every orderbook."
        ),
    )
    scan_concurrency: int = Field(
        default=6,
        ge=1,
        le=64,
        description="How many orderbook requests to run at once when scanning markets.",
    )

    # --- Logging ---
    log_level: str = Field(default="INFO")

    def require_api_key(self) -> str:
        """Return the API key or raise if it is missing."""
        if not self.api_key:
            raise RuntimeError("No API key configured. Set SUPERMARKET_API_KEY in your .env file.")
        return self.api_key


def load_settings() -> Settings:
    """Load settings from environment / .env."""
    return Settings()

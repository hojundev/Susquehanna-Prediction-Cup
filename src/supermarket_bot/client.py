"""HTTP client for the Super Market API v1.

Every price / orderbook / order call is scoped to one tournament via
``tournamentId`` so all reads and writes resolve the same isolated context.

Retries: 429 and 5xx are retried with backoff (honouring ``Retry-After``).
Order placement is safe to retry because every placement carries one
client-generated ``idempotencyKey`` that is reused across retries.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .models import (
    BulkExchangePrice,
    Market,
    MarketOrderbook,
    OrderIntent,
    Position,
    TournamentSummary,
)

log = logging.getLogger(__name__)

_MAX_PAGES = 100


class ApiError(RuntimeError):
    """API failure with HTTP status, stable error ``code`` and parsed body."""

    def __init__(self, status: int, code: str | None, message: str, body: Any = None) -> None:
        self.status = status
        self.code = code
        self.body = body
        super().__init__(f"[{status}] {code or ''} {message}".strip())


class TermsNotAcknowledgedError(ApiError):
    """403 TERMS_NOT_ACKNOWLEDGED: accept the current documents on the site."""


class ResidenceUpdateRequiredError(ApiError):
    """RESIDENCE_UPDATE_REQUIRED: update where you live on the site."""


class AuthError(ApiError):
    """Key missing, invalid, revoked, expired, banned or lacking scope."""


class TransientApiError(ApiError):
    """429 / 5xx that survived all retries."""


_AUTH_CODES = {
    "MISSING_API_KEY",
    "INVALID_API_KEY",
    "API_KEY_REVOKED",
    "API_KEY_EXPIRED",
    "ACCOUNT_BANNED",
}


class SuperMarketClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._tournament_id: str | None = None
        # Separate connect / read timeouts so one slow market can't hang the whole
        # scan: a stalled read errors after http_timeout and that market is skipped.
        timeout = httpx.Timeout(settings.http_timeout, connect=min(10.0, settings.http_timeout))
        limits = httpx.Limits(max_connections=settings.scan_concurrency + 4)
        self._client = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            timeout=timeout,
            limits=limits,
            transport=transport,
            headers={
                "Authorization": f"Bearer {settings.require_api_key()}",
                "Accept": "application/json",
                "User-Agent": "supermarket-bot/0.3",
            },
        )

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SuperMarketClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport -------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((TransientApiError, httpx.TransportError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        resp = self._client.request(method, path, params=params, json=json)
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                with contextlib.suppress(ValueError):
                    time.sleep(min(float(retry_after), 30.0))
            log.warning("Transient %s on %s %s; retrying", resp.status_code, method, path)
            raise TransientApiError(resp.status_code, None, resp.text[:300])
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _raise_for_error(resp: httpx.Response) -> None:
        code: str | None = None
        message = resp.text[:500]
        body: Any = None
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message", message)
        except ValueError:
            pass
        status = resp.status_code
        if code == "TERMS_NOT_ACKNOWLEDGED":
            raise TermsNotAcknowledgedError(status, code, message, body)
        if code == "RESIDENCE_UPDATE_REQUIRED":
            raise ResidenceUpdateRequiredError(status, code, message, body)
        if status == 401 or code in _AUTH_CODES:
            raise AuthError(status, code, message, body)
        raise ApiError(status, code, message, body)

    # -- tournament scope ------------------------------------------------
    def resolve_tournament(self) -> TournamentSummary:
        """Fetch the configured tournament (balance, window) and cache its UUID."""
        payload = self._request("GET", f"/tournaments/{self._settings.tournament_slug}")
        summary = TournamentSummary.model_validate(payload)
        self._tournament_id = summary.id
        log.debug("Tournament %s id=%s balance=%.2f", summary.slug, summary.id, summary.my_balance)
        return summary

    def list_tournaments(self) -> list[TournamentSummary]:
        """Every tournament this key can access (any status), for finding the slug."""
        out: list[TournamentSummary] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            payload = self._request(
                "GET", "/tournaments", params={"status": "any", "limit": 100, "offset": offset}
            )
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            out.extend(TournamentSummary.model_validate(t) for t in rows)
            page = payload.get("pagination") or {}
            if not rows or not page.get("hasMore"):
                break
            offset += len(rows)
        return out

    @property
    def tournament_id(self) -> str:
        if self._tournament_id is None:
            self.resolve_tournament()
        assert self._tournament_id is not None
        return self._tournament_id

    def _slug_path(self, suffix: str) -> str:
        return f"/tournaments/{self._settings.tournament_slug}{suffix}"

    # -- market data -----------------------------------------------------
    def list_markets(self, status: str = "open") -> list[Market]:
        """All tournament markets with the given status (cursor-paginated)."""
        markets: list[Market] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"status": status, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            payload = self._request("GET", self._slug_path("/markets"), params=params)
            markets.extend(Market.model_validate(m) for m in payload.get("data", []))
            page = payload.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                break
        return markets

    def get_orderbook(self, market_id: str, depth: int = 20) -> MarketOrderbook:
        """YES-perspective book for every exchange in the market, in our tournament."""
        tid = self.tournament_id
        payload = self._request(
            "GET",
            f"/markets/{market_id}/orderbook",
            params={"tournamentId": tid, "depth": depth},
        )
        for ctx in payload.get("contexts") or []:
            tournament = ctx.get("tournament") or {}
            if tournament.get("id") == tid and ctx.get("orderbook"):
                return MarketOrderbook.model_validate(ctx["orderbook"])
        return MarketOrderbook.model_validate(payload)

    def bulk_prices(self, exchange_ids: list[str]) -> tuple[dict[str, BulkExchangePrice], set[str]]:
        """Best bid/ask for many exchanges, 100 per request.

        Returns ``(quotes by exchange id, ids the API reported as missing)``.
        """
        quotes: dict[str, BulkExchangePrice] = {}
        missing: set[str] = set()
        ids = list(dict.fromkeys(exchange_ids))
        for i in range(0, len(ids), 100):
            payload = self._request(
                "GET",
                "/exchanges/prices",
                params={"ids": ",".join(ids[i : i + 100]), "tournamentId": self.tournament_id},
            )
            for raw in payload.get("data") or []:
                q = BulkExchangePrice.model_validate(raw)
                quotes[q.exchange_id] = q
            missing |= {str(x) for x in payload.get("missingIds") or []}
        return quotes, missing

    def get_orderbooks(
        self,
        market_ids: list[str],
        depth: int = 20,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, MarketOrderbook]:
        """Fetch many orderbooks concurrently; skip any that fail or time out.

        The Super Market orderbook endpoint is slow and uneven (sub-second to
        20s+), so fetching 200+ markets one at a time takes many minutes.
        Running a bounded thread pool keeps a full scan to seconds while a
        single stalled request only costs that one market.
        """
        out: dict[str, MarketOrderbook] = {}
        total = len(market_ids)
        done = 0
        workers = max(1, self._settings.scan_concurrency)

        def fetch(mid: str) -> tuple[str, MarketOrderbook | None]:
            try:
                return mid, self.get_orderbook(mid, depth)
            except Exception as exc:  # noqa: BLE001 - skip a bad book, keep scanning
                log.warning("Orderbook fetch failed for %s: %s", mid, exc)
                return mid, None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for mid, book in pool.map(fetch, market_ids):
                done += 1
                if book is not None:
                    out[mid] = book
                if on_progress and (done % 25 == 0 or done == total):
                    on_progress(done, total)
        return out

    # -- portfolio -------------------------------------------------------
    def get_positions(self) -> list[Position]:
        payload = self._request("GET", self._slug_path("/portfolio/positions"))
        return [Position.model_validate(p) for p in payload.get("positions", [])]

    def my_rank(self) -> int | None:
        payload = self._request(
            "GET", self._slug_path("/leaderboard"), params={"period": "all", "limit": 1}
        )
        rank = payload.get("myRank") if isinstance(payload, dict) else None
        return int(rank) if rank is not None else None

    # -- orders ----------------------------------------------------------
    @staticmethod
    def _key(prefix: str) -> str:
        return f"smb-{prefix}-{uuid.uuid4()}"

    def place_order(self, intent: OrderIntent, ttl_seconds: int = 0) -> dict[str, Any]:
        body = intent.to_payload(self.tournament_id, ttl_seconds)
        body["idempotencyKey"] = self._key("one")
        log.info("Placing order: %s", body)
        result: dict[str, Any] = self._request("POST", "/orders", json=body)
        return result

    def place_batch(self, intents: list[OrderIntent], ttl_seconds: int = 0) -> list[dict[str, Any]]:
        """Best-effort batch (<= 50). Returns per-item ``{index, ok, status, data}``."""
        if not 0 < len(intents) <= 50:
            raise ValueError("Batch must contain 1-50 orders")
        body = {
            "idempotencyKey": self._key("batch"),
            "orders": [i.to_payload(self.tournament_id, ttl_seconds) for i in intents],
        }
        log.info("Placing batch of %d orders", len(intents))
        try:
            payload = self._request("POST", "/orders/batch", json=body)
        except ApiError as exc:
            # 422 (all failed terminally) still carries per-item results.
            if isinstance(exc.body, dict) and isinstance(exc.body.get("results"), list):
                return list(exc.body["results"])
            raise
        return list(payload.get("results", []))

    def place_multi_leg(
        self, intents: list[OrderIntent], ttl_seconds: int = 0
    ) -> list[dict[str, Any]]:
        """Atomic placement (<= 10 legs): all legs are accepted or none are."""
        if not 0 < len(intents) <= 10:
            raise ValueError("Multi-leg order must contain 1-10 legs")
        body = {
            "idempotencyKey": self._key("legs"),
            "legs": [i.to_payload(self.tournament_id, ttl_seconds) for i in intents],
        }
        log.info("Placing %d-leg atomic order", len(intents))
        payload = self._request("POST", "/orders/multi-leg", json=body)
        return [r.get("data", {}) for r in payload.get("results", [])]

    # -- relationships (ALL) ---------------------------------------------
    def list_relationships(self) -> list[dict[str, Any]]:
        """All relationships in this tournament's context (cursor-paginated)."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"tournamentId": self.tournament_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = self._request("GET", "/relationships", params=params)
            out.extend(payload.get("data", []))
            page = payload.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                break
        return out

    def get_constraints(self, violations_only: bool = True) -> list[dict[str, Any]]:
        params = {
            "tournamentId": self.tournament_id,
            "violationsOnly": "true" if violations_only else "false",
            "minViolation": 0.0,
        }
        payload = self._request("GET", "/relationships/constraints", params=params)
        return list(payload.get("data", []))

    def cancel_order(self, order_id: int) -> None:
        self._request("DELETE", f"/orders/{order_id}")

    def cancel_all(self) -> int:
        """Cancel every open order of ours in this tournament."""
        payload = self._request(
            "POST", "/orders/cancel-all", json={"tournamentId": self.tournament_id}
        )
        return int((payload or {}).get("cancelled", 0))

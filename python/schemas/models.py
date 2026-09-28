"""Shared contract types for the MadunaCapital arbitrage scanner.

Every service (ingestion, engine, api) imports these instead of defining
its own copy. A change here is a deliberate version bump other repos pull
in, not a silent drift between services.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MarketOdds(BaseModel):
    """Decimal odds for a single market on a single bookmaker."""

    home_odds: float | None = None
    away_odds: float | None = None
    draw_odds: float | None = None


class OddsEvent(BaseModel):
    """The universal schema every scraper must normalize its payload into.

    `event_id` is deliberately optional: a scraper reports raw team names as
    scraped, and cannot compute the MD5 hash itself because that requires
    entity resolution (mapping "K. Chiefs" -> the universal team id), which
    only the normalization engine can do. Ingestion leaves it unset; the
    engine fills it in before publishing downstream.
    """

    event_id: str | None = Field(
        default=None, description="MD5 hash of home+away+start_time_utc, set by the engine, not the scraper"
    )
    sport: str
    league: str
    home_team: str
    away_team: str
    start_time: datetime
    bookmaker: str
    markets: dict[str, MarketOdds]
    scraped_at: datetime


class ArbStake(BaseModel):
    outcome: str
    bookmaker: str
    odds: float
    implied_probability: float
    stake: float
    expected_payout: float


class ArbEvent(BaseModel):
    """Published on `live_arbitrage_alerts` when the detection engine finds a match margin < 100%."""

    id: str
    event_id: str
    match: str
    sport: str
    market: str
    margin: float
    roi_percent: float
    stakes: list[ArbStake]
    expires_at: int = Field(..., description="Unix timestamp in milliseconds")
    type: Literal["arb_new", "arb_update"] = "arb_new"


class ArbExpiredEvent(BaseModel):
    id: str
    reason: Literal["odds_moved", "market_suspended", "market_voided", "ttl_elapsed"]
    type: Literal["arb_expired"] = "arb_expired"


class ScraperHeartbeat(BaseModel):
    """Written to `heartbeat:{bookmaker_id}` (see heartbeat.py) by
    scanner-ingestion's run_scraper_loop after every poll cycle it observes.
    Read by scanner-api's /api/health/scrapers for the frontend's status page.
    """

    bookmaker_id: str
    status: Literal["ok", "error"]
    timestamp: datetime
    events_published_this_cycle: int = 0
    error_message: str | None = None


class EngineHeartbeat(BaseModel):
    """Written to `heartbeat:engine` (see heartbeat.py) by scanner-engine's
    runner.py. Reports the health of the whole downstream pipeline -- Redis
    connectivity, whether a Postgres write has actually succeeded recently,
    and basic throughput -- not just "is the process alive". Read by
    scanner-api's /api/health/system for the frontend's status page.
    """

    status: Literal["ok", "error"]
    timestamp: datetime
    redis_connected: bool
    postgres_connected: bool | None = Field(
        default=None, description="None means no database was configured for this run"
    )
    messages_processed_total: int = 0
    last_db_write_at: datetime | None = None
    error_message: str | None = None

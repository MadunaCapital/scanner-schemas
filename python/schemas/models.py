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
    """The universal schema every scraper must normalize its payload into."""

    event_id: str = Field(..., description="MD5 hash of home+away+start_time_utc")
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

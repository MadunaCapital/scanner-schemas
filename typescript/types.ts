// Mirrors python/schemas/models.py — keep these in sync manually until
// a codegen step is worth the setup cost.

export interface MarketOdds {
  home_odds?: number;
  away_odds?: number;
  draw_odds?: number;
}

export interface OddsEvent {
  event_id: string;
  sport: string;
  league: string;
  home_team: string;
  away_team: string;
  start_time: string; // ISO 8601 UTC
  bookmaker: string;
  markets: Record<string, MarketOdds>;
  scraped_at: string;
}

export interface ArbStake {
  outcome: string;
  bookmaker: string;
  odds: number;
  implied_probability: number;
  stake: number;
  expected_payout: number;
}

export interface ArbEvent {
  id: string;
  event_id: string;
  match: string;
  sport: string;
  market: string;
  margin: number;
  roi_percent: number;
  stakes: ArbStake[];
  expires_at: number; // Unix ms
  type: "arb_new" | "arb_update";
}

export interface ArbExpiredEvent {
  id: string;
  reason: "odds_moved" | "market_suspended" | "market_voided" | "ttl_elapsed";
  type: "arb_expired";
}

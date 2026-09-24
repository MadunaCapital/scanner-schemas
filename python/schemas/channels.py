"""Redis channel names shared across services -- the wire protocol's
"topic" names, alongside the message shapes in models.py.

Centralized here (rather than each repo defining its own local constant)
because ingestion, engine, and api all already depend on scanner-schemas;
duplicating the string in each repo risks them silently drifting apart.
"""

RAW_ODDS_CHANNEL = "raw_odds_events"
ARBITRAGE_CHANNEL = "live_arbitrage_alerts"

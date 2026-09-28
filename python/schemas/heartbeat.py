"""Redis key naming for the liveness heartbeats written by scanner-ingestion's
scrapers and scanner-engine, and read by scanner-api's health endpoints (see
that repo's /api/health/scrapers and /api/health/system).

Each key is written with SETEX at roughly 3x the writer's own cycle interval,
so a component that stops running entirely (crashed process, hung event
loop) simply has its key expire on its own -- a missing/expired key IS the
"down" signal. No separate liveness-detection logic needed anywhere that
reads these keys.
"""

HEARTBEAT_KEY_PREFIX = "heartbeat:"

# component_id used for scanner-engine's own heartbeat key, alongside each
# scraper's bookmaker_id (e.g. "betway_za") for the per-scraper keys.
ENGINE_HEARTBEAT_ID = "engine"


def heartbeat_key(component_id: str) -> str:
    """component_id is a bookmaker_id for a scraper's heartbeat (see
    scanner-ingestion's raw_publisher.py) or ENGINE_HEARTBEAT_ID for
    scanner-engine's own heartbeat (see scanner-engine's runner.py)."""
    return f"{HEARTBEAT_KEY_PREFIX}{component_id}"

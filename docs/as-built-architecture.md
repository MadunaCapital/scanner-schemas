# As-Built Architecture

This is the current, real state of the project — as opposed to `arbitrage-scanner-plan.md`, which is the original brainstorming transcript (kept as a historical record; several of its specifics, like which bookmakers use what tech, turned out different once actually built) and its hardening addendum. Where the two disagree, this document is correct. Last updated 2026-09-28.

## Repos (9 total)

| Repo | Role |
|---|---|
| `scanner-schemas` | Shared contracts: Pydantic models (`OddsEvent`, `MarketOdds`, `ArbEvent`, `ArbStake`, `ArbExpiredEvent`) and the two Redis channel name constants (`RAW_ODDS_CHANNEL`, `ARBITRAGE_CHANNEL`). Everything else depends on this. |
| `scanner-ingestion` | Shared, bookmaker-agnostic toolkit: `BaseScraper` abstract class, the generic `raw_publisher` (publishes `OddsEvent`s to Redis). Previously also carried a `stealth` extra (TLS-impersonated client, Cloudflare session handling) — stripped out on 2026-09-27 once real experience across 5 bookmakers showed it wasn't earning its place. |
| `scanner-ingestion-betway-za` | Real, live Betway ZA scraper. Public unauthenticated JSON endpoint, plain `httpx`, no evasion. |
| `scanner-ingestion-wsb` | Real, live World Sports Betting scraper. Same approach, different (nested) response shape. |
| `scanner-engine` | Normalization, arbitrage math, cross-bookmaker aggregation, and persistence. The one service allowed to do blocking I/O (DB), everything else stays fast. |
| `scanner-api` | FastAPI, subscribes to `ARBITRAGE_CHANNEL`, streams to the frontend over SSE. |
| `scanner-frontend` | React dashboard. |
| `scanner-infra` | Terraform, targeting **Azure** (not AWS — switched over entirely on 2026-09-27). |

## How a bookmaker actually gets integrated

The plan originally assumed every SA bookmaker would need scraping evasion (TLS impersonation, Cloudflare-solving, stealth browsers) to reach their odds data. **In practice, neither bookmaker built so far needs any of that**: both Betway ZA and World Sports Betting serve odds via a plain, unauthenticated JSON endpoint that their own site's frontend calls — the same request any visitor's browser makes. The evasion tooling this assumption produced (`scanner-ingestion`'s `stealth` extra) sat unused and was removed entirely on 2026-09-27. If a genuinely WAF-protected bookmaker turns up later, building that tooling is a real decision to make fresh at that point, not something worth having pre-built speculatively.

Bookmakers tried and not yet cracked (harder API shapes — undocumented params, WebSocket/SignalR push, or Kambi-style session widgets, not evasion difficulty): Hollywoodbets, Sportingbet ZA, Bet.co.za. Confirmed licensed via the NGB verified-operators portal but not yet attempted: Supabets, Easybet, PantherBet, Swifty Sports.

## Data flow (as it actually runs)

```
scanner-ingestion-betway-za  ─┐
                               ├─→ Redis: RAW_ODDS_CHANNEL ─→ scanner-engine
scanner-ingestion-wsb        ─┘                                   │
                                                                   ├─→ OddsAggregator (EntityResolver-based event
                                                                   │    matching: exact alias → fuzzy → dead-letter,
                                                                   │    arb_new / arb_update / arb_expired lifecycle,
                                                                   │    staleness filtering, memory pruning)
                                                                   │
                                                                   ├─→ Postgres (arb history log)
                                                                   │
                                                                   └─→ Redis: ARBITRAGE_CHANNEL ─→ scanner-api (SSE) ─→ scanner-frontend
```

## Entity resolution is now wired into the matching path (closed 2026-09-28)

`scanner-engine/normalization.py`'s `EntityResolver` (three-tier: exact alias cache → RapidFuzz fuzzy match → dead-letter queue) is now called from `OddsAggregator.ingest()` (`aggregator.py`) for both `home_team` and `away_team` before events are grouped/hashed, instead of grouping by raw scraped team-name string equality. `OddsAggregator` takes a `resolver: EntityResolver` (shared with the one `__main__.py` loads from the `team_aliases` Postgres table at startup via `entity_store.load_aliases_into_resolver`, so previously-resolved aliases are honored immediately on every restart).

A name that resolves via Tier 1 (exact) or Tier 2 (fuzzy, ≥88 token_sort_ratio) is grouped by that canonical name, so a bookmaker's variant spelling ("Man Utd" vs "Manchester United") is now correctly matched to the same real-world event. A name that resolves via neither tier is queued to `EntityResolver.dead_letter_queue` for manual review (see `entity_store.persist_manual_link`) — this is never silently dropped and never crashes the aggregator, but the event is tracked under its own raw name in the meantime (the prior exact-string-match behaviour, kept only as the unresolved fallback), so it won't be merged with another bookmaker's spelling of the same team until someone resolves the dead-letter entry. `EntityResolver.resolve()` now also dedupes dead-letter entries by (league, case-insensitive name), since a live polling loop calls it on every re-scrape (e.g. every ~45s) and would otherwise grow the queue unboundedly for a name that stays unresolved.

Known remaining limitation: `EntityResolver`'s fuzzy tier (Tier 2) is O(cache size) per unresolved lookup and was originally documented as unsafe for a live hot path; wiring it into `ingest()` means a team that never resolves pays that cost on every single poll cycle for as long as it stays unresolved. Not a correctness problem (same safe failure mode as before: a missed match, never a false one), but worth watching as the alias dictionary and the set of never-resolved teams both grow.

## Database

Real schema (`scanner-engine/src/engine/models_db.py`), migrated via Alembic:
- `arb_events` — every detected arbitrage opportunity, with status (`active`/`expired`), margin, ROI, stakes (JSON), timestamps.
- `team_aliases` — the entity-resolution dictionary, loaded into `EntityResolver`'s in-memory cache at engine startup (see above) and consulted on every odds event; still populated only via the manual-link path (`entity_store.persist_manual_link`), since the "admin dashboard" for resolving dead-letter entries doesn't exist yet.

Defaults to a local SQLite file for zero-config dev; production points `DATABASE_URL` at the real Postgres Flexible Server provisioned in `scanner-infra`.

## Infrastructure: Azure, not AWS

The original plan (and the first draft of `scanner-infra`) targeted AWS (`af-south-1`, ECS Fargate, ElastiCache, ALB). This was **replaced entirely** with an Azure equivalent:

| Concern | Azure resource |
|---|---|
| Compute (all 4 services) | Container Apps Environment (built-in ingress + autoscaling — no separate ALB or scaling-policy resources needed) |
| Redis | Azure Cache for Redis, private-endpoint-only |
| Database | PostgreSQL Flexible Server, VNet-integrated |
| Container images | Azure Container Registry |
| Region | **UAE North** — South Africa North would have been the latency-optimal choice (Azure's equivalent to `af-south-1`), but the deploying subscription's regional-access policy doesn't allow it; UAE North is the closest geographically-permitted region |

`scanner-engine`, `scanner-ingestion-betway-za`, and `scanner-ingestion-wsb` are each pinned to exactly 1 replica in Terraform — they're singleton background workers, not horizontally scalable; a second copy of any of them would double-publish everything.

**Not yet done:** `terraform validate`/`plan` has never actually been run (no Terraform CLI available in the environment this was built in), and there's no Azure subscription to `apply` against yet. The GitHub Actions deploy workflow (build → push to ACR → update Container App revision) doesn't exist yet either.

## Legal / compliance status

A South African advocate (Ike Khumalo) has been contacted and scheduled an initial consultation; no written legal opinion has actually been delivered yet. Do not treat this project as legally cleared based on anything in this repo. Separately: the scraping approach itself (plain requests to public unauthenticated endpoints) is a materially lower-risk activity than the original plan's anti-bot-evasion approach would have been — but "lower risk" is not the same as "cleared," and ToS exposure is a real, distinct question from the Cybercrimes Act question that would have applied to evasion.

# As-Built Architecture

This is the current, real state of the project — as opposed to `arbitrage-scanner-plan.md`, which is the original brainstorming transcript (kept as a historical record; several of its specifics, like which bookmakers use what tech, turned out different once actually built) and its hardening addendum. Where the two disagree, this document is correct. Last updated 2026-09-29.

A long-form, illustrated version of this document (animated diagrams, the arbitrage math derived step by step, latency breakdown, Azure topology) lives in the dashboard itself: `scanner-frontend/src/components/docs/` ("Docs" in the side nav). Keep the two in sync.

## Repos (12 total)

| Repo | Role |
|---|---|
| `scanner-schemas` | Shared contracts: Pydantic models (`OddsEvent`, `MarketOdds`, `ArbEvent`, `ArbStake`, `ArbExpiredEvent`, `ScraperHeartbeat`, `EngineHeartbeat`), the two Redis channel name constants (`RAW_ODDS_CHANNEL`, `ARBITRAGE_CHANNEL`), and heartbeat key naming (`heartbeat_key()`). Everything else depends on this. **Public**, so service Dockerfiles can `pip install` it via git without a CI secret. |
| `scanner-ingestion` | Shared, bookmaker-agnostic toolkit: `BaseScraper` abstract class and `raw_publisher.run_scraper_loop` (publishes `OddsEvent`s to Redis and writes a per-bookmaker heartbeat after every poll cycle). No bookmaker-specific code. **Public**, same reason. Previously also carried a `stealth` extra (TLS-impersonated client, Cloudflare session handling) — stripped out on 2026-09-27 once real experience showed it wasn't earning its place. |
| `scanner-ingestion-betway-za` | Betway ZA scraper. |
| `scanner-ingestion-wsb` | World Sports Betting scraper. |
| `scanner-ingestion-interbet` | Interbet scraper (HTML feed). |
| `scanner-ingestion-sunbet` | Sunbet scraper (Kambi white-label). |
| `scanner-ingestion-easybet` | Easybet scraper (advbet/Aardvark platform). |
| `scanner-ingestion-supabets` | SupaBets scraper (H3 B2C API). |
| `scanner-engine` | Entity resolution, cross-bookmaker aggregation, arbitrage math, arb lifecycle, and persistence. The one service that does blocking-ish I/O (DB); everything else stays fast. |
| `scanner-api` | FastAPI: SSE stream of arb events, upcoming-fixtures endpoint for the calendar, and health endpoints for the status page. |
| `scanner-frontend` | React dashboard (Arb Feed, Calendar, Status, Docs), served by nginx. |
| `scanner-infra` | All Terraform, targeting **Azure** (not AWS — switched over entirely on 2026-09-27). |

The split is deliberately maximal — one repo, image and Container App per bookmaker — trading some duplicated code for isolation: a broken adapter can't take down another bookmaker's pipeline. The only coupling between repos is `scanner-schemas`.

## Bookmakers

### Live (6)

Every scraper reads a public, unauthenticated endpoint — the same request the bookmaker's own website makes for any visitor — with plain `httpx`, on a fixed 45 s poll interval. No logins, cookies, session tokens, TLS impersonation or fingerprint spoofing.

| Bookmaker | Endpoint / format | Sports |
|---|---|---|
| Betway ZA | `betway.co.za/sportsapi/br/v1/BetBook/Highlights/` — JSON, four flat arrays (events/markets/outcomes/prices) joined by id | soccer, rugby, cricket, tennis |
| World Sports Betting | `content-prod.worldsportsbetting.co.za/.../event-list` — nested JSON, sport chosen via `drilldownTagIds` | soccer, rugby, cricket, tennis, basketball, golf (H2H only) |
| Interbet | `interbet.co.za/FixedOdds/LoadCouponsPartial` — server-rendered HTML; odds read from each button's `data-url` query string (BeautifulSoup + `parse_qs`) | soccer, rugby, cricket, tennis, basketball |
| Sunbet | `eu-offering-api.kambicdn.com/.../listView/{sport}/all/all/all.json` — Kambi JSON, odds as integers ×1000 | soccer, rugby, cricket, tennis, basketball |
| Easybet | `sportsbook-sa01-backend.advbet.com/distributor/api/organizations/{orgUuid}/events` — JSON, markets keyed by type | soccer, rugby, cricket, tennis, basketball |
| SupaBets | `apib2c.supabets.co.za/api/frontend/matches/{sportTypeId}/...` — JSON, requires static header `x-api-key: H3DigitalAPIB2CWebSiteUser` | soccer, rugby, cricket, tennis, basketball, motorsport (H2H only) |

29 of the 30 bookmaker × core-sport combinations work. The one gap: **Betway ZA has no basketball** — `sportId=basketball` is valid, but no `marketTypes` filter (100+ tried) returns a match-winner market; only totals, which don't fit `MarketOdds`.

Judgment calls that apply to future additions:
- A required header is fine if it's a stateless public constant every visitor's browser sends (SupaBets' `x-api-key`, hard-coded in its public JS). A session/auth token is not, even if free to obtain (see 10bet below).
- Payload format doesn't matter (Interbet is HTML); only real auth/WAF/fingerprinting barriers do.
- Market shape (2-way vs 3-way, regulation vs incl. overtime) is always verified against live data, never assumed. This caught a real bug: Easybet basketball fixtures carry both a regulation-time `winner3` and the true full-game `winner2-incl-overtime`; the priority order had to be fixed.
- Golf and motorsport are **H2H-only** by design. Their standard product is an outright field, which the 2–3-outcome `MarketOdds` schema can't represent; supporting outrights is a schema change, not an extension. Easybet's golf/motorsport H2H markets exist but were deliberately not built (no fixture-level identity or stable market key).

### Investigated and ruled out (don't re-attempt without a genuinely new angle)

Each needed anti-bot evasion or a session/token mechanism, which is out of scope by design: **Hollywoodbets** and **Lucky Fish** (shared platform: Cloudflare WAF on config endpoint, Iovation/IGLOO fingerprinting), **Sportingbet ZA** (WAF 403 on odds API, reCAPTCHA Enterprise), **Bet.co.za** (Playtech SPA; feed URL only discoverable by executing the app's runtime), **Goldrush** (odds only over BetConstruct's session-based Swarm WebSocket; REST needs SSO), **ZARbet** (odds behind a session-bootstrapped iframe + FingerprintJS Pro), **PlayaBets** (AWS Cognito auth + Cloudflare bot management), **Yesplay** (Cloudflare TLS-fingerprint block on `httpx`/`aiohttp`), **10bet ZA** (needs a bootstrapped anonymous session JWT).

## Data flow (as it actually runs)

```
6 × scanner-ingestion-<bookmaker> ──PUBLISH──→ Redis: raw_odds_events ──→ scanner-engine
   (each also SETs heartbeat:<bookmaker>, TTL 135 s)                            │
                                                                               ├─ validate (Pydantic)
                                                                               ├─ EntityResolver: exact → fuzzy → dead-letter
                                                                               ├─ md5(home|away|start_utc) → event_id
                                                                               ├─ latest price per (event, market, bookmaker)
                                                                               ├─ drop prices > 120 s old
                                                                               ├─ best price per outcome, ≥ 2 distinct bookmakers
                                                                               ├─ arbitrage math (M = Σ 1/d < 1)
                                                                               ├─ arb_new / arb_update / arb_expired lifecycle
                                                                               │
                                                                               ├─→ Postgres: scheduled_events (every fixture),
                                                                               │             arb_events (every arb change)
                                                                               │
                                                                               └─PUBLISH─→ Redis: live_arbitrage_alerts
                                                                                              │
                                                    scanner-api (SSE /api/stream/arbitrage) ←─┘
                                                              │
                                                    scanner-frontend (EventSource) → user places bets manually
```

Scrapers publish the **full current state** every cycle, not diffs, so Redis pub/sub's at-most-once delivery is acceptable: anything lost while the engine restarts is re-sent within 45 s. Redis holds nothing at rest.

Scrapers, the engine and Redis are each pinned to exactly 1 replica — a second copy would double-publish or double-process everything. Only the API and frontend scale horizontally.

### Timers that matter

| Value | What | Why |
|---|---|---|
| 45 s | Scraper poll interval (`DEFAULT_POLL_INTERVAL_SECONDS`, every repo) | Page-refresh cadence; fixed, not randomized. Dominates end-to-end latency (a change waits 22.5 s on average for the next poll; engine work per event is ~0.2 ms). |
| 120 s | Engine odds freshness gate | > 2 missed polls, so one slow cycle doesn't drop a bookmaker, but a dead feed stops being compared quickly. |
| 150 s | Arb `expires_at` TTL | > 3 cycles; frontend safety net only — `arb_expired` is the primary signal. |
| 135 s / 30 s | Scraper / engine heartbeat TTL | 3 × the writer's cycle (45 s / 10 s). An expired key *is* the "down" signal. |
| 1.0 s + 0.1 s | SSE loop per client | `get_message(timeout=1.0)` + `sleep(0.1)`: ≤ ~1.1 s delivery delay; sends a keep-alive comment when idle. |
| 300 s / 4 h | Engine prune interval / retention past kick-off | Bounds memory on a long-running process. |

## Entity resolution

`scanner-engine/normalization.py`'s `EntityResolver` (three tiers: exact alias cache → RapidFuzz `token_sort_ratio` ≥ 88, same league only → dead-letter queue) is called from `OddsAggregator.ingest()` for both `home_team` and `away_team` before events are hashed. At startup `__main__.py` loads the `team_aliases` table into the resolver (`entity_store.load_aliases_into_resolver`), so previously resolved aliases apply immediately.

A name resolved via Tier 1 or 2 groups under its canonical name, so "Man Utd" and "Manchester United" at two bookmakers are the same event. An unresolved name is queued once (deduped by league + case-insensitive name) to `dead_letter_queue` and tracked under its own raw spelling in the meantime. The failure mode is always safe: a missed arbitrage, never a false one.

Known limitations:
- `team_aliases` is only populated via the manual-link path (`entity_store.persist_manual_link`). There is **no admin UI for the dead-letter queue yet**, so in practice most names currently resolve via their own raw spelling, and cross-bookmaker matching only works where spellings already agree exactly or fuzzy-match an existing alias.
- Tier 2 is O(cache size) per unresolved lookup (≈ 2.5 ms against 500 aliases, measured locally) and runs on every poll cycle for a name that stays unresolved. The engine also logs a warning per unresolved name per cycle. Neither is a correctness problem; both shrink as the alias backlog is cleared.

## Health monitoring

- Each scraper's `run_scraper_loop` writes `heartbeat:<bookmaker_id>` (`ScraperHeartbeat`: status, timestamp, events published this cycle, error) after every cycle. If every sport fails in a cycle, nothing is yielded and no heartbeat is written, so a real outage surfaces as "down" rather than "ok, 0 events".
- The engine writes `heartbeat:engine` (`EngineHeartbeat`) every 10 s regardless of traffic, including Redis ping, Postgres `SELECT 1`, total messages processed and last successful DB write.
- `scanner-api` exposes `/api/health/scrapers` (fixed list of the 6 known bookmakers, so an expired key shows as "down" rather than disappearing — update `KNOWN_BOOKMAKERS` when onboarding a new one) and `/api/health/system`. The frontend's Status page polls both every 4 s.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/stream/arbitrage` | SSE: `event: arb_new` / `arb_update` / `arb_expired`, keep-alive comments when idle. One Redis client per connection, always closed on disconnect. |
| `GET /api/events?sport=` | Upcoming fixtures from `scheduled_events` (Calendar page). |
| `GET /api/health/scrapers`, `/api/health/system` | Status page. |
| `GET /health` | Container Apps liveness probe. |

## Database

Real schema (`scanner-engine/src/engine/models_db.py`), migrated via Alembic:
- `arb_events` — every detected arbitrage opportunity: margin, ROI, stakes (JSON), status (`active`/`expired`), expiry reason, first-detected / last-updated / expired timestamps.
- `team_aliases` — the entity-resolution dictionary (league, scraped name → universal name).
- `scheduled_events` — every fixture any scraper has reported (arb or not), upserted by canonical `event_id`, for the Calendar page.

All DB writes from the engine are best-effort: a Postgres failure is logged and never blocks an arb alert from being published. Defaults to a local SQLite file for zero-config dev; production points `DATABASE_URL` at the Postgres Flexible Server provisioned in `scanner-infra`.

## Arbitrage math

`scanner-engine/arbitrage.py`: IP_i = 1/d_i, M = Σ IP_i over the best fresh price per outcome, arbitrage iff M < 1, stakes S_i = B·IP_i/M (equal payout B/M on every outcome), ROI = (1/M − 1) × 100. Guards: odds ≤ 1.0 or < 2 outcomes raise; at least 2 distinct bookmakers required; the draw is included whenever any fresh book prices one; per-outcome `max_stake` is modelled and flagged when exceeded, though no bookmaker's limits are known, so it's always unset today. The engine computes against a R1000 placeholder; the frontend recomputes stakes against the user's typed bankroll with the same formula.

Not accounted for: execution risk between alert and placement, poll lag (a compared price can be up to ~45 s old), real bet limits, rounding to each book's minimum stake increments, and settlement-rule differences between books.

## Infrastructure: Azure (UAE North)

**Live and confirmed working end to end since 2026-09-28**: all 6 scrapers publishing real odds, engine writing to Postgres, API streaming, frontend deployed. `terraform apply` is clean.

| Concern | Azure resource |
|---|---|
| Compute (10 apps) | Container Apps Environment in a delegated subnet (10.0.0.0/23) of VNet 10.0.0.0/16. 6 scrapers (0.25 vCPU / 0.5 GiB, 1 replica each), engine (0.5 vCPU / 1 GiB, 1 replica), Redis, API (external ingress :8000, 1–5 replicas, scales at 200 concurrent requests), frontend (nginx, external ingress :80, 1–3 replicas). |
| Redis | **Self-hosted `redis:7-alpine` Container App**, internal-only TCP ingress on 6379, no persistence. `azurerm_redis_cache` is retired on this subscription. Must be addressed by the app's **short name** (`redis://arbitrage-scanner-redis:6379`) — the FQDN resolves but TCP connections time out. |
| Database | PostgreSQL 15 Flexible Server (B_Standard_B1ms, 32 GB, 7-day backups), VNet-integrated in its own delegated subnet (10.0.3.0/24) with a private DNS zone, public access disabled. |
| Container images | Azure Container Registry (Basic, admin credentials stored as Container App secrets). |
| Logs | Log Analytics workspace, 30-day retention. |
| Terraform state | Remote backend: storage account `arbscannertfstate` in its own resource group (`arbitrage-scanner-tfstate-rg`), so it survives deleting `arbitrage-scanner-rg`. |
| Region | **UAE North** — South Africa North would be latency-optimal but is blocked by the subscription's regional policy. |

**CI/CD:** every service repo has `.github/workflows/deploy.yml`: on push to `main` → `azure/login` (service principal in `AZURE_CREDENTIALS`) → `docker build` tagged with the commit SHA and `latest` → `az acr login` + `docker push` → `az containerapp update --image <sha>`. Builds run on the GitHub runner because ACR Tasks is blocked by subscription policy. The frontend's API URL is baked in at build time via the `VITE_API_URL` build arg.

Deployment lessons (don't reintroduce):
- `infrastructure_resource_group_name` (Container Apps Environment) and `zone` (Postgres) are Azure-assigned and ForceNew/immutable — both are in `lifecycle.ignore_changes`.
- Changing only a Container App secret does not roll a new revision; an explicit `az containerapp revision restart` is needed for the running container to see the new value.
- Postgres VNet integration needs `public_network_access_enabled = false` set explicitly.

## Known gaps / possible next steps

- **Dead-letter review UI** for entity resolution (the biggest lever for cross-bookmaker matching).
- **Alembic isn't in the engine image** — its Dockerfile doesn't copy `alembic/` or `alembic.ini`, so migrations can't run in production. The current schema was bootstrapped by temporarily setting `AUTO_CREATE_TABLES=true`.
- **SSE idle behaviour** through Container Apps' Envoy ingress hasn't been explicitly verified for long quiet periods (the ~1.1 s keep-alive should cover it).
- **Hardening:** managed identity + AcrPull instead of ACR admin credentials; secrets (Postgres password) in Key Vault instead of tfvars.
- Move to South Africa North if the subscription ever allows it.
- More markets (totals, handicaps); N-way outrights would need a schema change.
- Backtesting / analytics over the `arb_events` history.

## Legal / compliance status

A South African advocate (Ike Khumalo) has been contacted and scheduled an initial consultation; no written legal opinion has actually been delivered yet. Do not treat this project as legally cleared based on anything in this repo. Separately: the scraping approach itself (plain requests to public unauthenticated endpoints) is a materially lower-risk activity than the original plan's anti-bot-evasion approach would have been — but "lower risk" is not the same as "cleared," and ToS exposure is a real, distinct question from the Cybercrimes Act question that would have applied to evasion.

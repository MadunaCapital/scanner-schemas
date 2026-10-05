# As-Built Architecture

This is the current, real state of the project — as opposed to `arbitrage-scanner-plan.md`, which is the original brainstorming transcript (kept as a historical record; several of its specifics, like which bookmakers use what tech, turned out different once actually built) and its hardening addendum. Where the two disagree, this document is correct. Last updated 2026-10-05.

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
                                                                               ├─ team_aliases (curated overrides, optional)
                                                                               ├─ EventMatcher: same sport, kickoff ±15 min,
                                                                               │   both names fuzzy ≥ 88 → canonical event_id
                                                                               ├─ latest price per (event, market, bookmaker)
                                                                               ├─ drop prices > 120 s old
                                                                               ├─ best price per outcome, ≥ 2 distinct bookmakers
                                                                               ├─ arbitrage math (M = Σ 1/d < 1)
                                                                               ├─ ROI > 10% → "suspect" (recorded, not published)
                                                                               ├─ arb_new / arb_update / arb_expired lifecycle
                                                                               │
                                                                               ├─→ Postgres: scheduled_events (every fixture),
                                                                               │             arb_events (latest state per arb),
                                                                               │             arb_snapshots (every change)
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

## Fixture matching (which prices belong to the same match)

`scanner-engine/src/engine/matching.py`'s `EventMatcher` decides when two bookmakers' fixtures are the same real match. The rule is: same sport, kickoffs within **±15 minutes**, and **both** team names fuzzy-matching at ≥ 88. Names are first normalised: accents folded, punctuation and club noise words (FC, SC, "and") dropped. Scoring uses RapidFuzz `token_sort_ratio`, so "Monteiro, Thiago" = "Thiago Monteiro". For names of 2+ words it also uses `token_set_ratio`, so "CA Platense" = "Platense". A bookmaker listing the teams the other way round is detected, and its home/away odds are swapped. League is deliberately ignored, because every bookmaker names competitions differently.

The first bookmaker to report a match defines its canonical names, kickoff and `event_id` (MD5 of sport, normalised names and kickoff). Later matches reuse it. Each (bookmaker, fixture) is matched once and then cached: about 0.2 ms for a first sighting, 0.2 µs per repeat.

A wrong merge would fabricate an arb out of two unrelated matches, so the guards are conservative:
- both names must match, never just one;
- "marker" tokens (W/women, U19–U23, B, II, reserves, youth…) must agree exactly, so "Zimbabwe (W)" never merges with "Zimbabwe";
- one bookmaker can never have two of its own fixtures merged into one event;
- any arb above **10% ROI** is recorded as `suspect` and not published, since it's almost always a mismatch or a palpable pricing error.

Why it was rebuilt (2026-10-05): the previous identity was MD5 over exact names plus an exact-to-the-second kickoff. On live data it split 489 pairs of real matches apart, and production streamed zero arbs. Validation of the new matcher:
- **On 1,392 live fixtures:** 197 merges, and every one of the 45 that needed fuzzy, subset or swap matching was audited by hand and correct.
- **On a full live scrape cycle:** 381 matches across 2–6 bookmakers, 5 arbs at 0.3–2.8% ROI, 0 suspect. Production began streaming arbs immediately after deploy.

`team_aliases` (loaded into `EntityResolver` at startup) still works as an optional curated override, applied before matching. The aggregator no longer dead-letters unaliased names: a fixture only one bookmaker lists is normal, not an error.

Known limits: a match whose two listings disagree on kickoff by more than 15 minutes, or whose names differ beyond the threshold (e.g. "Aviron Bayonne" vs "Aviron Bayonnais", 86.7), is still tracked separately. That's a missed arb, never a false one.

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

Real schema (`scanner-engine/src/engine/models_db.py`), migrated via Alembic. The engine runs `alembic upgrade head` itself at startup (`engine/migrations.py`), so schema changes ship with the image. Production's original tables were created by `create_all`, so on first boot the unversioned schema is stamped at `1a4a61ad1456` before upgrading.
- `arb_events` — latest state of every detected arbitrage: margin, ROI, stakes (JSON), status (`active`/`expired`), expiry reason, first-detected / last-updated / expired timestamps. Arbs still `active` when the engine restarts are closed at startup with reason `engine_restart` at their `last_updated_at`. Treat those lifetimes as censored (a lower bound).
- `arb_snapshots` — the time series behind lifetime and peak analysis: one row whenever an arb's margin or legs change, plus `new`, `expired` and `suspect` events. Each row records every leg's bookmaker, odds and scrape time, seconds to kickoff (negative means in-play), and the fixture-matching confidence. Unchanged re-confirmations aren't written; `arb_events.last_updated_at` carries the last time an arb was seen alive. Lifetimes are only as precise as the 45 s poll interval.
- `team_aliases` — the entity-resolution dictionary (league, scraped name → universal name).
- `scheduled_events` — every fixture any scraper has reported (arb or not), upserted by canonical `event_id`, for the Calendar page.

All DB writes from the engine are best-effort: a Postgres failure is logged and never blocks an arb alert from being published. Defaults to a local SQLite file for zero-config dev; production points `DATABASE_URL` at the Postgres Flexible Server provisioned in `scanner-infra`.

## Arbitrage math

`scanner-engine/arbitrage.py`: IP_i = 1/d_i, M = Σ IP_i over the best fresh price per outcome, arbitrage iff M < 1, stakes S_i = B·IP_i/M (equal payout B/M on every outcome), ROI = (1/M − 1) × 100. Guards: odds ≤ 1.0 or < 2 outcomes raise; at least 2 distinct bookmakers required; the draw is included whenever any fresh book prices one; per-outcome `max_stake` is modelled and flagged when exceeded, though no bookmaker's limits are known, so it's always unset today. The engine computes against a R1000 placeholder; the frontend recomputes stakes against the user's typed bankroll with the same formula.

Not accounted for: execution risk between alert and placement, poll lag (a compared price can be up to ~45 s old), real bet limits, rounding to each book's minimum stake increments, and settlement-rule differences between books.

## Infrastructure: Azure (South Africa North)

**Live and verified end to end since 2026-10-05** in subscription `maduna-scanner` (Aruna AI tenant, pay-as-you-go Azure Plan): all 6 scrapers publishing real odds, engine writing to Postgres, API streaming, frontend deployed.

- Dashboard: https://arbitrage-scanner-frontend.happybeach-b55a0f45.southafricanorth.azurecontainerapps.io
- API: https://arbitrage-scanner-api.happybeach-b55a0f45.southafricanorth.azurecontainerapps.io

History: the first deployment (2026-09-28) ran in **UAE North** under an Azure for Students subscription, whose policy blocked South Africa North and ACR Tasks. Its credit ran out and it was disabled on 2026-10-03. It was rebuilt fresh in the new subscription on 2026-10-05, with no data carried over. The old subscription's data is purged on 2027-01-01, and nothing in it needs keeping.

| Concern | Azure resource |
|---|---|
| Compute (10 apps) | Container Apps Environment in a delegated subnet (10.0.0.0/23) of VNet 10.0.0.0/16. 6 scrapers (0.25 vCPU / 0.5 GiB, 1 replica each), engine (0.5 vCPU / 1 GiB, 1 replica), Redis, API (external ingress :8000, 1–5 replicas, scales at 200 concurrent requests), frontend (nginx, external ingress :80, 1–3 replicas). |
| Redis | **Self-hosted `redis:7-alpine` Container App**, internal-only TCP ingress on 6379, no persistence. `azurerm_redis_cache` is retired. Must be addressed by the app's **short name** (`redis://arbitrage-scanner-redis:6379`) — the FQDN resolves but TCP connections time out. |
| Database | PostgreSQL 15 Flexible Server (B_Standard_B1ms, 32 GB, 7-day backups), VNet-integrated in its own delegated subnet (10.0.3.0/24) with a private DNS zone, public access disabled. |
| Container images | Azure Container Registry `arbitragescannerregistryza` (Basic, admin credentials stored as Container App secrets). |
| Logs | Log Analytics workspace, 30-day retention, ingestion capped at 0.5 GB/day (`log_analytics_daily_quota_gb`). |
| Terraform state | Remote backend: storage account `arbscannertfstateza` in its own resource group (`arbitrage-scanner-tfstate-rg`), so it survives deleting `arbitrage-scanner-rg`. |
| Region | **South Africa North** (Johannesburg), the closest region to the bookmakers. |
| Global names | Registry, Postgres server (`arbitrage-scanner-postgres-za`) and state storage carry the Terraform `name_suffix` (`za`), because the disabled first subscription still holds the unsuffixed names until it's purged. |

**CI/CD:** every service repo has `.github/workflows/deploy.yml`: on push to `main` → `azure/login` (service principal in `AZURE_CREDENTIALS`) → `docker build` tagged with the commit SHA and `latest` → `az acr login` + `docker push` → `az containerapp update --image <sha>`. Builds run on the GitHub runner rather than with ACR Tasks: the first subscription blocked Tasks, and runner builds work on any subscription. CI authenticates as the `scanner-deploy-za` service principal (Contributor on `arbitrage-scanner-rg`). The frontend's API URL is baked in at build time from the `VITE_API_URL` Actions variable on `scanner-frontend`. The full deploy order for a fresh subscription is in `scanner-infra/README.md`.

Deployment lessons (don't reintroduce):
- `infrastructure_resource_group_name` (Container Apps Environment) and `zone` (Postgres) are Azure-assigned and ForceNew/immutable — both are in `lifecycle.ignore_changes`.
- Changing only a Container App secret does not roll a new revision; an explicit `az containerapp revision restart` is needed for the running container to see the new value.
- Postgres VNet integration needs `public_network_access_enabled = false` set explicitly.
- In a fresh subscription, the registry has to exist and hold images before Terraform can create the Container Apps: `terraform apply -target=azurerm_container_registry.main` first, push images via CI, then the full apply.

## Cost

Measured on the first deployment (Azure Cost Management, 1–3 Oct 2026): **$18.70/day, about $560/month**. Of that, **86% was Log Analytics ingestion** (~5 GB/day at $3.29/GB). The engine was logging a warning for every unresolved team name, twice per event, on every 45 s cycle, and `team_aliases` is empty, so that was every event. All 10 Container Apps together were about $2.40/day, Postgres was free under the Students offer, and the registry was $0.17/day.

Fixed on 2026-10-05: the per-name warning was first cut to once per name, then removed entirely when fixture matching was rebuilt the same day (an unaliased name is normal, not an error). `arb_update` logs at DEBUG, and Log Analytics ingestion is capped at 0.5 GB/day, so a future log flood costs at most ~$1.70/day.

Expected now, at South Africa North retail prices: Container Apps ≈ $72, Postgres B1ms + 32 GB ≈ $19, registry ≈ $5, logs ≈ $0–5 (first 5 GB/month free), other ≈ $1, for a total of **≈ $95–100/month**. A `scanner-monthly` budget of $100 emails at 80%. A nightly off-window (scrapers and engine scaled to zero overnight via a cron scale rule) would save only ~$12/month, so it hasn't been built.

## Known gaps / possible next steps

- **Arb analysis** (once ~2 weeks of `arb_snapshots` exist): survival curves for time-to-place, peak timing, pre-match vs in-play, and the causing bookmaker, all per sport.
- **Bankroll tooling:** per-bookmaker standby funds sized from history, a linear program to choose among simultaneous arbs given each book's balance, and a portfolio page tracking placed arbs and per-book balances before and after payout. The API has no authentication yet, so that must come first.
- **SSE idle behaviour** through Container Apps' Envoy ingress hasn't been explicitly verified for long quiet periods (the ~1.1 s keep-alive should cover it).
- **Hardening:** managed identity + AcrPull instead of ACR admin credentials; secrets (Postgres password) in Key Vault instead of tfvars.
- More markets (totals, handicaps); N-way outrights would need a schema change.

## Legal / compliance status

A South African advocate (Ike Khumalo) has been contacted and scheduled an initial consultation; no written legal opinion has actually been delivered yet. Do not treat this project as legally cleared based on anything in this repo. Separately: the scraping approach itself (plain requests to public unauthenticated endpoints) is a materially lower-risk activity than the original plan's anti-bot-evasion approach would have been — but "lower risk" is not the same as "cleared," and ToS exposure is a real, distinct question from the Cybercrimes Act question that would have applied to evasion.

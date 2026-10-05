# Architecture Diagrams — MadunaCapital Arbitrage Scanner

Reflects the **as-built** system (see [as-built-architecture.md](as-built-architecture.md)) as of 2026-10-05 — not the original brainstorm in [arbitrage-scanner-plan.md](arbitrage-scanner-plan.md), which assumed anti-bot evasion and AWS; neither turned out to be what was actually needed or used. Renders natively on GitHub; for a standalone browser view see `architecture-diagrams.html` in this same folder. An animated, interactive version lives in the dashboard's Docs page (`scanner-frontend/src/components/docs/`).

## 1. System Architecture (End-to-End Data Flow)

```mermaid
flowchart LR
    subgraph Sources["SA Bookmakers (public endpoints)"]
        BM1["Betway ZA<br/>JSON"]
        BM2["World Sports Betting<br/>JSON"]
        BM3["Interbet<br/>HTML coupons"]
        BM4["Sunbet<br/>Kambi JSON"]
        BM5["Easybet<br/>advbet JSON"]
        BM6["SupaBets<br/>H3 JSON + static x-api-key"]
    end

    subgraph Ingestion["Ingestion: 1 repo each, httpx, 45s"]
        SC1["scanner-ingestion-betway-za"]
        SC2["scanner-ingestion-wsb"]
        SC3["scanner-ingestion-interbet"]
        SC4["scanner-ingestion-sunbet"]
        SC5["scanner-ingestion-easybet"]
        SC6["scanner-ingestion-supabets"]
    end

    subgraph Engine["scanner-engine (1 replica)"]
        ER["EntityResolver<br/>exact -> fuzzy -> dead-letter"]
        AGG["OddsAggregator<br/>md5 event hash, 120s freshness gate,<br/>best price per outcome, >= 2 bookmakers<br/>arb_new / arb_update / arb_expired"]
        MATH["Arbitrage Math<br/>M = sum(1/d) < 1, S = B*IP/M"]
    end

    REDIS[("Redis channel<br/>raw_odds_events")]
    REDIS2[("Redis channel<br/>live_arbitrage_alerts<br/>(same Redis 7 instance)")]
    PG[("PostgreSQL 15<br/>arb_events, team_aliases,<br/>scheduled_events")]

    subgraph Delivery["API & Frontend"]
        API["scanner-api<br/>FastAPI: SSE, /api/events, /api/health/*"]
        UI["scanner-frontend<br/>React: Arb Feed, Calendar, Status, Docs"]
    end

    USER["User<br/>places each leg manually<br/>at each bookmaker"]

    BM1 --> SC1
    BM2 --> SC2
    BM3 --> SC3
    BM4 --> SC4
    BM5 --> SC5
    BM6 --> SC6
    SC1 & SC2 & SC3 & SC4 & SC5 & SC6 -->|OddsEvent| REDIS
    REDIS --> ER
    ER --> AGG
    AGG --> MATH
    AGG -->|fixtures + arb history| PG
    AGG -->|arb_new / arb_update / arb_expired| REDIS2
    REDIS2 --> API
    PG --> API
    API -->|SSE stream| UI
    UI --> USER
```

## 2. Scrape-to-Alert Lifecycle (Sequence)

```mermaid
sequenceDiagram
    participant BM as Bookmaker public endpoint
    participant SC as Bookmaker repo (httpx)
    participant REDIS as Redis
    participant ENG as scanner-engine
    participant PG as Postgres
    participant API as scanner-api (SSE)
    participant UI as React Dashboard
    participant U as User

    loop Every 45s (plain, fixed interval), once per sport
        SC->>BM: GET public odds endpoint
        BM-->>SC: JSON / HTML payload
        SC->>SC: Map to OddsEvent (raw team names, event_id left unset)
        SC->>REDIS: PUBLISH raw_odds_events (one message per match)
        SC->>REDIS: SET heartbeat:bookmaker (TTL 135s)
    end
    REDIS->>ENG: OddsEvent
    ENG->>ENG: Validate, resolve team names (EntityResolver), md5(home|away|start_utc)
    ENG->>PG: Upsert scheduled_events row (non-fatal)
    ENG->>ENG: Drop prices older than 120s, best price per outcome, M = sum(1/d)
    alt M < 1, not previously active
        ENG->>REDIS: PUBLISH arb_new
        ENG->>PG: Insert arb_events row
    else M < 1, already active
        ENG->>REDIS: PUBLISH arb_update
        ENG->>PG: Update arb_events row
    else M >= 1, was active
        ENG->>REDIS: PUBLISH arb_expired (odds_moved)
        ENG->>PG: Mark arb_events row expired
    end
    REDIS->>API: live_arbitrage_alerts message (polled every ~1.1s per client)
    API->>UI: SSE event: arb_new / arb_update / arb_expired
    UI->>U: Show/update/fade out alert, recompute stakes against typed-in bankroll
    U->>BM: Manually place bets on each leg
```

## 3. Azure Infrastructure (South Africa North)

```mermaid
flowchart TB
    subgraph GH["GitHub (MadunaCapital, 12 repos)"]
        ACT["GitHub Actions deploy.yml (SP scanner-deploy-za), on push to main<br/>docker build :sha :latest -> push to ACR -><br/>az containerapp update"]
        TF["scanner-infra<br/>Terraform (azurerm)"]
    end

    subgraph StateRG["arbitrage-scanner-tfstate-rg"]
        STATE[("Storage account arbscannertfstateza<br/>remote Terraform state")]
    end

    subgraph Azure["arbitrage-scanner-rg -- South Africa North<br/>(subscription maduna-scanner)"]
        subgraph VNet["VNet 10.0.0.0/16"]
            subgraph CAEnv["Container Apps Environment (subnet 10.0.0.0/23)"]
                SCR["6 bookmaker scrapers<br/>0.25 vCPU / 0.5 GiB, 1 replica each"]
                ENG["scanner-engine<br/>0.5 vCPU / 1 GiB, 1 replica"]
                REDIS[("redis:7-alpine<br/>internal TCP :6379, short-name only<br/>1 replica")]
                API["scanner-api<br/>external ingress :8000<br/>1-5 replicas, scale at 200 concurrent"]
                FE["scanner-frontend<br/>nginx, external ingress :80<br/>1-3 replicas"]
            end
            subgraph PGNet["Delegated subnet 10.0.3.0/24"]
                PG[("Postgres Flexible Server -za<br/>PG 15, B1ms, private DNS,<br/>public access off")]
            end
        end
        ACR["Container Registry arbitragescannerregistryza (Basic)"]
        LOGS["Log Analytics<br/>30-day retention, 0.5 GB/day cap"]
    end

    USERBROWSER["User Browser"]
    BOOKS["Bookmaker public endpoints"]

    ACT -->|push image| ACR
    ACT -->|deploy revision| CAEnv
    TF -->|terraform apply| Azure
    TF -.->|state| STATE
    ACR -->|image pull| CAEnv
    SCR -->|HTTPS egress| BOOKS
    SCR --> REDIS
    ENG <--> REDIS
    API <--> REDIS
    ENG --> PG
    API --> PG
    CAEnv -.-> LOGS
    USERBROWSER --> FE
    USERBROWSER -->|SSE + REST| API
```

## 4. Entity Resolution — Three-Tier Matching Pipeline

```mermaid
flowchart TD
    IN["Incoming scraped team name<br/>(league, name)"] --> T1{"Tier 1:<br/>In-memory exact match<br/>~1 us"}
    T1 -->|hit| OUT["Canonical team name<br/>-> md5 event hash"]
    T1 -->|miss| T2{"Tier 2:<br/>RapidFuzz token_sort_ratio<br/>same league only"}
    T2 -->|score >= 88| CACHE["Write to in-memory cache"] --> OUT
    T2 -->|score < 88| T3["Tier 3:<br/>Dead-letter queue (deduped)<br/>manual review"]
    T3 --> RAW["Fallback: track under raw spelling<br/>(safe: missed arb, never a false one)"]
    T3 -->|admin links team| LINK["entity_store.persist_manual_link<br/>-> Postgres team_aliases"] --> CACHE
    STARTUP["Engine startup"] -->|load_aliases_into_resolver| CACHE
```

**Status:** wired into OddsAggregator.ingest() for both team names (closed 2026-09-28). team_aliases is only populated via manual links today -- there is no admin UI for the dead-letter queue yet, so unresolved names are tracked under their raw spelling until someone links them.

## 5. Health Monitoring — Heartbeats

```mermaid
flowchart LR
    SC["Each scraper<br/>(after every poll cycle)"] -->|"SET heartbeat:bookmaker_id<br/>EX 135 (3 x 45s)"| REDIS[("Redis")]
    ENG["scanner-engine<br/>(every 10s, even when idle)"] -->|"SET heartbeat:engine EX 30<br/>redis ping, postgres SELECT 1,<br/>messages processed, last DB write"| REDIS
    REDIS --> H1["GET /api/health/scrapers<br/>live / error / down per bookmaker"]
    REDIS --> H2["GET /api/health/system<br/>Redis + Postgres reachability + engine heartbeat"]
    H1 & H2 -->|polled every 4s| STATUS["Frontend Status page"]
```

**Status:** a missing/expired key is the 'down' signal -- no separate liveness checker exists or is needed.

## 6. Original Build Order (Historical)

This was the plan going in. Actual work followed a similar shape but diverged in the details -- notably, bookmaker integration turned out not to need the stealth/Cloudflare tooling this roadmap assumed, infra moved from AWS/ECS to Azure Container Apps, and a database layer, health monitoring and four more bookmakers were added as later milestones.

```mermaid
flowchart LR
    S1["1. Arb Math<br/>+ Unit Tests<br/>+ Backtest Harness"] --> S2["2. Normalization<br/>+ Entity Resolution"]
    S2 --> S3["3. Single Bookmaker<br/>Scraper + Freshness<br/>Circuit Breaker"]
    S3 --> S4["4. SSE Pipeline<br/>+ React Dashboard<br/>+ Staleness Indicator"]
    S4 --> S5["5. Infra/Deploy<br/>Terraform, ECS, CI/CD"]
```

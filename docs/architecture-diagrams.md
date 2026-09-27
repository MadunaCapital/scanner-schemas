# Architecture Diagrams — MadunaCapital Arbitrage Scanner

Reflects the **as-built** system (see [as-built-architecture.md](as-built-architecture.md)) — not the original brainstorm in [arbitrage-scanner-plan.md](arbitrage-scanner-plan.md), which assumed anti-bot evasion and AWS; neither turned out to be what was actually needed or used. Renders natively on GitHub; for a standalone browser view see `architecture-diagrams.html` in this same folder.

## 1. System Architecture (End-to-End Data Flow)

```mermaid
flowchart LR
    subgraph Sources["SA Bookmakers"]
        BM1["Betway ZA<br/>public JSON endpoint"]
        BM2["World Sports Betting<br/>public JSON endpoint"]
    end

    subgraph Ingestion["Ingestion (one repo per bookmaker)"]
        SC1["scanner-ingestion-betway-za<br/>plain httpx, no evasion"]
        SC2["scanner-ingestion-wsb<br/>plain httpx, no evasion"]
    end

    subgraph Engine["scanner-engine"]
        AGG["OddsAggregator<br/>exact-name event matching<br/>arb_new / arb_update / arb_expired"]
        MATH["Arbitrage Math<br/>implied prob + stake calc"]
    end

    subgraph Storage["Storage"]
        REDIS[("Redis<br/>pub/sub only, nothing at rest")]
        PG[("PostgreSQL<br/>arb history + entity dictionary")]
    end

    subgraph Delivery["API & Frontend"]
        API["scanner-api<br/>FastAPI SSE"]
        UI["scanner-frontend<br/>React Dashboard"]
    end

    USER["User<br/>Manual Bet Placement"]

    BM1 -->|odds/JSON| SC1
    BM2 -->|odds/JSON| SC2
    SC1 -->|raw OddsEvent| REDIS
    SC2 -->|raw OddsEvent| REDIS
    REDIS -->|RAW_ODDS_CHANNEL| AGG
    AGG --> MATH
    AGG -->|history| PG
    AGG -->|ARBITRAGE_CHANNEL| REDIS
    REDIS --> API
    API -->|SSE stream| UI
    UI --> USER
    USER -->|places bets manually| BM1
    USER -->|places bets manually| BM2
```

## 2. Scrape-to-Alert Lifecycle (Sequence)

```mermaid
sequenceDiagram
    participant BM as Bookmaker's public endpoint
    participant SC as Bookmaker repo (httpx)
    participant REDIS as Redis Pub/Sub
    participant AGG as OddsAggregator
    participant PG as Postgres
    participant API as scanner-api (SSE)
    participant UI as React Dashboard
    participant U as User

    loop Every 45s (plain, fixed interval)
        SC->>BM: GET public odds endpoint
        BM-->>SC: JSON payload
        SC->>SC: Map to OddsEvent (schema-defined, event_id left unset)
        SC->>REDIS: Publish to RAW_ODDS_CHANNEL
    end
    REDIS->>AGG: OddsEvent
    AGG->>AGG: Hash event (exact team-name match), compare best odds across bookmakers
    alt Margin < 100% (new arb)
        AGG->>REDIS: Publish arb_new
        AGG->>PG: Insert arb_events row
    else Still active, odds changed
        AGG->>REDIS: Publish arb_update
        AGG->>PG: Update arb_events row
    else No longer profitable
        AGG->>REDIS: Publish arb_expired
        AGG->>PG: Mark arb_events row expired
    end
    REDIS->>API: Pub/Sub message
    API->>UI: SSE event: arb_new / arb_update / arb_expired
    UI->>U: Show/update/remove alert, recompute stakes against typed-in bankroll
    U->>BM: Manually place bets on each leg
```

## 3. Azure Infrastructure (South Africa North)

```mermaid
flowchart TB
    subgraph GH["GitHub"]
        ACT["GitHub Actions<br/>Build + Push + Deploy<br/>(not yet built)"]
    end

    subgraph Azure["Azure -- South Africa North"]
        subgraph VNet["Virtual Network"]
            subgraph CAEnv["Container Apps Environment"]
                CA1["scanner-api<br/>external ingress, autoscaled"]
                CA2["scanner-engine<br/>1 replica, singleton"]
                CA3["scanner-ingestion-betway-za<br/>1 replica, singleton"]
                CA4["scanner-ingestion-wsb<br/>1 replica, singleton"]
            end
            REDIS[("Azure Cache for Redis<br/>private endpoint only")]
            PG[("Postgres Flexible Server<br/>VNet-integrated")]
        end
        ACR["Azure Container Registry"]
    end

    USERBROWSER["User Browser"]

    ACT -->|push image| ACR
    ACT -->|deploy revision| CA1
    ACT -->|deploy revision| CA2
    ACT -->|deploy revision| CA3
    ACT -->|deploy revision| CA4
    ACR --> CA1 & CA2 & CA3 & CA4
    CA1 <--> REDIS
    CA2 <--> REDIS
    CA3 --> REDIS
    CA4 --> REDIS
    CA2 <--> PG
    USERBROWSER --> CA1
```

## 4. Entity Resolution — Three-Tier Matching Pipeline

```mermaid
flowchart TD
    IN["Incoming scraped team name"] --> T1{"Tier 1:<br/>In-memory Exact Match<br/>sub-millisecond"}
    T1 -->|hit| OUT["Universal team name"]
    T1 -->|miss| T2{"Tier 2:<br/>RapidFuzz<br/>token_sort_ratio"}
    T2 -->|score >= 88%| CACHE["Write to in-memory cache<br/>+ persist to Postgres team_aliases"] --> OUT
    T2 -->|score < 88%| T3["Tier 3:<br/>Dead Letter Queue<br/>Manual Review"]
    T3 -->|admin links team| CACHE
```

**Status:** built and tested (`normalization.py`, `entity_store.py`), but **not yet called by `aggregator.py`** — event matching across bookmakers is exact-name-only today. See as-built-architecture.md's "Known gap" section.

## 5. Original Build Order (Historical)

This was the plan going in. Actual work followed a similar shape but diverged in the details — notably, bookmaker integration turned out not to need the stealth/Cloudflare tooling this roadmap assumed, and a database layer + full Azure infra were added as later, separate milestones once 2 bookmakers were working. See as-built-architecture.md for what's actually true today.

```mermaid
flowchart LR
    S1["1. Arb Math<br/>+ Unit Tests<br/>+ Backtest Harness"] --> S2["2. Normalization<br/>+ Entity Resolution"]
    S2 --> S3["3. Single Bookmaker<br/>Scraper + Freshness<br/>Circuit Breaker"]
    S3 --> S4["4. SSE Pipeline<br/>+ React Dashboard<br/>+ Staleness Indicator"]
    S4 --> S5["5. Infra/Deploy<br/>Terraform, ECS, CI/CD"]
```

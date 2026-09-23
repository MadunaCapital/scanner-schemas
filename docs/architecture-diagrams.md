# Architecture Diagrams — MadunaCapital Arbitrage Scanner

Companion diagrams for [arbitrage-scanner-plan.md](arbitrage-scanner-plan.md). Renders natively on GitHub; for a standalone browser view see `architecture-diagrams.html` in this same folder.

## 1. System Architecture (End-to-End Data Flow)

```mermaid
flowchart LR
    subgraph Sources["SA Bookmakers"]
        BM1[Betway ZA]
        BM2[Hollywoodbets]
        BM3[Supabets]
        BM4[World Sports Betting]
    end

    subgraph Ingestion["Ingestion Layer (ECS Fargate)"]
        SC1[Scraper: curl_cffi]
        SC2["Playwright Stealth<br/>Cloudflare Solver"]
    end

    subgraph Processing["Processing"]
        NORM["Normalization Engine<br/>Entity Resolution"]
        ENGINE["Arbitrage Detection Engine<br/>Implied Prob + Stake Calc"]
    end

    subgraph Storage["Storage & Cache"]
        REDIS[("Redis<br/>ElastiCache")]
        PG[("PostgreSQL<br/>Accounts, History")]
    end

    subgraph Delivery["API & Frontend"]
        API["FastAPI<br/>SSE Stream"]
        UI[React Dashboard]
    end

    USER["User<br/>Manual Bet Placement"]

    BM1 & BM2 & BM3 & BM4 -->|odds/JSON| SC1
    BM1 & BM2 & BM3 & BM4 -.->|JS challenge| SC2
    SC2 -->|cf_clearance cookie| SC1
    SC1 -->|raw JSON| NORM
    NORM -->|normalized events| REDIS
    NORM --> PG
    REDIS --> ENGINE
    ENGINE -->|arb_new/update/expired| REDIS
    REDIS -->|pub/sub| API
    API -->|SSE stream| UI
    UI --> USER
    USER -->|places bets| BM1 & BM2 & BM3 & BM4
```

## 2. Scrape-to-Alert Lifecycle (Sequence)

```mermaid
sequenceDiagram
    participant BM as Bookmaker Site
    participant PW as Playwright (stealth)
    participant CF as curl_cffi (fast client)
    participant NORM as Normalization Engine
    participant ENGINE as Arb Detection Engine
    participant REDIS as Redis Pub/Sub
    participant API as FastAPI SSE
    participant UI as React Dashboard
    participant U as User

    PW->>BM: Load page, solve Cloudflare challenge
    BM-->>PW: cf_clearance cookie + session
    PW->>CF: Export cookies + user agent
    loop Every polling interval / WebSocket tick
        CF->>BM: Request odds JSON (impersonated TLS)
        BM-->>CF: Raw odds payload
        CF->>NORM: Raw JSON
        NORM->>NORM: Map teams/markets, convert odds, hash event ID
        NORM->>REDIS: Publish normalized event
    end
    REDIS->>ENGINE: Normalized odds
    ENGINE->>ENGINE: Calculate implied probability + margin
    alt Margin < 100%
        ENGINE->>REDIS: Publish arb_new (stakes, ROI, expires_at)
        REDIS->>API: Pub/Sub message
        API->>UI: SSE event: arb_new
        UI->>U: Show alert + stake calculator
        U->>BM: Manually place bets on each leg
    else Margin >= 100%
        ENGINE->>ENGINE: Discard, no arb
    end
```

## 3. AWS Infrastructure (af-south-1)

```mermaid
flowchart TB
    subgraph GH["GitHub"]
        ACT["GitHub Actions<br/>Build + Push + Deploy"]
    end

    subgraph AWS["AWS af-south-1 (Cape Town)"]
        subgraph VPC["VPC"]
            subgraph Public["Public Subnets"]
                NAT[NAT Gateway]
                ALB["Application Load Balancer<br/>idle_timeout=3600, HTTP/2"]
            end
            subgraph Private["Private Subnets"]
                ECS1["ECS Fargate<br/>Scrapers"]
                ECS2["ECS Fargate<br/>FastAPI Backend"]
                REDIS[("ElastiCache Redis<br/>cache.c6g.large")]
                RDS[("RDS PostgreSQL")]
            end
        end
        ECR["ECR<br/>Docker Images"]
    end

    PROXY["Residential/Mobile<br/>Proxy Provider"]
    INTERNET(("SA Bookmaker Sites"))
    USERBROWSER["User Browser"]

    ACT -->|push image| ECR
    ACT -->|deploy| ECS1
    ACT -->|deploy| ECS2
    ECR --> ECS1
    ECR --> ECS2
    ECS1 -->|egress| NAT
    NAT --> PROXY
    PROXY -->|scrape| INTERNET
    ECS1 <--> REDIS
    ECS2 <--> REDIS
    ECS2 <--> RDS
    ALB --> ECS2
    USERBROWSER --> ALB
```

## 4. Entity Resolution — Three-Tier Matching Pipeline

```mermaid
flowchart TD
    IN["Incoming scraped team name"] --> T1{"Tier 1:<br/>Redis Exact Match<br/>sub-millisecond"}
    T1 -->|hit| OUT["Universal team_id"]
    T1 -->|miss| T2{"Tier 2:<br/>RapidFuzz<br/>token_sort_ratio"}
    T2 -->|score >= 88%| CACHE["Write to Redis cache"] --> OUT
    T2 -->|score < 88%| T3["Tier 3:<br/>Dead Letter Queue<br/>Manual Review"]
    T3 -->|admin links team| CACHE
```

## 5. Suggested Build Order (Roadmap)

```mermaid
flowchart LR
    S1["1. Arb Math<br/>+ Unit Tests<br/>+ Backtest Harness"] --> S2["2. Normalization<br/>+ Entity Resolution"]
    S2 --> S3["3. Single Bookmaker<br/>Scraper + Freshness<br/>Circuit Breaker"]
    S3 --> S4["4. SSE Pipeline<br/>+ React Dashboard<br/>+ Staleness Indicator"]
    S4 --> S5["5. Infra/Deploy<br/>Terraform, ECS, CI/CD"]
```

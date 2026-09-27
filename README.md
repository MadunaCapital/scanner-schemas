# scanner-schemas

Shared contracts for the MadunaCapital arbitrage scanner. This is the single source of truth for the data shapes and channel names that flow between every other repo — nobody defines their own copy.

## Contents

- `python/schemas/models.py` — Pydantic models (`OddsEvent`, `MarketOdds`, `ArbEvent`, `ArbStake`, `ArbExpiredEvent`), installable as a package
- `python/schemas/channels.py` — the two Redis channel name constants (`RAW_ODDS_CHANNEL`, `ARBITRAGE_CHANNEL`), so they can't silently drift between the repos that publish/subscribe to them
- `typescript/` — mirrored TypeScript types for the frontend
- `docs/as-built-architecture.md` — **start here** for what the project actually is today
- `docs/arbitrage-scanner-plan.md` + `docs/architecture-diagrams.md` — the original brainstorm and its diagrams, kept as a historical record (several specifics turned out different once actually built — see the as-built doc for what's current)

## Why this exists

Every service depends on agreeing on the exact odds event, arbitrage alert, and channel-name shape. Keeping that contract in its own versioned package means a schema change is a deliberate version bump other repos pull in, not a silent drift between services.

## Related repos

- [scanner-ingestion](https://github.com/MadunaCapital/scanner-ingestion) — shared, bookmaker-agnostic scraper toolkit
- [scanner-ingestion-betway-za](https://github.com/MadunaCapital/scanner-ingestion-betway-za) — Betway ZA scraper
- [scanner-ingestion-wsb](https://github.com/MadunaCapital/scanner-ingestion-wsb) — World Sports Betting scraper
- [scanner-engine](https://github.com/MadunaCapital/scanner-engine) — normalization + arbitrage detection + persistence
- [scanner-api](https://github.com/MadunaCapital/scanner-api) — FastAPI backend / SSE
- [scanner-frontend](https://github.com/MadunaCapital/scanner-frontend) — React dashboard
- [scanner-infra](https://github.com/MadunaCapital/scanner-infra) — Terraform (Azure) + CI/CD

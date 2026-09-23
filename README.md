# scanner-schemas

Shared contracts for the MadunaCapital arbitrage scanner. This is the single source of truth for the data shapes that flow between `scanner-ingestion`, `scanner-engine`, `scanner-api`, and `scanner-frontend` — nobody defines their own copy of these types.

## Contents

- `python/schemas/` — Pydantic models used by ingestion, engine, and API (installable as a package)
- `typescript/` — Mirrored TypeScript types for the frontend
- `docs/` — Architecture plan and diagrams for the whole project

## Why this exists

Normalization and detection depend on every service agreeing on the exact odds event and arbitrage alert shape. Keeping that contract in its own versioned package means a schema change is a deliberate version bump other repos pull in, not a silent drift between services.

## Related repos

- [scanner-ingestion](https://github.com/MadunaCapital/scanner-ingestion) — scrapers
- [scanner-engine](https://github.com/MadunaCapital/scanner-engine) — normalization + arbitrage detection
- [scanner-api](https://github.com/MadunaCapital/scanner-api) — FastAPI backend / SSE
- [scanner-frontend](https://github.com/MadunaCapital/scanner-frontend) — React dashboard
- [scanner-infra](https://github.com/MadunaCapital/scanner-infra) — Terraform + CI/CD

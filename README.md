# app-vmm — Standalone Codelist Labeling Stack for VMM based on DECIDe UC 0.1

Minimal, self-contained Docker Compose setup for the Codelist Labeling pipeline only.

## Services (19)

| Service | Purpose |
|---------|---------|
| `identifier` | Entry point (port 80) |
| `dispatcher` | URL routing |
| `database` | SPARQL auth layer |
| `virtuoso` | RDF triple store (port 8890 in dev) |
| `resource` | JSON:API for frontends |
| `cache` | Resource cache |
| `deltanotifier` | Event propagation |
| `sink` | No-op delta target |
| `migrations` | SPARQL migrations |
| `login` | Auth service |
| `mocklogin` | Mock auth (dev) |
| `frontend-harvesting` | Dashboard UI |
| `job-controller` | Pipeline orchestration |
| `scheduled-job-controller` | Cron scheduling |
| `annotation-job-splitter` | Task splitting |
| `harvest_singleton-job` | Singleton task creation |
| `codelist-labeling` | LLM annotation |
| `annotation-review` | Validation backend |
| `frontend-human-validator` | Human validation UI |

## Quick Start

```bash
# Start all services (production mode)
docker compose up -d

# Start with dev overrides (ports published, restart:no)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

## Configuration

### LLM codelist classifier
Edit `config/codelist/config.json` to set your LLM provider, API key, and model.

### Access the Dashboard
- Dashboard: http://dashboard.localhost (or http://localhost with hosts file)
- Human Validator: http://human-validator.localhost
- Virtuoso SPARQL (dev): http://localhost:8890/sparql

### Mock Login
In dev mode, use `mocklogin` at `/mock/sessions/` to authenticate without real credentials.

## What's NOT Included
This stack excludes: OParl harvesting, PDF harvesting, NER/NEL, OSLO/Ghent, search/embedding, data-space/DCAT, smart-search, policy-impact-report, and reporting services.

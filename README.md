# Familiar

> A local-first AI coding agent with a built-in knowledge base.

Familiar is a coding agent that lives in your repo and does real work — it reads the
codebase, plans changes, runs commands in a sandbox, and delivers pull requests. On top
of that it ships a full RAG knowledge base: drop in PDFs, ask questions, get cited
answers with cross-session memory.

It runs locally by default (SQLite + your local shell, zero external services) and
scales up to Postgres / Redis / Milvus / MinIO / an isolated container sandbox when you
need it.

## Features

- **Coding agent** — a DeepAgents + LangGraph pipeline that clones a repo, reads it,
  calls tools, executes in a sandbox (local shell or OpenSandbox container), and opens
  a PR as the deliverable.
- **Knowledge base** — PDF → layout parsing (DotsOCR) → heading-aware chunking →
  hybrid search (dense + BM25) → cited answers with a self-evaluation step and a
  human-approval interrupt. Includes cross-session semantic memory.
- **PR review** — pull a diff, apply review rules, record structured findings, and post
  them back to the PR as a comment.
- **Three forges** — Gitee, GitHub, and GitLab are all handled through one `scm` layer;
  the platform is inferred from the repo URL, credentials are injected per-command
  via `GIT_ASKPASS` (never persisted).
- **Observability** — LLM traces in Langfuse, structured events, and OpenTelemetry
  logs/metrics into Prometheus + Grafana, all keyed by `thread_id` / `run_id`.

## Architecture

Two product surfaces share one platform layer:

- **Coding agent** and **Knowledge base** are the two business surfaces.
- Auth, model/repo defaults, usage, sandbox, and observability are the platform.

See [`docs/architecture.md`](docs/architecture.md) for the full picture.

## Quick start

Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/), and
[bun](https://bun.sh) (for the frontend).

```bash
# 1. Backend
uv sync --extra dev
cp .env.example .env        # fill in at least one model API key

# 2. Frontend (in another terminal)
cd frontend
bun install
bun run dev                 # http://localhost:3000

# 3. Backend (in another terminal)
uv run uvicorn familiar.app:app --port 2024
```

The backend serves the dashboard at `http://localhost:2024` and the frontend dev
server proxies to it at `http://localhost:3000`.

### Optional services

Postgres, Redis, Milvus, MinIO, Langfuse, OpenSandbox, and the observability stack are
all in one compose file:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

By default nothing is required — the agent works with SQLite and your local shell.
Enable Postgres/Redis/sandbox/vector store by editing `.env` (see
[`.env.example`](.env.example)).

| Service     | Port  | Purpose                              |
|-------------|-------|--------------------------------------|
| Backend API | 2024  | FastAPI + agent runtime              |
| Frontend    | 3000  | Vite dev server                      |
| Postgres    | 5432  | checkpoints / store / memory         |
| Redis       | 6379  | event bus                            |
| Milvus      | 19530 | vector + BM25 search                 |
| MinIO       | 9002  | knowledge-base object storage        |
| Langfuse    | 3001  | LLM traces                           |
| Grafana     | 3002  | dashboards                           |
| Prometheus  | 9095  | metrics                              |
| Attu        | 8000  | Milvus admin UI                      |
| OpenSandbox | 8080  | container sandbox server             |

## Configuration

All configuration lives in `.env` (see [`.env.example`](.env.example)). The most
important knobs:

- `MAIN_MODEL` — the model the agent uses (default `deepseek-flash`).
- `PERSISTENCE_TYPE` — `sqlite` (default) or `postgres`.
- `SANDBOX_TYPE` — `local_shell` (default) or `opensandbox`.
- `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` — embedding used by the knowledge base.
- `DOTS_OCR_BASE_URL` — the layout-parsing service for PDF ingestion (optional; without
  it, PDFs fall back to the text layer only).

## Project layout

```
familiar/          Python package (backend)
  api/             FastAPI routers (dashboard + knowledge)
  core/            settings, auth, model, persistence, observability, memory
  tools/           agent tools (scm, web search, knowledge search, …)
  backends/        sandbox execution (local_shell / opensandbox)
  knowledge/       the knowledge base (parse → chunk → embed → answer)
  review/          PR review (diff / findings / publish)
frontend/          web UI (React + Vite + TypeScript)
deploy/            docker-compose + observability config + sandbox image
tests/             pytest unit tests + smoke scripts
scripts/           ops scripts (start, admin, rebuild, …)
docs/              architecture and guides
```

## Contributing

PRs and issues are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md). If you find a
security issue, see [`SECURITY.md`](SECURITY.md) first.

## License

[MIT](LICENSE) © Familiar contributors.

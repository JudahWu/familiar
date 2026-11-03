# Architecture

Familiar is two business surfaces sharing one platform layer.

```
┌─────────────────────────────────────────────────────────────────┐
│                          Frontend (Vite)                        │
│   agents · review · knowledge · login · settings · usage        │
└───────────────┬─────────────────────────────────┬───────────────┘
                │ /api/... (agent SSE)            │ /dashboard/api/...
                ▼                                 ▼
┌───────────────────────────────┐   ┌───────────────────────────────┐
│        Coding agent           │   │        Knowledge base          │
│  DeepAgents + LangGraph       │   │  LangGraph QA graph            │
│  tools / backends / review    │   │  parse / chunk / answer / mem  │
└───────────────┬───────────────┘   └───────────────┬───────────────┘
                │                                     │
                └──────────────┬──────────────────────┘
                               ▼
              ┌─────────────────────────────────┐
              │          Platform layer          │
              │ auth · model · store · sandbox   │
              │ scm · observability · memory     │
              └─────────────────────────────────┘
```

## Two business surfaces

### 1. Coding agent

The core product. A single user turn goes through:

`thread → intent classification → agent graph → tool calls → sandbox execution → diff/PR`

- **Graph & runtime**: `familiar/core/graph.py`, `runtime.py`, `streaming_runtime.py`.
- **Tools**: `familiar/tools/` — `fetch_url`, `web_search`, `search_knowledge`, and the
  SCM/PR tools (`open_gitee_pull_request`, `publish_gitee_pr_comment`, …).
- **Skills**: `familiar/skills/` — domain prompts injected by task type
  (`ai-coding-implementation`, `code-review`, `repo-bootstrap-analysis`).
- **Sandbox**: `familiar/backends/` — `local_shell` (run on the host) or `opensandbox`
  (isolated container). Permissions in `backends/permissions.py`.
- **PR review** is a mode of the coding agent, not a separate product: `task_kind="review"`
  routes through the same graph/tools, but the deliverable is a PR comment
  (`familiar/review/`), not code changes.

### 2. Knowledge base

A standalone HTTP service with its own LangGraph QA graph, also callable from the agent
via the `search_knowledge` tool.

- **Ingestion**: PDF → DotsOCR layout parsing (`familiar/knowledge/dotsocr/`) →
  heading-aware chunking + inline-image extraction (`chunk.py`) → embedding.
- **Retrieval**: Milvus hybrid search — dense (multimodal embedding) + sparse (BM25).
- **Answering**: `familiar/knowledge/graph.py` — rewrite → context recall → retrieve →
  generate → self-evaluate → human-approval interrupt (mirrors the reference
  Multimodal_RAG graph).
- **Memory**: short-term turns in Postgres (`chat.py`), cross-session semantic recall
  via a Milvus index that is fully rebuildable from Postgres (`memory.py`).

## Platform layer

- **Auth** (`core/auth.py`) — JWT in an httpOnly cookie, shared with SSE.
- **Model** (`core/model.py`) — a single factory for chat/intent/judge models.
- **Store** (`familiar/store/`) — SQLite or Postgres behind one interface; holds
  threads, runs, findings, and the knowledge base metadata.
- **SCM** (`tools/scm.py`) — one layer for Gitee/GitHub/GitLab; platform inferred from
  the repo URL, credentials injected per-command via `GIT_ASKPASS`.
- **Observability** (`core/observability.py`, `telemetry.py`) — Langfuse for LLM traces,
  OpenTelemetry for logs/metrics, Prometheus + Grafana for dashboards.

## Storage topology

| What                                  | Where                                        |
|---------------------------------------|----------------------------------------------|
| Threads / runs / findings / kb meta   | Postgres (or SQLite) — `familiar/store/`     |
| Agent + QA graph checkpoints          | Postgres / SQLite (LangGraph checkpointer)   |
| Knowledge chunks (dense + BM25)       | Milvus `knowledge_chunks`                    |
| Cross-session memory index            | Milvus `knowledge_memory` (rebuildable)      |
| Original PDFs + extracted images      | MinIO                                        |
| LLM traces                            | Langfuse (ClickHouse)                        |
| Logs / metrics                        | OTel collector → ClickHouse / Prometheus     |

## Backend package layout

```
familiar/
  app.py            FastAPI app + router assembly
  cli.py            console entry point
  api/              dashboard + knowledge routers
  core/             settings, auth, model, graph, runtime, persistence,
                    observability, memory/, middleware/
  tools/            agent tools + scm (three forges)
  backends/         local_shell / opensandbox + permissions
  store/            sqlite_store / postgres_store
  skills/           domain skill prompts
  knowledge/        parse → chunk → embed → answer → memory
  review/           diff / findings / engine / publish
```

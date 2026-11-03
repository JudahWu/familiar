# Contributing

Thanks for your interest in Familiar. This is a short guide to get you from clone to
pull request.

## Development setup

Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/), [bun](https://bun.sh/).

```bash
git clone <repo-url>
cd <repo>

# backend
uv sync --extra dev
cp .env.example .env    # add your model API key(s)

# frontend
cd frontend
bun install
```

Run the backend with `uv run uvicorn familiar.app:app --port 2024` and the frontend
with `bun run dev`. Optional services (Postgres, Milvus, …) via
`docker compose --env-file .env -f deploy/docker-compose.yml up -d`.

## Code style

- **Backend**: Ruff. CI enforces the bug-catching rules (`ruff check --select E9,F`);
  run `uv run ruff check familiar tests` before committing. The full rule set is
  defined in `pyproject.toml`; style-only rules are being adopted incrementally.
- **Frontend**: ESLint + Prettier (`bun run lint`, `bun run format`).
- Keep comments and docstrings useful; the codebase mixes Chinese and English and both
  are fine, but a new module should be internally consistent.

## Tests

```bash
uv run pytest              # offline unit tests (fast, no external services)
uv run pytest -m smoke     # smoke tests — need Milvus/Postgres/LLM/etc.
```

- Unit tests live in `tests/unit/` and must not touch the network or external services.
- `tests/verify_*.py` are legacy smoke scripts; they're wrapped by `tests/test_smoke.py`
  and skipped by default.

## Making a change

1. Open an issue first for anything non-trivial, so we can agree on the approach.
2. Keep commits small and focused; write a clear commit message.
3. Add or update tests for behavior changes.
4. Run `uv run pytest` and `uv run ruff check --select E9,F familiar tests` locally.
5. Open a PR; CI runs lint, typecheck, tests, and a frontend build.

## Conventions worth knowing

- `PERSISTENCE_TYPE=sqlite` is the zero-dependency default; the Postgres path is
  exercised in CI-optional integration tests only.
- Credentials for Gitee/GitHub/GitLab are injected per-command via `GIT_ASKPASS` and
  never written to disk — don't put tokens into URLs or `.git/config`.
- The `dotsocr/` subtree is vendored from the reference Multimodal_RAG project and is
  intentionally kept close to upstream (its lint noise is ignored in `pyproject.toml`).

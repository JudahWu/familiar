# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Coding agent: DeepAgents + LangGraph pipeline with sandbox execution (local shell or
  OpenSandbox container) and PR delivery.
- Knowledge base: PDF layout parsing (DotsOCR) → heading-aware chunking → hybrid
  retrieval (dense + BM25) → cited answers with self-evaluation and a human-approval
  interrupt.
- Cross-session semantic memory for the knowledge base (Postgres as source of truth,
  rebuildable Milvus index).
- PR review mode: diff parsing, structured findings, and PR comments.
- Multi-forge support: Gitee, GitHub, and GitLab behind one `scm` layer.
- Observability: Langfuse LLM traces, OpenTelemetry logs/metrics, Prometheus + Grafana.
- CI: Ruff, pytest, ESLint, `tsc`, and a frontend build.

### Changed

- Renamed the package to `familiar` and reorganized the backend into `core`, `tools`,
  `knowledge`, `review`, and `store` subpackages.

## [0.1.0] - 2026-11-02

Initial open-source release.

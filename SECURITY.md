# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities. Report them
privately via [GitHub's private vulnerability reporting](https://github.com/<owner>/<repo>/security/advisories/new)
(or email the maintainers directly). We'll acknowledge within a few days and work on a
fix.

## Secrets & credentials

Familiar reads credentials from `.env`, which is git-ignored and must never be
committed. `.env.example` contains only `YOUR_*` placeholders.

Sensitive values include (non-exhaustive):

- Model API keys: `DEEPSEEK_API_KEY`, `ALIBABA_API_KEY`
- Langfuse: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
- Langfuse/ClickHouse/MinIO infra secrets: `LF_SALT`, `LF_ENCRYPTION_KEY`,
  `LF_NEXTAUTH_SECRET`, `LF_CH_PWD`, `LF_MINIO_PWD`
- Dashboard auth: `DASHBOARD_JWT_SECRET`, `DASHBOARD_USERS` (password hashes)
- Object storage: `S3_ACCESS_KEY`, `S3_SECRET_KEY`
- Forge tokens: `GITEE_TOKEN`, `GITHUB_TOKEN`, `GITLAB_TOKEN`

### Before making a repository public

If this repository was previously private, do all of the following **before** flipping
it to public:

1. **Rotate every real credential** that has ever been present in the repository
   (whether in `.env`, a config file, or a past commit). Assume any value that ever hit
   the repo is compromised.
2. **Scan git history for leaked secrets** — e.g. with
   [gitleaks](https://github.com/gitleaks/gitleaks) (`gitleaks detect --report-format sarif`).
   Note that rotation (step 1) is the only real fix; deleting history is cosmetic.
3. If you must rewrite history, use `git filter-repo` — but only after rotating, and
   be aware it invalidates all clone/PR references.

## Notes

- Forge tokens are injected per-command via `GIT_ASKPASS` and are never written into
  repo URLs or `.git/config` — keep it that way in new code.
- The sandbox image (`deploy/sandbox/`) runs as root inside the container; use
  short-lived, fine-grained tokens if you wire real credentials into it.

# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately rather than opening a public issue.

**Email:** dawei.lin7689@gmail.com (Ta-Wei Lin, maintainer)

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce it (a minimal example is ideal).
- Which version/commit you tested against.

You should receive an acknowledgment within a few days. Once a fix is
available, a new release/tag will be published and the reporter credited
(unless anonymity is requested). Please allow a reasonable amount of time to
fix the issue before any public disclosure.

## Supported Versions

This is a template repository with a single, continuously updated `main`
branch; there are no maintained release branches. Security fixes are applied
to `main` only; if you've forked or vendored this template into your own
project, you're responsible for pulling in fixes yourself.

## Known, Intentional Simplifications

This project is a **template**, not a hardened production deployment. A few
things are deliberately simplified for clarity and are documented as such in
[`docs/technical/api-conventions.md`](docs/technical/api-conventions.md) and
[`docs/technical/operations.md`](docs/technical/operations.md); please check
there before filing a report, since these are known trade-offs rather than
bugs:

- **`SECRET_KEY` ships with a placeholder default** (`change-me-in-production-...`).
  You are expected to set a real, random `SECRET_KEY` via environment variable
  before deploying anywhere reachable; see `.env.example`.
- **JWT claims are not re-checked against the database on every request.** A
  deactivated/deleted user's token stays valid until it expires
  (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES`). If your deployment needs stronger
  revocation guarantees, add a revocation check (e.g. a denylist keyed by
  token id); this template doesn't include one.
- **The hot-reload admin endpoint** (`POST /api/v1/admin/plugins/{name}/reload`)
  re-imports and re-executes plugin code on a live process. It's
  `require_admin`-gated, but treat the admin role as fully trusted: this
  endpoint is intentionally powerful, not a sandboxed operation.

If you believe one of these deliberate trade-offs is insufficient for a
*template's* default posture (as opposed to your own deployment's specific
requirements), that's still worth reporting; we may tighten the default.

## Scope

This policy covers the code in this repository. It does not cover
vulnerabilities in third-party dependencies (`requirements.txt`); please
report those to the respective upstream projects. If a dependency
vulnerability is exploitable specifically because of how this template uses
it, that is in scope here.

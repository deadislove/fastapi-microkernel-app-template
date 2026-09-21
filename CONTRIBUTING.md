# Contributing

Thanks for considering a contribution to `fastapi-microkernel-app-template`. This is a template project demonstrating a Microkernel architecture on FastAPI, so contributions that keep it a clean, well-documented reference are especially welcome.

By participating, you're expected to follow the [Code of Conduct](./CODE_OF_CONDUCT.md).

## Before you start

Read [`docs/technical/architecture.md`](./docs/technical/architecture.md) first if you haven't. This project enforces a strict module-boundary discipline (kernel never imports a plugin, plugins never import each other, etc.); understanding *why* before changing code will save you a round-trip.

## Setting up your environment

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
```

Run the app: `uvicorn app.main:app --reload` (Swagger UI at `http://localhost:8000/docs`).

## Before opening a pull request

All three of these must pass; they're what `pytest` itself enforces, so a green `pytest` run covers the first two automatically:

```bash
pytest                                          # full test suite
python scripts/check_architecture_boundaries.py # module-boundary rules (also runs inside pytest)
ruff check . --fix                              # lint
```

See [`docs/technical/testing.md`](./docs/technical/testing.md) for what the test suite covers and the conventions behind it (e.g. a new static check should have a test proving it actually catches a violation, not just that it passes against clean code).

## Adding or changing a plugin

Use the scaffold generator for a new plugin rather than copying an existing one by hand:

```bash
python scripts/new_plugin.py your_plugin_name
```

This produces a skeleton already wired to the current `AbstractPlugin` contract (`KernelContext`, `dependencies`, `api_version`). See [`docs/technical/plugin-development.md`](./docs/technical/plugin-development.md) for the full contract, conventions, and what's optional vs. required.

The two shipped plugins (`user_plugin`, `product_plugin`) are the reference implementation; if you're unsure how something should look, match their style.

## Changing the kernel (`app/core/`)

Changes here affect every plugin, so:

- Explain the *why* in your PR description, not just the *what*.
- If you change `AbstractPlugin`'s contract (a new required `KernelContext` field, a new abstract method), consider whether it's a breaking change that should bump `KERNEL_API_VERSION` (`app/core/plugin_base.py`); see the `api_version` section of `plugin-development.md`.
- Update `docs/technical/architecture.md` alongside the code. Stale architecture docs are worse than none: they actively mislead the next contributor.

## Commit messages and PRs

- Keep commits focused; one logical change per commit is easier to review than a large mixed diff.
- Write commit messages that explain *why*, not just *what*: the diff already shows what changed.
- In the PR description, include a short test plan (what you ran, what you'd expect a reviewer to check).

## Documentation

If your change affects behavior described in `docs/technical/*.md`, update the relevant document in the same PR. If you're not sure which file, `docs/technical/README.md` has a one-line summary of what each covers.

## Reporting bugs / requesting features

Open a GitHub issue. For security vulnerabilities, do **not** open a public issue; see [SECURITY.md](./SECURITY.md) instead.

## License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](./LICENSE).

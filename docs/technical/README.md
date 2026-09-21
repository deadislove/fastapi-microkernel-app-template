# Technical Documentation

This directory documents how `fastapi-microkernel-app-template` is built and how to extend it. It's aimed at engineers integrating with, or contributing to, this codebase.

| Document | Covers |
|---|---|
| [architecture.md](./architecture.md) | The Microkernel design: kernel vs. plugins vs. facades, the request/startup lifecycle, and the module-boundary rules that keep them decoupled. |
| [plugin-development.md](./plugin-development.md) | How to write a plugin: the `AbstractPlugin` contract, `KernelContext`, dependency declarations, versioning, the scaffold tool, and testing conventions. |
| [operations.md](./operations.md) | Configuration reference, the health/introspection endpoint, plugin load modes, hot-reloading a single plugin, and deployment notes. |
| [api-conventions.md](./api-conventions.md) | The `Result`/`PluginError` pattern, HTTP status mapping, authentication, and rate limiting conventions used across every endpoint. |
| [testing.md](./testing.md) | The test suite: fixtures, what each test file covers, and the testing conventions (proving a check catches something, not touching the filesystem for scaffold tests, cleaning up injected fake plugins). |

For a fast "clone and run" quick start, see the top-level [README.md](../../README.md) instead; these documents assume you've already got the app running and want to understand *why* it's built this way, or *how* to add to it.

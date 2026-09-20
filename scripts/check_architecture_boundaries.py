"""
Static check for the Microkernel module-boundary rules documented in
docs/spec/done/microkernel-architecture-improvements.md (S3.11 / S3.13) and
docs/spec/done/microkernel-architecture-refinements.md (S4.4):

  1. app.plugins.<a> must not import app.plugins.<b> for a != b — plugins may
     only reach each other via app.core.registry.service_registry or
     app.core.hooks.event_bus (never each other's Service/model/schema
     classes, and never each other's SQLAlchemy tables/Foreign Keys).
  2. app.api.v1.* (the kernel route layer) must not import
     app.plugins.*.schemas / .service / .models.
  3. app.core.* (the kernel) must not import app.plugins.* at all.
  4. A plugin's `plugin.py` (its AbstractPlugin lifecycle hooks) must not
     import the `service_registry`/`event_bus` singletons directly — it must
     reach them through the `ctx: KernelContext` parameter instead, since
     `ctx.service_registry`/`ctx.event_bus` are scoped views that tag the
     plugin's name for hot-reload cleanup. This rule is deliberately scoped
     to `plugin.py` only: a plugin's `service.py`/`router.py` (which never
     receive a `ctx`) MAY import `service_registry`/`event_bus` directly to
     call `.resolve(...)`/`.emit(...)` — neither registers anything, so
     neither needs scoping. See refinements.md S4.1/S4.2/S4.4 for the full
     reasoning.

Run directly: `python scripts/check_architecture_boundaries.py`
Also wired into the test suite via tests/test_architecture_boundaries.py, so
`pytest` is the enforcement gate for this repo (it has no CI pipeline of its
own to hook into).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# Rule 4: names a plugin.py must never import from these modules — must come
# through ctx.service_registry/ctx.event_bus instead.
_CTX_ONLY_NAMES = {
    "app.core.registry": {"service_registry"},
    "app.core.hooks": {"event_bus"},
}


def _iter_py_files(root: Path):
    yield from sorted(root.rglob("*.py"))


def _module_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def _from_imports(path: Path) -> list[tuple[str, str]]:
    """Returns (module, imported_name) for every top-level `from X import Y`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                pairs.append((node.module, alias.name))
    return pairs


def _module_name_for(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = rel.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _plugin_name(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == "app" and parts[1] == "plugins":
        return parts[2]
    return None


def check() -> list[str]:
    violations: list[str] = []

    for path in _iter_py_files(APP_ROOT):
        own_module = _module_name_for(path)
        own_plugin = _plugin_name(own_module)
        imports = _module_imports(path)

        for imported in imports:
            imported_plugin = _plugin_name(imported)

            # Rule 1: a plugin importing a DIFFERENT plugin's internals.
            if own_plugin and imported_plugin and imported_plugin != own_plugin:
                violations.append(
                    f"{path}: plugin '{own_plugin}' imports plugin "
                    f"'{imported_plugin}' ({imported}) directly — use "
                    f"service_registry/event_bus instead."
                )

            # Rule 2: kernel route layer importing plugin internals.
            if (
                own_module.startswith("app.api.v1.")
                and imported_plugin
                and any(
                    imported.startswith(f"app.plugins.{imported_plugin}.{suffix}")
                    for suffix in ("schemas", "service", "models")
                )
            ):
                violations.append(
                    f"{path}: kernel route module imports plugin internal "
                    f"'{imported}' — core routes must stay plugin-agnostic."
                )

            # Rule 3: kernel (app.core.*) importing any plugin.
            if own_module.startswith("app.core.") and imported_plugin:
                violations.append(
                    f"{path}: kernel module imports plugin '{imported_plugin}' "
                    f"({imported}) — the kernel must stay plugin-agnostic."
                )

        # Rule 4: a plugin's `plugin.py` bypassing ctx for service_registry/event_bus.
        if own_plugin and own_module.endswith(".plugin"):
            for module, name in _from_imports(path):
                if name in _CTX_ONLY_NAMES.get(module, set()):
                    violations.append(
                        f"{path}: plugin.py imports '{name}' from '{module}' "
                        f"directly — lifecycle hooks must use ctx.service_registry"
                        f"/ctx.event_bus instead."
                    )

    return violations


def main() -> int:
    violations = check()
    if violations:
        print("Architecture boundary violations found:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            f"\n{len(violations)} violation(s). See "
            "docs/spec/done/microkernel-architecture-improvements.md S3.11 and "
            "docs/spec/done/microkernel-architecture-refinements.md S4.4."
        )
        return 1
    print("No architecture boundary violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Phase 4 (3.14): the scaffold generator's rendering logic must produce
syntactically valid, on-contract plugin code. This deliberately does NOT
call `scripts/new_plugin.py main()` — that writes into the real
app/plugins/ directory, which a test run must never do as a side effect.
Instead it exercises `_render()` directly (pure string generation, no I/O)
and compiles each output.

An actual end-to-end run (scaffold a plugin into a scratch copy of the repo,
run its generated test, run the full suite) was performed manually while
building this feature; see the Phase 4 checklist in
docs/spec/done/microkernel-architecture-improvements.md for that trace.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from new_plugin import _pascal_case, _render  # noqa: E402


def test_pascal_case():
    assert _pascal_case("billing_plugin") == "BillingPlugin"
    assert _pascal_case("inventory") == "Inventory"


def test_render_produces_expected_files():
    files = _render("billing_plugin")
    assert set(files.keys()) == {
        "__init__.py",
        "plugin.py",
        "models.py",
        "schemas.py",
        "service.py",
        "router.py",
        "__test__billing_plugin",
    }


def test_render_output_is_syntactically_valid_python():
    files = _render("billing_plugin")
    for filename, content in files.items():
        py_filename = filename if filename.endswith(".py") else "test_billing_plugin.py"
        compile(content, py_filename, "exec")  # raises SyntaxError if malformed


def test_render_plugin_module_follows_current_contract():
    files = _render("billing_plugin")
    plugin_src = files["plugin.py"]
    assert "class BillingPlugin(AbstractPlugin):" in plugin_src
    assert 'name = "billing_plugin"' in plugin_src
    assert "async def register(self, app: FastAPI, ctx: KernelContext)" in plugin_src
    assert "async def boot(self, app: FastAPI, ctx: KernelContext)" in plugin_src
    assert "async def shutdown(self, app: FastAPI, ctx: KernelContext)" in plugin_src
    assert "ctx.service_registry.provide(" in plugin_src
    assert "as _models" in plugin_src  # avoids the `app` parameter shadowing bug

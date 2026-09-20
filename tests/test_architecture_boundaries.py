"""
Enforces the Microkernel module-boundary rules (see docs/technical/architecture.md,
"Module boundaries", and the docstring of scripts/check_architecture_boundaries.py
for what the four rules are and why) as part of the regular test suite —
`.github/workflows/CI.yaml` also runs the script directly as its own step,
but wiring it into `pytest` too means a local `pytest` run catches a
violation just as reliably as CI does, without needing to remember a second
command.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_architecture_boundaries as boundaries  # noqa: E402


def test_no_architecture_boundary_violations():
    violations = boundaries.check()
    assert violations == [], "\n" + "\n".join(violations)


# ── Proof the checker actually catches something (not a no-op) ──────────────────

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fake_app_root(tmp_path, monkeypatch):
    """
    A minimal two-plugin `app/` tree under a real directory literally named
    `app` (the checker's module-name resolution is relative-path-based, so it
    must be named `app` to behave like the real one). Individual tests add
    the specific violation they want to prove gets caught.
    """
    root = tmp_path / "app"
    _write(root / "__init__.py", "")
    _write(root / "core" / "__init__.py", "")
    _write(root / "core" / "registry.py", "service_registry = object()\n")
    _write(root / "core" / "hooks.py", "event_bus = object()\n")
    _write(root / "api" / "__init__.py", "")
    _write(root / "api" / "v1" / "__init__.py", "")
    _write(root / "api" / "v1" / "health.py", "status = 'ok'\n")
    _write(root / "plugins" / "__init__.py", "")
    _write(root / "plugins" / "plugin_a" / "__init__.py", "")
    _write(
        root / "plugins" / "plugin_a" / "plugin.py",
        "class PluginA:\n    name = 'plugin_a'\n",
    )
    _write(root / "plugins" / "plugin_b" / "__init__.py", "")
    _write(
        root / "plugins" / "plugin_b" / "plugin.py",
        "class PluginB:\n    name = 'plugin_b'\n",
    )
    monkeypatch.setattr(boundaries, "APP_ROOT", root)
    return root


def test_clean_fake_tree_has_no_violations(fake_app_root):
    assert boundaries.check() == []


def test_rule1_catches_plugin_importing_another_plugin(fake_app_root):
    _write(
        fake_app_root / "plugins" / "plugin_a" / "plugin.py",
        "from app.plugins.plugin_b.plugin import PluginB  # noqa\n"
        "class PluginA:\n    name = 'plugin_a'\n",
    )
    violations = boundaries.check()
    assert any("imports plugin 'plugin_b'" in v for v in violations)


def test_rule2_catches_kernel_route_importing_plugin_internals(fake_app_root):
    _write(
        fake_app_root / "api" / "v1" / "health.py",
        "from app.plugins.plugin_a.service import Something  # noqa\n",
    )
    violations = boundaries.check()
    assert any("kernel route module imports plugin internal" in v for v in violations)


def test_rule3_catches_kernel_importing_a_plugin(fake_app_root):
    _write(
        fake_app_root / "core" / "hooks.py",
        "event_bus = object()\n"
        "from app.plugins.plugin_a.plugin import PluginA  # noqa\n",
    )
    violations = boundaries.check()
    assert any("kernel module imports plugin" in v for v in violations)


def test_rule4_catches_plugin_py_bypassing_ctx_for_service_registry(fake_app_root):
    _write(
        fake_app_root / "plugins" / "plugin_a" / "plugin.py",
        "from app.core.registry import service_registry  # noqa\n"
        "class PluginA:\n    name = 'plugin_a'\n",
    )
    violations = boundaries.check()
    assert any("must use ctx.service_registry" in v for v in violations)


def test_rule4_catches_plugin_py_bypassing_ctx_for_event_bus(fake_app_root):
    _write(
        fake_app_root / "plugins" / "plugin_a" / "plugin.py",
        "from app.core.hooks import event_bus  # noqa\n"
        "class PluginA:\n    name = 'plugin_a'\n",
    )
    violations = boundaries.check()
    assert any("must use ctx.service_registry" in v for v in violations)


def test_rule4_does_not_flag_service_py_importing_event_bus(fake_app_root):
    """service.py/router.py never receive a ctx — direct emit()/resolve() is fine."""
    _write(
        fake_app_root / "plugins" / "plugin_a" / "service.py",
        "from app.core.hooks import event_bus  # noqa\n"
        "from app.core.registry import service_registry  # noqa\n",
    )
    assert boundaries.check() == []

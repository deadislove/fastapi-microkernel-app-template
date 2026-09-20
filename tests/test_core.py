"""
Tests for the microkernel core: registry, event bus, JWT service, and password utils.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.config import settings
from app.core.errors import PluginError, PluginErrorCode
from app.core.hooks import EventBus
from app.core.loader import PluginLoader, PluginLoadError
from app.core.plugin_base import KERNEL_API_VERSION, AbstractPlugin, is_api_version_compatible
from app.core.registry import (
    PluginRegistry,
    PluginState,
    ServiceRegistry,
    plugin_registry,
    service_registry,
)
from app.infrastructure.jwt import JWTService
from app.infrastructure.password import hash_password, verify_password

# ── Registry ──────────────────────────────────────────────────────────────────

class _FakePlugin(AbstractPlugin):
    name = "fake"

    async def register(self, app, ctx): ...
    async def boot(self, app, ctx): ...
    async def shutdown(self, app, ctx): ...


def test_registry_register_and_get():
    reg = PluginRegistry()
    p = _FakePlugin()
    reg.register(p)
    assert reg.get("fake") is p


def test_registry_duplicate_raises():
    reg = PluginRegistry()
    reg.register(_FakePlugin())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_FakePlugin())


def test_registry_unregister():
    reg = PluginRegistry()
    reg.register(_FakePlugin())
    reg.unregister("fake")
    assert reg.get("fake") is None


def test_registry_nameless_plugin_raises():
    class _Nameless(AbstractPlugin):
        name = ""
        async def register(self, app, ctx): ...
        async def boot(self, app, ctx): ...
        async def shutdown(self, app, ctx): ...

    reg = PluginRegistry()
    with pytest.raises(ValueError, match="non-empty"):
        reg.register(_Nameless())


# ── EventBus ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_bus_emit_calls_handler():
    bus = EventBus()
    received = []

    async def handler(**kwargs):
        received.append(kwargs)

    bus.subscribe("test.event", handler)
    await bus.emit("test.event", value=42)
    assert received == [{"value": 42}]


@pytest.mark.asyncio
async def test_event_bus_bad_handler_does_not_propagate():
    bus = EventBus()

    async def bad_handler(**kwargs):
        raise RuntimeError("boom")

    bus.subscribe("test.event", bad_handler)
    # Should not raise — bad handlers are logged and swallowed
    await bus.emit("test.event")


@pytest.mark.asyncio
async def test_event_bus_unsubscribe():
    bus = EventBus()
    called = []

    async def handler(**kwargs):
        called.append(True)

    bus.subscribe("ev", handler)
    bus.unsubscribe("ev", handler)
    await bus.emit("ev")
    assert called == []


# ── EventBus introspection ────────────────────────────────────────

def test_event_bus_subscriptions_reports_owners():
    async def handler(**kwargs):
        ...

    bus = EventBus()
    bus.subscribe("user.created", handler, owner="plugin_a")
    bus.subscribe("user.created", handler, owner="plugin_b")
    bus.subscribe("product.created", handler)  # no owner — bypassed ctx

    subs = bus.subscriptions()

    assert subs["user.created"] == ["plugin_a", "plugin_b"]
    assert subs["product.created"] == ["<unowned>"]


def test_event_bus_subscriptions_omits_events_with_no_subscribers():
    async def handler(**kwargs):
        ...

    bus = EventBus()
    bus.subscribe("ev", handler, owner="p1")
    bus.unsubscribe("ev", handler)

    assert bus.subscriptions() == {}


def test_event_bus_subscriptions_reflects_unsubscribe_all_from():
    async def handler(**kwargs):
        ...

    bus = EventBus()
    bus.subscribe("ev", handler, owner="p1")
    bus.subscribe("ev", handler, owner="p2")
    bus.unsubscribe_all_from("p1")

    assert bus.subscriptions() == {"ev": ["p2"]}


# ── JWT ───────────────────────────────────────────────────────────────────────

def test_jwt_roundtrip():
    svc = JWTService(secret="test-secret", algorithm="HS256", access_expire_minutes=30, refresh_expire_days=7)
    token = svc.create_access_token("user-123", extra_claims={"roles": ["admin"]})
    result = svc.decode_access_token(token)
    assert result.is_ok()
    payload = result.unwrap()
    assert payload.sub == "user-123"
    assert "admin" in payload.roles


def test_jwt_invalid_token():
    svc = JWTService(secret="test-secret", algorithm="HS256", access_expire_minutes=30, refresh_expire_days=7)
    result = svc.decode_access_token("not.a.valid.token")
    assert result.is_err()
    assert result.unwrap_err().code == PluginErrorCode.TOKEN_INVALID


def test_jwt_expired_token():
    svc = JWTService(secret="test-secret", algorithm="HS256", access_expire_minutes=-1, refresh_expire_days=7)
    token = svc.create_access_token("user-123")
    result = svc.decode_access_token(token)
    assert result.is_err()
    assert result.unwrap_err().code == PluginErrorCode.TOKEN_EXPIRED


# ── Password ──────────────────────────────────────────────────────────────────

def test_password_hash_and_verify():
    hashed = hash_password("supersecret")
    assert verify_password("supersecret", hashed)
    assert not verify_password("wrongpassword", hashed)


# ── PluginError ───────────────────────────────────────────────────────────────

def test_plugin_error_constructors():
    err = PluginError.not_found("User", "42")
    assert err.code == PluginErrorCode.NOT_FOUND
    assert "42" in err.message

    err2 = PluginError.invalid_credentials()
    assert err2.code == PluginErrorCode.INVALID_CREDENTIALS


# ── PluginRegistry state tracking ───────────────────────────────

def test_registry_tracks_pending_state_on_register():
    reg = PluginRegistry()
    reg.register(_FakePlugin())
    assert reg.state_of("fake") == PluginState.PENDING
    assert reg.error_of("fake") is None


def test_registry_set_state_transitions_and_records_error():
    reg = PluginRegistry()
    reg.register(_FakePlugin())

    reg.set_state("fake", PluginState.REGISTERED)
    assert reg.state_of("fake") == PluginState.REGISTERED

    reg.set_state("fake", PluginState.FAILED, error="boom")
    assert reg.state_of("fake") == PluginState.FAILED
    assert reg.error_of("fake") == "boom"

    # Moving to a non-FAILED state clears the recorded error
    reg.set_state("fake", PluginState.BOOTED)
    assert reg.error_of("fake") is None


def test_registry_unregister_clears_state_and_error():
    reg = PluginRegistry()
    reg.register(_FakePlugin())
    reg.set_state("fake", PluginState.FAILED, error="boom")

    reg.unregister("fake")

    assert reg.get("fake") is None
    assert reg.state_of("fake") is None
    assert reg.error_of("fake") is None


# ── ServiceRegistry ──────────────────────────────────────────────

def test_service_registry_provide_and_resolve():
    reg = ServiceRegistry()
    reg.provide("greeter", lambda name: f"hello {name}")

    result = reg.resolve("greeter")

    assert result.is_ok()
    assert result.unwrap()("world") == "hello world"


def test_service_registry_duplicate_provide_raises():
    reg = ServiceRegistry()
    reg.provide("greeter", lambda: None)
    with pytest.raises(ValueError, match="already provided"):
        reg.provide("greeter", lambda: None)


def test_service_registry_missing_capability_is_err():
    reg = ServiceRegistry()
    result = reg.resolve("does_not_exist")
    assert result.is_err()
    assert result.unwrap_err().code == PluginErrorCode.NOT_FOUND


def test_service_registry_clear():
    reg = ServiceRegistry()
    reg.provide("greeter", lambda: None)
    reg.clear()
    assert reg.resolve("greeter").is_err()
    assert reg.names() == []


# ── PluginLoader failure isolation ───────────────────────────────

class _LoaderGoodPlugin(AbstractPlugin):
    name = "loader_test_good"

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


class _LoaderBadRegisterPlugin(AbstractPlugin):
    name = "loader_test_bad_register"

    async def register(self, app, ctx):
        raise RuntimeError("boom-register")

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


class _LoaderBadBootPlugin(AbstractPlugin):
    name = "loader_test_bad_boot"

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        raise RuntimeError("boom-boot")

    async def shutdown(self, app, ctx):
        ...


@pytest.fixture(autouse=True)
def _clean_loader_test_singletons():
    """
    PluginLoader drives the module-level `plugin_registry`/`service_registry`
    singletons (shared with the real app). Tests below inject fake plugins
    into them via a monkeypatched `_discover`, so we must scrub any leftovers
    here — otherwise a fail-fast test that aborts mid-`load_all()` would leak
    a FAILED entry that breaks unrelated tests (e.g. the `client` fixture's
    own plugin registration).
    """
    yield
    for name in list(plugin_registry.names()):
        if name.startswith("loader_test_"):
            plugin_registry.unregister(name)
    service_registry.clear()


@pytest.mark.asyncio
async def test_loader_fail_fast_aborts_and_marks_failed(monkeypatch):
    monkeypatch.setattr(settings, "plugin_load_mode", "fail_fast")
    loader = PluginLoader(FastAPI())
    monkeypatch.setattr(loader, "_discover", lambda: [_LoaderBadRegisterPlugin()])

    with pytest.raises(PluginLoadError):
        await loader.load_all()

    assert plugin_registry.state_of("loader_test_bad_register") == PluginState.FAILED
    assert "register() failed" in plugin_registry.error_of("loader_test_bad_register")


@pytest.mark.asyncio
async def test_loader_best_effort_isolates_failure_and_continues(monkeypatch):
    monkeypatch.setattr(settings, "plugin_load_mode", "best_effort")
    loader = PluginLoader(FastAPI())
    good = _LoaderGoodPlugin()
    bad = _LoaderBadBootPlugin()
    monkeypatch.setattr(loader, "_discover", lambda: [bad, good])

    await loader.load_all()  # must not raise

    assert plugin_registry.state_of("loader_test_bad_boot") == PluginState.FAILED
    assert plugin_registry.state_of("loader_test_good") == PluginState.BOOTED

    await loader.unload_all()
    assert plugin_registry.get("loader_test_good") is None
    assert plugin_registry.get("loader_test_bad_boot") is None


# ── PluginLoader accepts an injected plugin_registry ─────────────

@pytest.mark.asyncio
async def test_loader_uses_injected_plugin_registry_not_the_global_one(monkeypatch):
    """
    Proves two independently-constructed PluginLoaders don't interfere: a
    loader built with its own PluginRegistry never touches the module-level
    singleton, so two loaders (e.g. in a test, or a future multi-app setup)
    can run in complete isolation.
    """
    monkeypatch.setattr(settings, "plugin_load_mode", "best_effort")
    fake_registry = PluginRegistry()
    loader = PluginLoader(FastAPI(), plugin_registry=fake_registry)
    monkeypatch.setattr(loader, "_discover", lambda: [_LoaderGoodPlugin()])

    await loader.load_all()

    assert fake_registry.state_of("loader_test_good") == PluginState.BOOTED
    # The global singleton must be completely untouched by this loader.
    assert plugin_registry.get("loader_test_good") is None

    await loader.unload_all()
    assert fake_registry.get("loader_test_good") is None


# ── Plugin dependency ordering ───────────────────────────────────

class _DepPluginA(AbstractPlugin):
    name = "loader_test_dep_a"

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


class _DepPluginB(AbstractPlugin):
    name = "loader_test_dep_b"
    dependencies = ["loader_test_dep_a"]

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


class _DepPluginMissing(AbstractPlugin):
    name = "loader_test_dep_missing"
    dependencies = ["loader_test_dep_nonexistent"]

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


class _DepPluginCycleA(AbstractPlugin):
    name = "loader_test_cycle_a"
    dependencies = ["loader_test_cycle_b"]

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


class _DepPluginCycleB(AbstractPlugin):
    name = "loader_test_cycle_b"
    dependencies = ["loader_test_cycle_a"]

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


def test_resolve_load_order_respects_dependencies():
    loader = PluginLoader(FastAPI())
    # Declared out of dependency order — B depends on A, but A comes second.
    ordered = loader._resolve_load_order([_DepPluginB(), _DepPluginA()])
    assert [p.name for p in ordered] == ["loader_test_dep_a", "loader_test_dep_b"]


def test_resolve_load_order_missing_dependency_raises():
    loader = PluginLoader(FastAPI())
    with pytest.raises(PluginLoadError, match="not discovered"):
        loader._resolve_load_order([_DepPluginMissing()])


def test_resolve_load_order_circular_dependency_raises():
    loader = PluginLoader(FastAPI())
    with pytest.raises(PluginLoadError, match="Circular"):
        loader._resolve_load_order([_DepPluginCycleA(), _DepPluginCycleB()])


# ── enabled_plugins / dependencies error message differentiation ───────────

class _ConsumerOfDisabledDep(AbstractPlugin):
    name = "loader_test_dep_consumer"
    # A real, on-disk plugin — just not the one under test in enabled_plugins.
    dependencies = ["user_plugin"]

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


def test_missing_dependency_message_distinguishes_disabled_from_nonexistent(monkeypatch):
    # Case 1: the dependency genuinely doesn't exist under app/plugins/.
    loader = PluginLoader(FastAPI())
    with pytest.raises(PluginLoadError, match="was not discovered under app/plugins/"):
        loader._resolve_load_order([_DepPluginMissing()])

    # Case 2: the dependency exists on disk but was excluded via enabled_plugins.
    monkeypatch.setattr(settings, "enabled_plugins", ["loader_test_dep_consumer"])
    with pytest.raises(PluginLoadError, match="disabled via enabled_plugins"):
        loader._resolve_load_order([_ConsumerOfDisabledDep()])


class _DepLoaderDependent(AbstractPlugin):
    name = "loader_test_dependent_on_bad"
    dependencies = ["loader_test_bad_register"]

    async def register(self, app, ctx):
        ...

    async def boot(self, app, ctx):
        ...

    async def shutdown(self, app, ctx):
        ...


@pytest.mark.asyncio
async def test_loader_best_effort_skips_dependents_of_failed_plugin(monkeypatch):
    monkeypatch.setattr(settings, "plugin_load_mode", "best_effort")
    loader = PluginLoader(FastAPI())
    bad = _LoaderBadRegisterPlugin()
    dependent = _DepLoaderDependent()
    monkeypatch.setattr(loader, "_discover", lambda: [bad, dependent])

    await loader.load_all()  # must not raise in best_effort mode

    assert plugin_registry.state_of("loader_test_bad_register") == PluginState.FAILED
    assert plugin_registry.state_of("loader_test_dependent_on_bad") == PluginState.FAILED
    assert "dependency failed" in plugin_registry.error_of("loader_test_dependent_on_bad")


# ── Plugin enable/disable via settings ───────────────────────────

def test_discover_respects_enabled_plugins(monkeypatch):
    monkeypatch.setattr(settings, "enabled_plugins", ["user_plugin"])
    loader = PluginLoader(FastAPI())
    discovered = loader._discover()
    assert [p.name for p in discovered] == ["user_plugin"]


def test_discover_loads_everything_when_enabled_plugins_is_none(monkeypatch):
    monkeypatch.setattr(settings, "enabled_plugins", None)
    loader = PluginLoader(FastAPI())
    names = {p.name for p in loader._discover()}
    assert {"user_plugin", "product_plugin"}.issubset(names)


# ── Kernel Context ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loader_passes_kernel_context_into_lifecycle_hooks():
    seen: dict[str, object] = {}

    class _CtxCapturePlugin(AbstractPlugin):
        name = "loader_test_ctx_capture"

        async def register(self, app, ctx):
            seen["register_ctx"] = ctx

        async def boot(self, app, ctx):
            seen["boot_ctx"] = ctx

        async def shutdown(self, app, ctx):
            seen["shutdown_ctx"] = ctx

    loader = PluginLoader(FastAPI())
    plugin = _CtxCapturePlugin()

    # Drive register()/boot()/shutdown() directly through a loader-built ctx
    # instead of load_all() (which would re-discover the real filesystem
    # plugins too).
    ctx = loader._build_ctx_for(plugin.name)
    await plugin.register(loader._app, ctx)
    await plugin.boot(loader._app, ctx)
    await plugin.shutdown(loader._app, ctx)

    assert seen["register_ctx"] is ctx
    assert seen["boot_ctx"] is ctx
    assert seen["shutdown_ctx"] is ctx
    assert ctx.event_bus is not None
    assert ctx.service_registry is not None
    assert ctx.db_factory is not None
    assert ctx.settings is settings


# ── FacadeRegistry ───────────────────────────────────────────────

def test_catalog_facade_is_self_registered():
    # Importing app.facades (triggered by importing facade_registry) runs
    # app/facades/__init__.py, which imports app.facades.router — whose
    # bottom calls `facade_registry.register(CatalogFacade, router=router)`.
    # This just confirms that side effect ran.
    from app.facades.catalog_facade import CatalogFacade
    from app.facades.registry import facade_registry

    assert "catalog_facade" in facade_registry.names()
    assert facade_registry.get("catalog_facade") is CatalogFacade


def test_catalog_facade_router_is_registered_for_auto_mount():
    from app.facades.registry import facade_registry
    from app.facades.router import router as catalog_router

    assert catalog_router in facade_registry.routers()


def test_facade_registry_duplicate_raises():
    from app.facades.base import AbstractFacade
    from app.facades.registry import FacadeRegistry

    class _DupFacade(AbstractFacade):
        name = "dup_facade_for_test"

    reg = FacadeRegistry()
    reg.register(_DupFacade)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_DupFacade)


def test_facade_registry_nameless_raises():
    from app.facades.base import AbstractFacade
    from app.facades.registry import FacadeRegistry

    class _Nameless(AbstractFacade):
        name = ""

    reg = FacadeRegistry()
    with pytest.raises(ValueError, match="non-empty"):
        reg.register(_Nameless)


def test_facade_registry_router_is_optional():
    from app.facades.base import AbstractFacade
    from app.facades.registry import FacadeRegistry

    class _RouterlessFacade(AbstractFacade):
        name = "routerless_facade_for_test"

    reg = FacadeRegistry()
    reg.register(_RouterlessFacade)  # no router= kwarg
    assert reg.routers() == []
    assert "routerless_facade_for_test" in reg.names()


def test_facade_registry_routers_returns_registered_routers():
    from fastapi import APIRouter

    from app.facades.base import AbstractFacade
    from app.facades.registry import FacadeRegistry

    class _RoutedFacade(AbstractFacade):
        name = "routed_facade_for_test"

    fake_router = APIRouter()
    reg = FacadeRegistry()
    reg.register(_RoutedFacade, router=fake_router)
    assert reg.routers() == [fake_router]


# ── Plugin contract versioning ────────────────────────────────────

def test_is_api_version_compatible_matches_major_only():
    assert is_api_version_compatible("1.0", kernel_version="1.5") is True
    assert is_api_version_compatible("1.9", kernel_version="1.0") is True


def test_is_api_version_compatible_rejects_different_major():
    assert is_api_version_compatible("2.0", kernel_version="1.0") is False


def test_abstract_plugin_defaults_to_current_kernel_api_version():
    assert _FakePlugin.api_version == KERNEL_API_VERSION


def test_abstract_plugin_version_defaults_and_is_independent_of_api_version():
    # version is purely informational and defaults independently of
    # api_version — overriding one must not affect the other.
    assert _FakePlugin.version == "0.0.0"

    class _VersionedPlugin(AbstractPlugin):
        name = "versioned_for_test"
        version = "2.3.1"

        async def register(self, app, ctx): ...
        async def boot(self, app, ctx): ...
        async def shutdown(self, app, ctx): ...

    assert _VersionedPlugin.version == "2.3.1"
    assert _VersionedPlugin.api_version == KERNEL_API_VERSION

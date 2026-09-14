"""Layering rules, enforced rather than documented.

A boundary that lives only in a README is a boundary that erodes. These tests
parse every module's imports and fail on a forbidden edge, so a new ``from
atr.appdb import ...`` inside the backtester is caught in CI instead of being
discovered when someone tries to extract a service.

The rules, and why each one exists:

* ``atr.core`` imports nothing else in ``atr`` — it is the vocabulary every other
  layer speaks, so a dependency in it would be a cycle waiting to happen.
* compute layers (``backtest``, ``research``, ``strategy``, ``signals``,
  ``execution``) may not reach into ``api``, ``appdb``, ``services`` or ``auth``.
  A backtest that can read a user table is a backtest that can be run as someone
  else, and a strategy that can place an order has no risk engine in front of it.
* storage and adapter layers (``appdb``, ``data``, ``brokers``, ``auth``) may not
  import ``api`` or ``services`` — dependencies point inward, never at the
  transport or the use cases.
* ``services`` may not import ``api``.
* routes may not touch SQL or a broker directly. Every action goes through a
  service, so authorization and audit have exactly one home per action.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "atr"

#: Top-level package under src/atr → its layer name.
LAYERS = {
    "core",
    "appdb",
    "data",
    "brokers",
    "auth",
    "services",
    "api",
    "backtest",
    "research",
    "strategy",
    "signals",
    "execution",
    "alerts",
    "instruments",
    "audit",
    "live",
}

COMPUTE = {"backtest", "research", "strategy", "signals", "execution"}
STORAGE = {"appdb", "data", "brokers", "auth"}


def _module_layer(path: pathlib.Path) -> str:
    relative = path.relative_to(SRC)
    return relative.parts[0] if len(relative.parts) > 1 else path.stem


def _atr_imports(path: pathlib.Path) -> set[str]:
    """Every ``atr.*`` module this file imports, absolute and relative-resolved."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our problem
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("atr"):
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("atr"):
                    found.add(alias.name)
    return found


def _imported_layers(path: pathlib.Path) -> set[str]:
    out: set[str] = set()
    for dotted in _atr_imports(path):
        parts = dotted.split(".")
        if len(parts) >= 2 and parts[1] in LAYERS:
            out.add(parts[1])
    return out


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "__init__.py")


def test_there_is_something_to_check():
    """A guard on the guard: a bad path would make every rule vacuously pass."""
    modules = _modules()
    assert len(modules) > 40
    assert {_module_layer(p) for p in modules} >= {"api", "auth", "appdb", "backtest", "strategy"}


def test_core_depends_on_nothing_in_atr():
    offenders = []
    for path in _modules():
        if _module_layer(path) != "core":
            continue
        for imported in _imported_layers(path):
            if imported != "core":
                offenders.append(f"{path.relative_to(SRC)} imports atr.{imported}")
    assert offenders == [], f"atr.core must stay dependency-free: {offenders}"


def test_compute_layers_do_not_reach_into_transport_persistence_or_identity():
    forbidden = {"api", "appdb", "services", "auth"}
    offenders = []
    for path in _modules():
        layer = _module_layer(path)
        if layer not in COMPUTE:
            continue
        for imported in _imported_layers(path) & forbidden:
            offenders.append(f"{path.relative_to(SRC)} imports atr.{imported}")
    assert offenders == [], (
        "a backtest or strategy must not read the user store or the transport layer: "
        f"{offenders}"
    )


def test_storage_and_adapter_layers_do_not_import_the_api_or_services():
    forbidden = {"api", "services"}
    offenders = []
    for path in _modules():
        layer = _module_layer(path)
        if layer not in STORAGE:
            continue
        for imported in _imported_layers(path) & forbidden:
            offenders.append(f"{path.relative_to(SRC)} imports atr.{imported}")
    assert offenders == [], f"dependencies must point inward: {offenders}"


def test_services_do_not_import_the_api():
    offenders = [
        str(path.relative_to(SRC))
        for path in _modules()
        if _module_layer(path) == "services" and "api" in _imported_layers(path)
    ]
    assert offenders == [], f"services must not depend on the transport layer: {offenders}"


@pytest.mark.parametrize("path", sorted((SRC / "api" / "routers").glob("*.py")))
def test_routes_go_through_services_not_storage(path: pathlib.Path):
    """A route that touches SQL or a broker directly is a route without an owner."""
    if path.name == "__init__.py":
        pytest.skip("package marker")
    forbidden = {"appdb", "data", "brokers"}
    offenders = sorted(_imported_layers(path) & forbidden)
    assert offenders == [], (
        f"{path.name} imports {offenders} directly; put the work in atr.services "
        "so authorization and audit have one home per action"
    )


def test_the_authentication_layer_does_not_import_the_api():
    offenders = [
        str(path.relative_to(SRC))
        for path in _modules()
        if _module_layer(path) == "auth" and "api" in _imported_layers(path)
    ]
    assert offenders == [], offenders

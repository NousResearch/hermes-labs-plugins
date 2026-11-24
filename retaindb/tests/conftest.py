"""Test bootstrap: import the plugin directory as the package ``retaindb_plugin``.

The Hermes plugin loader imports a memory provider directory as a package so the relative layout
resolves; tests mirror that under a stable alias. Hermes core (``agent``, ``tools``, ``hermes_cli``)
must be importable — run with a hermes-agent checkout on ``PYTHONPATH`` (``HERMES_AGENT_REPO`` is
also honoured). Core no longer bundles RetainDB, so nothing here depends on ``plugins.memory.retaindb``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PACKAGE = "retaindb_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the plugin dir puts it on sys.path; keep only core's packages importable by name.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]
_CORE = os.environ.get("HERMES_AGENT_REPO")
if _CORE and _CORE not in sys.path:
    sys.path.insert(0, _CORE)
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_package()


class _RepoRootIsNotATestPackage:
    """The plugin root *is* the package (it has an ``__init__.py``), so pytest would collect it as a test
    ``Package`` and import that ``__init__.py`` standalone. Collect the root as a plain directory instead.
    Registered as a global plugin because the root's collector is created through its parent's hook proxy."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("retaindb-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "retaindb-repo-root")


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Every test gets its own HERMES_HOME and no inherited RETAINDB_* env."""
    home = tmp_path / ".hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in [n for n in os.environ if n.startswith(('RETAINDB_',))]:
        monkeypatch.delenv(name, raising=False)
    yield home

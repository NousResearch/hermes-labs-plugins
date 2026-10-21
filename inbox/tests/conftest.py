"""Import the plugin as the package ``inbox_plugin`` and give every test its own empty
HERMES_HOME (and kanban root), so nothing reads or writes the real ~/.hermes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package() -> None:
    if "inbox_plugin" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "inbox_plugin", PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["inbox_plugin"] = module
    spec.loader.exec_module(module)


_load_plugin_package()
# Test helpers (inbox_seed.py) import by name; --import-mode=importlib keeps tests/ off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The kanban board lives at the default Hermes root, which resolves to the REAL ~/.hermes
    # whenever tmp_path sits under it; pin it to the temp home explicitly.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    return home

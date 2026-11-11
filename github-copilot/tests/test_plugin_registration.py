"""The plugin as Hermes loads it: real provider discovery from a HERMES_HOME install, and the guard
that keeps it inert on a core that still serves Copilot itself."""

from __future__ import annotations

import logging
import shutil
import sys
import uuid
from pathlib import Path

import pytest

import providers
from providers.base import ProviderProfile

from conftest import PLUGIN_ROOT, load_plugin_package


def _install(home: Path) -> Path:
    dest = home / "plugins" / "github-copilot"
    shutil.copytree(PLUGIN_ROOT, dest, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    return dest


def _fresh_load(monkeypatch):
    """Execute the plugin under a new package name, recording register_provider calls."""
    calls: list[str] = []
    monkeypatch.setattr(providers, "register_provider", lambda profile: calls.append(profile.name))
    name = f"github_copilot_probe_{uuid.uuid4().hex[:8]}"
    try:
        module = load_plugin_package(name)
    finally:
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]
    return module, calls


def test_installed_plugin_resolves_both_profiles_through_discovery(hermes_home):
    _install(hermes_home)
    for name, alias in (("copilot", "github-copilot"), ("copilot-acp", "github-copilot-acp")):
        profile = providers.get_provider_profile(name)
        assert profile is not None and profile.name == name
        assert type(profile).__module__.startswith("_hermes_user_provider_")
        assert providers.provider_source(name) == "user"
        assert providers.get_provider_profile(alias) is profile
    assert providers.get_provider_profile("copilot-acp").create_client.__func__ is not (
        ProviderProfile.create_client)


@pytest.mark.any_core
def test_core_that_bundles_copilot_gets_no_registration(tmp_path, monkeypatch, caplog):
    fake_core = tmp_path / "core"
    (fake_core / "plugins" / "model-providers" / "copilot").mkdir(parents=True)
    (fake_core / "plugins" / "model-providers" / "copilot" / "__init__.py").write_text("")
    (fake_core / "providers").mkdir()
    monkeypatch.setattr(providers, "__file__", str(fake_core / "providers" / "__init__.py"))

    with caplog.at_level(logging.WARNING):
        module, calls = _fresh_load(monkeypatch)

    assert module.ACTIVE is False
    assert calls == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "still bundles GitHub Copilot" in warnings[0]


@pytest.mark.parametrize("seam", ["api_mode_for_model", "prepare_pool_entry",
                                 "refresh_pool_runtime_credentials", "credential_pool_base_urls"])
def test_core_without_the_provider_seams_gets_no_registration(monkeypatch, caplog, seam):
    monkeypatch.delattr(ProviderProfile, seam)

    with caplog.at_level(logging.WARNING):
        module, calls = _fresh_load(monkeypatch)

    assert module.ACTIVE is False and calls == []
    assert seam in caplog.text


def test_leftover_bytecode_dir_does_not_count_as_bundled(tmp_path, monkeypatch):
    # A core update removes the tracked files but can leave an ignored __pycache__ behind.
    fake_core = tmp_path / "core"
    (fake_core / "plugins" / "model-providers" / "copilot" / "__pycache__").mkdir(parents=True)
    (fake_core / "providers").mkdir()
    monkeypatch.setattr(providers, "__file__", str(fake_core / "providers" / "__init__.py"))

    module, calls = _fresh_load(monkeypatch)

    assert module.ACTIVE is True
    assert calls == ["copilot", "copilot-acp"]


def test_core_without_runtime_pair_type_stays_inactive(monkeypatch, caplog):
    import providers.base as base

    monkeypatch.delattr(base, "PoolRuntimeCredential")
    with caplog.at_level(logging.WARNING):
        module, calls = _fresh_load(monkeypatch)
    assert module.ACTIVE is False and calls == []
    assert "PoolRuntimeCredential" in caplog.text

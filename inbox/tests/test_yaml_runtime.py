"""Hermes' supported runtime no longer requires the independent PyYAML package."""
import builtins
import importlib.util
import sys
from pathlib import Path

import pytest
from inbox_plugin import config_yaml


@pytest.mark.parametrize('document', [
    'value: !!python/name:builtins.eval',
    'origin_name: first\norigin_name: second\n',
])
def test_safe_yaml_rejects_objects_and_duplicate_identity(document):
    with pytest.raises(config_yaml.YAMLError):
        config_yaml.safe_load(document)


def test_snapshot_import_and_read_work_when_bare_yaml_is_unavailable(hermes_home, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    name = 'inbox_without_pyyaml'
    spec = importlib.util.spec_from_file_location(name, root / '__init__.py', submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    original = builtins.__import__

    def reject_bare_yaml(requested, *args, **kwargs):
        if requested == 'yaml' or requested.startswith('yaml.'):
            raise ModuleNotFoundError('PyYAML deliberately unavailable in this regression')
        return original(requested, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', reject_bare_yaml)
    spec.loader.exec_module(module)
    report = importlib.import_module(name + '.report')
    data = report.build_inbox(home=hermes_home)
    assert data['attention'] == []
    assert data['sources']['cron'] == 'absent'
    assert list(hermes_home.iterdir()) == []

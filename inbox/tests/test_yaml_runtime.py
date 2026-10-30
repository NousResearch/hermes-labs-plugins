"""Inbox reads YAML through Hermes core's ``hermes_yaml``; it ships no YAML dependency of its own."""
import ast
import builtins
import importlib.util
import sys
from pathlib import Path

import hermes_yaml
import pytest
from inbox_plugin import herald

ROOT = Path(__file__).resolve().parents[1]


def test_herald_config_reads_through_core_hermes_yaml():
    assert herald.yaml is hermes_yaml


@pytest.mark.parametrize('document', [
    'hermes_herald:\n  value: !!python/name:builtins.eval\n',
    'hermes_herald:\n  origin_name: first\n  origin_name: second\n',
])
def test_core_yaml_rejects_objects_and_duplicate_identity(hermes_home, document):
    (hermes_home / 'config.yaml').write_text(document)
    with pytest.raises(hermes_yaml.YAMLError):
        herald._config(hermes_home)


def test_plugin_declares_and_imports_no_yaml_library():
    manifest = hermes_yaml.safe_load((ROOT / 'plugin.yaml').read_text())
    assert 'python_dependencies' not in manifest
    for source in ROOT.glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert name.split('.')[0] not in ('yaml', 'ruamel'), f'{source.name} imports {name}'


def test_snapshot_import_and_read_work_when_bare_yaml_is_unavailable(hermes_home, monkeypatch):
    name = 'inbox_without_pyyaml'
    spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
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

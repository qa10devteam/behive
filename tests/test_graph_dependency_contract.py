"""Dependency contract: graph feature must not rely on harvest transitives."""
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (supported by requires-python): the tomli backport from the dev extra
    import tomli as tomllib


def test_graph_dependency_has_declared_install_extra():
    project = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())['project']
    extras = project['optional-dependencies']
    for dependency in ('networkx', 'numpy', 'rapidfuzz'):
        assert any(dep.startswith(dependency + '>=') for dep in extras.get('graph', []))
    assert 'graph' in ','.join(extras['all'])

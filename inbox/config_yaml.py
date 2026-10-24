"""Public YAML dependency with an explicit safe, per-call YAML 1.1 policy."""
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


def safe_load(document):
    # Config identity must not depend on native-parser availability or shared state.
    parser = YAML(typ="safe", pure=True)
    parser.version = (1, 1)
    return parser.load(document)

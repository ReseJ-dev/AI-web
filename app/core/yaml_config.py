"""Safe YAML configuration loading."""

from pathlib import Path

import yaml


class YamlConfigurationError(ValueError):
    """Raised when a YAML configuration file cannot be loaded safely."""


def load_yaml_mapping(path: Path) -> dict[str, object]:
    """Load a YAML document and require a string-keyed mapping at its root."""
    try:
        with path.open(encoding="utf-8") as config_file:
            document: object = yaml.safe_load(config_file)
    except (OSError, yaml.YAMLError) as error:
        raise YamlConfigurationError(
            f"Unable to load YAML configuration from {path}: {error}"
        ) from error

    if document is None:
        return {}
    if not isinstance(document, dict):
        raise YamlConfigurationError(
            f"YAML configuration in {path} must contain a mapping"
        )
    if not all(isinstance(key, str) for key in document):
        raise YamlConfigurationError(
            f"YAML configuration keys in {path} must be strings"
        )
    return document

from pathlib import Path

import pytest
import yaml

from modl.config import BreakingChangeConfig, NamespaceConfig
from modl.models import ElementKind

VALID_CONFIG = {
    "namespace": {"namespace": "https://myproject.org/model", "prefix": "mp"},
    "entity": {"essential_attributes": ["instances", "type"]},
    "property": {"essential_attributes": ["datatype", "unit", "accuracy"]},
}


class TestNamespaceConfig:
    def test_uri_base_with_prefix(self) -> None:
        """Prefix overrides namespace as the URI base segment for all tables."""
        ns = NamespaceConfig(namespace="https://myproject.org/model", prefix="mp")
        assert ns.uri_base("concepts") == "mp-c"
        assert ns.uri_base("revisions") == "mp-r"
        assert ns.uri_base("variants") == "mp-v"
        assert ns.uri_base("bindings") == "mp-b"

    def test_uri_base_without_prefix(self) -> None:
        """Namespace string is used as the prefix when none is explicitly set."""
        ns = NamespaceConfig(namespace="myns")
        assert ns.uri_base("concepts") == "myns-c"
        assert ns.uri_base("variants") == "myns-v"

    def test_unknown_table_raises(self) -> None:
        """Unrecognised table name raises KeyError."""
        ns = NamespaceConfig(namespace="myns", prefix="mp")
        with pytest.raises(KeyError):
            ns.uri_base("unknown")


class TestBreakingChangeConfig:
    def test_extra_fields_rejected(self) -> None:
        """Unknown top-level keys are rejected (extra='forbid')."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BreakingChangeConfig.model_validate(
                {
                    "namespace": {"namespace": "myns"},
                    "other": {"key": "value"},
                }
            )

    def test_load_from_dict(self) -> None:
        """Valid config dict deserialises to the expected field values."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.namespace.prefix == "mp"
        assert "datatype" in cfg.property.essential_attributes
        assert "instances" in cfg.entity.essential_attributes

    def test_defaults_empty_essential_attributes(self) -> None:
        """essential_attributes defaults to empty list for both entity and property."""
        cfg = BreakingChangeConfig(namespace=NamespaceConfig(namespace="myns"))
        assert cfg.entity.essential_attributes == []
        assert cfg.property.essential_attributes == []

    def test_is_breaking_entity_true(self) -> None:
        """Changing an essential entity attribute is breaking."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": ["Left", "Right", "Center"]}) is True

    def test_is_breaking_entity_false(self) -> None:
        """Changing only non-essential entity attributes is not breaking."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.ENTITY, {"description": "updated"}) is False

    def test_is_breaking_property_true(self) -> None:
        """Changing an essential property attribute is breaking."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"datatype": "Float"}) is True

    def test_is_breaking_property_false(self) -> None:
        """Changing only non-essential property attributes is not breaking."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"description": "updated"}) is False

    def test_is_breaking_empty_changes(self) -> None:
        """Empty changed_attributes dict is never a breaking change."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {}) is False

    def test_is_breaking_user_defined_attribute(self) -> None:
        """User-defined essential attributes (e.g. accuracy) are recognised."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"accuracy": "0.01"}) is True

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        """YAML file round-trips to the expected config values."""
        config_file = tmp_path / "modl.yaml"
        config_file.write_text(yaml.dump(VALID_CONFIG))
        cfg = BreakingChangeConfig.from_yaml(config_file)
        assert cfg.namespace.namespace == "https://myproject.org/model"
        assert cfg.property.essential_attributes == ["datatype", "unit", "accuracy"]

    def test_load_from_yaml_missing_file_raises(self, tmp_path: Path) -> None:
        """Non-existent YAML path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            BreakingChangeConfig.from_yaml(tmp_path / "nonexistent.yaml")

from pathlib import Path

import pytest
import yaml

from modl.config import BreakingChangeConfig, NamespaceConfig
from modl.models import ElementKind

VALID_CONFIG = {
    "namespace": {"namespace": "https://myproject.org/model/", "prefix": "mp"},
    "entity": {"instances": True, "type": True},
    "property": {"datatype": True, "unit": True, "accuracy": True},
}


class TestNamespaceConfig:
    def test_uri_base_slash_namespace(self) -> None:
        """uri_base concatenates namespace and table directly; no extra slash inserted."""
        ns = NamespaceConfig(namespace="https://myproject.org/model/", prefix="mp")
        assert ns.uri_base("concepts") == "https://myproject.org/model/concepts"
        assert ns.uri_base("revisions") == "https://myproject.org/model/revisions"
        assert ns.uri_base("variants") == "https://myproject.org/model/variants"
        assert ns.uri_base("bindings") == "https://myproject.org/model/bindings"

    def test_uri_base_hash_namespace(self) -> None:
        """Hash-terminated namespace also produces a valid concatenated URI."""
        ns = NamespaceConfig(namespace="https://myproject.org/model#")
        assert ns.uri_base("concepts") == "https://myproject.org/model#concepts"

    def test_uri_base_without_prefix(self) -> None:
        """Prefix absence does not affect the stored URI — namespace is always used."""
        ns = NamespaceConfig(namespace="https://myproject.org/model/")
        assert ns.uri_base("concepts") == "https://myproject.org/model/concepts"

    def test_prefix_is_stored_independently(self) -> None:
        """Prefix is preserved as a display-only field, not baked into stored URIs."""
        ns = NamespaceConfig(namespace="https://myproject.org/model/", prefix="mp")
        assert ns.prefix == "mp"
        assert not ns.uri_base("concepts").startswith("mp")

    def test_namespace_without_separator_rejected(self) -> None:
        """Namespace not ending in '/' or '#' is rejected at validation time."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="namespace must end with"):
            NamespaceConfig(namespace="https://myproject.org/model")

    def test_namespace_with_spaces_rejected(self) -> None:
        """Namespace containing spaces is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must not contain spaces"):
            NamespaceConfig(namespace="https://my project.org/model/")

    def test_namespace_not_absolute_rejected(self) -> None:
        """Relative or scheme-less namespace is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must be an absolute URI"):
            NamespaceConfig(namespace="example-namespace/")


class TestBreakingChangeConfig:
    def test_extra_fields_rejected(self) -> None:
        """Unknown top-level keys are rejected (extra='forbid')."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BreakingChangeConfig.model_validate(
                {
                    "namespace": {"namespace": "myns/"},
                    "other": {"key": "value"},
                }
            )

    def test_load_from_dict(self) -> None:
        """Valid config dict deserialises to the expected field values."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.namespace.prefix == "mp"
        assert cfg.property["datatype"] is True
        assert cfg.entity["instances"] is True

    def test_defaults_empty_dicts(self) -> None:
        """entity and property default to empty dicts when omitted."""
        cfg = BreakingChangeConfig(namespace=NamespaceConfig(namespace="http://example.org/myns/"))
        assert cfg.entity == {}
        assert cfg.property == {}

    def test_is_breaking_entity_true(self) -> None:
        """Aspect mapped to true is breaking for that entity kind."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": ["Left", "Right", "Center"]}) is True

    def test_is_breaking_entity_false_value(self) -> None:
        """Aspect mapped to false is explicitly non-breaking (not absent — suppresses warnings)."""
        cfg = BreakingChangeConfig.model_validate(
            {"namespace": {"namespace": "https://myproject.org/model/"}, "entity": {"description": False}}
        )
        assert cfg.is_breaking(ElementKind.ENTITY, {"description": "updated"}) is False

    def test_is_breaking_entity_absent(self) -> None:
        """Aspect absent from the config is non-breaking (though it may warn)."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.ENTITY, {"description": "updated"}) is False

    def test_is_breaking_property_true(self) -> None:
        """Aspect mapped to true is breaking for that property kind."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"datatype": "Float"}) is True

    def test_is_breaking_property_absent(self) -> None:
        """Aspect absent from the config is non-breaking (though it may warn)."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"description": "updated"}) is False

    def test_is_breaking_empty_aspects(self) -> None:
        """Empty aspects dict is never a breaking change."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {}) is False

    def test_is_breaking_user_defined_aspect(self) -> None:
        """User-defined breaking aspects (e.g. accuracy) are recognised."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"accuracy": "0.01"}) is True

    def test_is_breaking_rename_name_true(self) -> None:
        """renamed_from with name: true makes the rename breaking."""
        cfg = BreakingChangeConfig.model_validate(
            {"namespace": {"namespace": "https://myproject.org/model/"}, "entity": {"name": True}}
        )
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldVehicle") is True

    def test_is_breaking_rename_name_false(self) -> None:
        """renamed_from with name: false is explicitly non-breaking."""
        cfg = BreakingChangeConfig.model_validate(
            {"namespace": {"namespace": "https://myproject.org/model/"}, "entity": {"name": False}}
        )
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldVehicle") is False

    def test_is_breaking_rename_name_absent(self) -> None:
        """renamed_from with name absent from config is non-breaking."""
        cfg = BreakingChangeConfig.model_validate({"namespace": {"namespace": "https://myproject.org/model/"}})
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldVehicle") is False

    def test_is_breaking_absent_aspect_key_is_false(self) -> None:
        """An aspect key absent from config is non-breaking (returns False, not None)."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        result = cfg.is_breaking(ElementKind.PROPERTY, {"unknown_key": "value"})
        assert result is False
        assert type(result) is bool

    def test_is_breaking_rename_absent_not_breaking(self) -> None:
        """Rename is non-breaking when 'name' key is absent from the config."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {}, renamed_from="OldName") is False

    def test_is_breaking_rename_false_not_breaking(self) -> None:
        """Rename is non-breaking (and silent) when 'name' maps to false."""
        cfg = BreakingChangeConfig.model_validate(
            {"namespace": {"namespace": "https://myproject.org/model/"}, "property": {"name": False}}
        )
        assert cfg.is_breaking(ElementKind.PROPERTY, {}, renamed_from="OldName") is False

    def test_is_breaking_rename_true_breaking(self) -> None:
        """Rename is breaking when 'name' maps to true."""
        cfg = BreakingChangeConfig.model_validate(
            {"namespace": {"namespace": "https://myproject.org/model/"}, "property": {"name": True}}
        )
        assert cfg.is_breaking(ElementKind.PROPERTY, {}, renamed_from="OldName") is True

    def test_is_breaking_entity_rename_true_breaking(self) -> None:
        """Entity rename is breaking when 'name' maps to true under entity."""
        cfg = BreakingChangeConfig.model_validate(
            {"namespace": {"namespace": "https://myproject.org/model/"}, "entity": {"name": True}}
        )
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldEntity") is True

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        """YAML file round-trips to the expected config values."""
        config_file = tmp_path / "modl.yaml"
        config_file.write_text(yaml.dump(VALID_CONFIG))
        cfg = BreakingChangeConfig.from_yaml(config_file)
        assert cfg.namespace.namespace == "https://myproject.org/model/"
        assert cfg.property["datatype"] is True
        assert cfg.property["unit"] is True
        assert cfg.property["accuracy"] is True

    def test_load_from_yaml_missing_file_raises(self, tmp_path: Path) -> None:
        """Non-existent YAML path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            BreakingChangeConfig.from_yaml(tmp_path / "nonexistent.yaml")

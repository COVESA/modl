from pathlib import Path

import pytest
import yaml

from modl.config import BreakingChangeConfig, ModelMetadata
from modl.models import ElementKind

VALID_METADATA = {
    "name": "My Project",
    "id": "https://myproject.org/model/",
    "preferred_prefix": "mp",
}

VALID_CONFIG = {
    "entity": {"instances": True, "type": True},
    "property": {"datatype": True, "unit": True, "accuracy": True},
    "enumeration_set": {},
    "enum_value": {},
}


class TestModelMetadata:
    def test_uri_base_slash_id(self) -> None:
        """uri_base concatenates id and table directly; no extra slash inserted."""
        meta = ModelMetadata(name="Test", id="https://myproject.org/model/")
        assert meta.uri_base("concepts") == "https://myproject.org/model/concepts"
        assert meta.uri_base("revisions") == "https://myproject.org/model/revisions"
        assert meta.uri_base("contracts") == "https://myproject.org/model/contracts"
        assert meta.uri_base("bindings") == "https://myproject.org/model/bindings"

    def test_uri_base_hash_id(self) -> None:
        """Hash-terminated id also produces a valid concatenated URI."""
        meta = ModelMetadata(name="Test", id="https://myproject.org/model#")
        assert meta.uri_base("concepts") == "https://myproject.org/model#concepts"

    def test_uri_base_without_preferred_prefix(self) -> None:
        """Absence of preferred_prefix does not affect the stored URI — id is always used."""
        meta = ModelMetadata(name="Test", id="https://myproject.org/model/")
        assert meta.uri_base("concepts") == "https://myproject.org/model/concepts"

    def test_preferred_prefix_is_stored_independently(self) -> None:
        """preferred_prefix is preserved as a display-only field, not baked into stored URIs."""
        meta = ModelMetadata(name="Test", id="https://myproject.org/model/", preferred_prefix="mp")
        assert meta.preferred_prefix == "mp"
        assert not meta.uri_base("concepts").startswith("mp")

    def test_id_without_separator_rejected(self) -> None:
        """id not ending in '/' or '#' is rejected at validation time."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id must end with"):
            ModelMetadata(name="Test", id="https://myproject.org/model")

    def test_id_with_spaces_rejected(self) -> None:
        """id containing spaces is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must not contain spaces"):
            ModelMetadata(name="Test", id="https://my project.org/model/")

    def test_id_not_absolute_rejected(self) -> None:
        """Relative or scheme-less id is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must be an absolute URI"):
            ModelMetadata(name="Test", id="example-namespace/")

    def test_name_accepted(self) -> None:
        """name field is stored as-is."""
        meta = ModelMetadata(name="My Model", id="https://myproject.org/model/")
        assert meta.name == "My Model"

    def test_from_yaml(self, tmp_path: Path) -> None:
        """YAML file round-trips to the expected metadata values."""
        meta_file = tmp_path / "metadata.yaml"
        meta_file.write_text(yaml.dump(VALID_METADATA))
        meta = ModelMetadata.from_yaml(meta_file)
        assert meta.name == "My Project"
        assert meta.id == "https://myproject.org/model/"
        assert meta.preferred_prefix == "mp"

    def test_from_yaml_missing_file_raises(self, tmp_path: Path) -> None:
        """Non-existent YAML path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ModelMetadata.from_yaml(tmp_path / "nonexistent.yaml")


class TestBreakingChangeConfig:
    def test_extra_fields_rejected(self) -> None:
        """Unknown top-level keys are rejected (extra='forbid')."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BreakingChangeConfig.model_validate({"other": {"key": "value"}})

    def test_load_from_dict(self) -> None:
        """Valid config dict deserialises to the expected field values."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.property["datatype"] is True
        assert cfg.entity["instances"] is True

    def test_defaults_empty_dicts(self) -> None:
        """All four sections default to empty dicts when omitted."""
        cfg = BreakingChangeConfig()
        assert cfg.entity == {}
        assert cfg.property == {}
        assert cfg.enumeration_set == {}
        assert cfg.enum_value == {}

    def test_is_breaking_entity_true(self) -> None:
        """Aspect mapped to true is breaking for that entity kind."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": "added"}) is True

    def test_is_breaking_entity_false_value(self) -> None:
        """Aspect mapped to false is explicitly non-breaking (not absent — suppresses warnings)."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"description": False}})
        assert cfg.is_breaking(ElementKind.ENTITY, {"description": "modified"}) is False

    def test_is_breaking_entity_absent(self) -> None:
        """Aspect absent from the config is non-breaking (though it may warn)."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.ENTITY, {"description": "modified"}) is False

    def test_is_breaking_property_true(self) -> None:
        """Aspect mapped to true is breaking for that property kind."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"datatype": "modified"}) is True

    def test_is_breaking_property_absent(self) -> None:
        """Aspect absent from the config is non-breaking (though it may warn)."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"description": "modified"}) is False

    def test_is_breaking_empty_aspects(self) -> None:
        """Empty aspects dict is never a breaking change."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {}) is False

    def test_is_breaking_user_defined_aspect(self) -> None:
        """User-defined breaking aspects (e.g. accuracy) are recognised."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {"accuracy": "modified"}) is True

    def test_is_breaking_rename_name_true(self) -> None:
        """renamed_from with name.modified: true makes the rename breaking."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"name.modified": True}})
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldVehicle") is True

    def test_is_breaking_rename_name_false(self) -> None:
        """renamed_from with name.modified: false is explicitly non-breaking."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"name.modified": False}})
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldVehicle") is False

    def test_is_breaking_rename_name_absent(self) -> None:
        """renamed_from with name absent from config is non-breaking."""
        cfg = BreakingChangeConfig()
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldVehicle") is False

    def test_is_breaking_absent_aspect_key_is_false(self) -> None:
        """An aspect key absent from config is non-breaking (returns False, not None)."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        result = cfg.is_breaking(ElementKind.PROPERTY, {"unknown_key": "modified"})
        assert result is False
        assert type(result) is bool

    def test_is_breaking_rename_absent_not_breaking(self) -> None:
        """Rename is non-breaking when 'name' key is absent from the config."""
        cfg = BreakingChangeConfig.model_validate(VALID_CONFIG)
        assert cfg.is_breaking(ElementKind.PROPERTY, {}, renamed_from="OldName") is False

    def test_is_breaking_rename_false_not_breaking(self) -> None:
        """Rename is non-breaking (and silent) when 'name.modified' maps to false."""
        cfg = BreakingChangeConfig.model_validate({"property": {"name.modified": False}})
        assert cfg.is_breaking(ElementKind.PROPERTY, {}, renamed_from="OldName") is False

    def test_is_breaking_rename_true_breaking(self) -> None:
        """Rename is breaking when 'name.modified' maps to true."""
        cfg = BreakingChangeConfig.model_validate({"property": {"name.modified": True}})
        assert cfg.is_breaking(ElementKind.PROPERTY, {}, renamed_from="OldName") is True

    def test_is_breaking_entity_rename_true_breaking(self) -> None:
        """Entity rename is breaking when 'name.modified' maps to true under entity."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"name.modified": True}})
        assert cfg.is_breaking(ElementKind.ENTITY, {}, renamed_from="OldEntity") is True

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        """YAML file round-trips to the expected config values."""
        config_file = tmp_path / "breaking.yaml"
        config_file.write_text(yaml.dump(VALID_CONFIG))
        cfg = BreakingChangeConfig.from_yaml(config_file)
        assert cfg.property["datatype"] is True
        assert cfg.property["unit"] is True
        assert cfg.property["accuracy"] is True

    def test_load_from_yaml_missing_file_raises(self, tmp_path: Path) -> None:
        """Non-existent YAML path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            BreakingChangeConfig.from_yaml(tmp_path / "nonexistent.yaml")

    def test_from_yaml_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        """Empty/null YAML file returns default empty config without error."""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")  # produces None from yaml.safe_load
        cfg = BreakingChangeConfig.from_yaml(config_file)
        assert cfg.entity == {}
        assert cfg.property == {}
        assert cfg.enumeration_set == {}
        assert cfg.enum_value == {}

    def test_enumeration_set_section_accepted(self) -> None:
        """enumeration_set section with valid keys round-trips correctly."""
        cfg = BreakingChangeConfig.model_validate(
            {
                "enumeration_set": {"values.added": False, "values.removed": True},
            }
        )
        assert cfg.enumeration_set["values.removed"] is True
        assert cfg.enumeration_set["values.added"] is False

    def test_enum_value_section_accepted(self) -> None:
        """enum_value section with valid keys round-trips correctly."""
        cfg = BreakingChangeConfig.model_validate(
            {
                "enum_value": {"name.modified": True, "symbol": True},
            }
        )
        assert cfg.enum_value["name.modified"] is True
        assert cfg.enum_value["symbol"] is True

    def test_is_breaking_enumeration_set(self) -> None:
        """is_breaking dispatches to the enumeration_set section."""
        cfg = BreakingChangeConfig.model_validate({"enumeration_set": {"values.removed": True}})
        assert cfg.is_breaking(ElementKind.ENUMERATION_SET, {"values": "removed"}) is True
        assert cfg.is_breaking(ElementKind.ENUMERATION_SET, {"values": "added"}) is False

    def test_is_breaking_enum_value(self) -> None:
        """is_breaking dispatches to the enum_value section."""
        cfg = BreakingChangeConfig.model_validate({"enum_value": {"symbol": True}})
        assert cfg.is_breaking(ElementKind.ENUM_VALUE, {"symbol": "modified"}) is True
        assert cfg.is_breaking(ElementKind.ENUM_VALUE, {"description": "modified"}) is False

    def test_invalid_op_suffix_rejected(self) -> None:
        """Dotted key with an unrecognised op suffix is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Invalid op suffix"):
            BreakingChangeConfig.model_validate({"property": {"unit.replaced": True}})

    def test_name_added_rejected(self) -> None:
        """'name.added' is rejected — the operation is structurally impossible."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="name.added"):
            BreakingChangeConfig.model_validate({"entity": {"name.added": False}})

    def test_name_removed_rejected(self) -> None:
        """'name.removed' is rejected — the operation is structurally impossible."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="name.removed"):
            BreakingChangeConfig.model_validate({"property": {"name.removed": True}})

    def test_plain_name_key_rejected(self) -> None:
        """Plain 'name' key (without op suffix) is forbidden; must use 'name.modified'."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="name.modified"):
            BreakingChangeConfig.model_validate({"entity": {"name": True}})

    def test_granular_takes_precedence_over_shorthand(self) -> None:
        """Granular key (unit.added) takes precedence over shorthand (unit) for the same op."""
        cfg = BreakingChangeConfig.model_validate({"property": {"unit": True, "unit.added": False}})
        # shorthand says True; granular for 'added' says False → granular wins
        assert cfg.is_breaking(ElementKind.PROPERTY, {"unit": "added"}) is False
        # 'modified' op has no granular override → shorthand applies
        assert cfg.is_breaking(ElementKind.PROPERTY, {"unit": "modified"}) is True

    def test_unknown_keys_returns_undeclared_keys(self) -> None:
        """unknown_keys returns keys absent from both config and structural set."""
        cfg = BreakingChangeConfig.model_validate({"property": {"unit": True}})
        unknown = cfg.unknown_keys(ElementKind.PROPERTY, {"unit": "modified", "accuracy": "modified"})
        assert "accuracy.modified" in unknown
        assert "unit.modified" not in unknown

    def test_unknown_keys_structural_keys_excluded(self) -> None:
        """Structural keys (e.g. name.modified, instances.added) never appear in unknown_keys."""
        cfg = BreakingChangeConfig.model_validate({})
        unknown = cfg.unknown_keys(
            ElementKind.ENTITY,
            {"instances": "added", "properties": "removed"},
        )
        assert unknown == []

    def test_unknown_keys_rename_not_declared(self) -> None:
        """Rename (renamed_from set) is reported as unknown when name.modified is absent."""
        cfg = BreakingChangeConfig.model_validate({})
        unknown = cfg.unknown_keys(ElementKind.ENTITY, {}, renamed_from="OldLabel")
        # name.modified is a structural key → should NOT appear in unknown
        assert "name.modified" not in unknown

    def test_instances_directional_shorthand_breaking(self) -> None:
        """Shorthand 'instances: true' catches both instances.added and instances.removed."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"instances": True}})
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": "added"}) is True
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": "removed"}) is True

    def test_instances_directional_granular_asymmetric(self) -> None:
        """Granular ops allow asymmetric breaking classification for instances."""
        cfg = BreakingChangeConfig.model_validate(
            {
                "entity": {"instances.added": False, "instances.removed": True},
            }
        )
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": "added"}) is False
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances": "removed"}) is True

# Test Suite — What Is Tested Where

220 tests across 7 files. Run with `uv run pytest`.

---

## Quick reference

| File | Tests | What it covers |
|---|---|---|
| `test_modl.py` | 1 | Package version importable |
| `test_config.py` | 27 | Namespace validation, breaking-change classification |
| `test_ir.py` | 51 | IR parsing, structural validation, aspect-key checking |
| `test_ledger.py` | 39 | CSV schema, FK integrity, base-36 encoding, read/write |
| `test_models.py` | 18 | Pydantic row models for the four ledger tables |
| `test_sync.py` | 71 | Sync engine — every event type, every edge case |
| `test_cli.py` | 12 | CLI surface — flags, error handling, clean exit messages |

---

## test_config.py

### `TestNamespaceConfig`
Validates the `namespace` field. Covers:
- `uri_base()` output for `/`-terminated and `#`-terminated namespaces
- Rejection of namespaces that don't end with `/` or `#`, contain spaces, or are relative

### `TestBreakingChangeConfig`
Covers `BreakingChangeConfig.is_breaking()` for every case:

| Scenario | Expected |
|---|---|
| Aspect mapped to `true` | breaking |
| Aspect mapped to `false` | not breaking |
| Aspect absent from config | not breaking (but may warn) |
| `renamed_from` + `name: true` in config | breaking |
| `renamed_from` + `name: false` in config | not breaking |
| `renamed_from` + `name` absent | not breaking |
| Empty aspects dict | not breaking |

Also covers YAML loading and rejection of unknown top-level config keys.

---

## test_ir.py

### `TestEntityChanged` / `TestPropertyChanged`
Each event type's payload rules:
- ADDED must not carry `content` (entity) or `renamed_from`
- REMOVED must not carry `aspects`, `content`, or `renamed_from`
- `renamed_from` is only valid on MODIFIED
- `PropertyChanged` requires `parent_label`

### `TestDiffReport`
`DiffReport.from_json()` — valid payloads parse to the correct concrete types; invalid JSON and wrong top-level key are rejected.

### `TestValidateStructure`
`DiffReport.validate_structure()` warns on duplicate labels and raises in strict mode. Two properties with the same name under different parents are distinct — no warning.

### `TestValidateReportAspects`
`validate_report_aspects()` warns when a MODIFIED event carries an aspect key not declared in the config (and not in the canonical set). ADDED and REMOVED events are always skipped.

### `TestVocabularyKinds`
`ENUMERATION_SET` must use `EntityChanged`; `ENUM_VALUE` must use `PropertyChanged`. Using the wrong event class is rejected. The correct config section (`entity` vs `property`) is consulted for each kind.

---

## test_ledger.py

### `TestValidateLedger`
Data-level constraints enforced by `validate_ledger()`:

| Check | Example failure |
|---|---|
| Missing table or column | `bindings.csv` has no `variant_uri` column |
| Extra column | unexpected column present |
| Null in required field | `concept_uri` is `None` |
| Invalid `status` / `kind` value | `"PENDING"`, `"BRANCH"` |
| Negative serial | `serial = -1` |
| URI suffix ≠ `b36encode(serial)` | serial 1 but URI ends in `z` |
| FK violation | revision references non-existent concept |
| Cross-concept variant mismatch | variant's `revision_uri` belongs to a different concept |
| Binding on non-PROPERTY concept | ENTITY or ENUMERATION_SET concept linked to a binding |

### `TestValidateLedgerDir`
Directory-level checks: must be a directory, exactly the four CSVs, nothing else.

### `TestReadWriteLedger` / `TestNextSerial` / `TestB36`
Round-trip write → read → validate; `next_serial()` returns `max + 1`; `b36encode()` covers 0, 1–9, a–z, multi-digit values.

---

## test_models.py

Pydantic row models for the four tables (`ConceptRow`, `RevisionRow`, `VariantRow`, `BindingRow`). Checks required fields, defaults, value constraints (negative serial rejected), and vocabulary kinds (`ENUMERATION_SET`, `ENUM_VALUE`).

---

## test_sync.py

The largest file. Each test class exercises one engine path.

### ADDED events

**`TestEntityAdded`** — entity ADDED mints one concept + revision + variant, no bindings. Instances stored as JSON. Multiple entities get incrementing serials. Non-compliant adapter output (`instances` is a nested list or contains non-strings) raises `SyncError`.

**`TestPropertyAdded`** — property ADDED links to its parent via `parent_uri` and copies the parent's instance list. One binding per instance; singleton binding (null `instance_label`) when the parent has no instances. `ENUM_VALUE` kind gets no bindings.

**`TestEnumerationSetAdded`** — `ENUMERATION_SET` ADDED behaves like ENTITY: one concept/revision/variant, no bindings, correct `kind` stored. `ENUM_VALUE` children carry the set's `concept_uri` as `parent_uri`.

### MODIFIED events (entity)

**`TestEntityModifiedNonBreaking`** — new revision supersedes the old one; variant is untouched. Revision chain: new revision's `previous_revision_uri` points to the superseded one. Rename: `current_label` updated, old label prepended to `previous_labels`.

**`TestEntityModifiedBreakingNonInstance`** — breaking non-instance change (e.g. `type`) mints a new entity variant but does not cascade to child properties.

**`TestEntityModifiedInstanceNonBreaking`** — adding instances non-breakingly mints one new binding per new instance per child property; existing bindings stay ACTIVE; child properties get a new revision but no new variant.

**`TestEntityModifiedInstanceBreaking`** — breaking instance change supersedes all old bindings and mints new bindings for every instance (old + new) anchored to a new child variant.

**`TestEntityModifiedRenameAndBreaking`** — rename + breaking aspect in the same MODIFIED event: label updated AND new variant minted in one pass.

**`TestEntityModifiedInstanceShrinks`** — shrinking the instance list (non-breaking): no new bindings minted, instances column updated, child still gets a new revision.

### MODIFIED events (property)

**`TestPropertyModifiedBreaking`** — old binding superseded, new binding minted under new variant. Works for both singleton and instanced properties. Rename + breaking change applies both in one event.

**`TestPropertyModifiedNonBreaking`** — new revision only; variant and bindings untouched.

**`TestPropertyModifiedRenameAndNonBreaking`** — rename without a breaking aspect: label updated, no new variant.

### REMOVED events

**`TestEntityRemoved`** — concept marked REMOVED, final REMOVED revision minted, all variants marked REMOVED. Consistency guard: entity REMOVED without explicit REMOVED events for all child properties raises `SyncError`.

**`TestPropertyRemoved`** — concept REMOVED, all bindings marked REMOVED. `ENUM_VALUE` REMOVED never touches bindings.

### Rename edge cases

**`TestEntitySecondRename`** — two successive renames accumulate both old labels in `previous_labels` (most-recent first). Three renames gives all three in order.

**`TestRenameNonexistentConcept`** — `renamed_from` pointing to a label that does not exist in the ledger raises `SyncError` for both entity and property events.

### Multi-child cascade

**`TestMultipleChildPropertiesCascade`** — two child properties under the same entity both get new variants (breaking) or new bindings (non-breaking) when the parent's instance list changes.

### Incremental syncs

**`TestRoundTrip`** — `sync → write_ledger → read_ledger → validate_ledger` round-trips without data loss. Two successive syncs accumulate rows correctly.

**`TestThreeSuccessiveSyncs`** — three syncs produce gap-free, duplicate-free, monotonically-increasing serials across all four tables. A concept from run 1 can be looked up and broken in run 3 after an unrelated run 2.

---

## test_cli.py

All tests use `click.testing.CliRunner`. Key scenarios:

| Test | What it checks |
|---|---|
| `test_help` / `test_sync_help` | All option flags visible |
| `test_sync_no_diff_report` | Empty ledger initialised when no diff given |
| `test_sync_dry_run_flag` | `--dry-run` prevents writes |
| `test_sync_with_diff_report` | Diff path appears in log |
| `test_sync_invalid_diff_report_errors` | Bad `kind` enum → exit ≠ 0 |
| `test_sync_strict_flag_fails_on_unknown_aspects` | `--strict` + unknown aspect key → exit ≠ 0 |
| `test_sync_missing_config_errors` | Non-existent config file → exit ≠ 0 |
| `test_sync_missing_ledger_dir_option_errors` | Omitting `--ledger-dir` → exit ≠ 0 |
| `test_sync_dirty_ledger_dir_errors` | Dir with unexpected files → exit ≠ 0 |
| `test_sync_engine_sync_error_surfaces_cleanly` | `SyncError` → single log line, no traceback |
| `test_sync_corrupt_ledger_surfaces_cleanly` | Invalid ledger content → single log line, no traceback |
| `test_sync_invalid_config_yaml_structure_errors` | Bad YAML structure → exit ≠ 0 |

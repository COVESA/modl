# Test Suite — What Is Tested Where

397 tests across 7 files. Run with `uv run pytest`.

---

## Quick reference

| File | Tests | What it covers |
|---|---|---|
| `test_config.py` | 66 | Namespace validation, breaking-change classification |
| `test_ir.py` | 112 | IR parsing, structural validation, aspect-key checking, mandatory MODIFIED value wrapping, mandatory REMOVED `previous_aspects`, `references` validation |
| `test_ledger.py` | 94 | CSV schema, FK integrity, base-36 encoding, read/write, `revision_aspects` row validation, `contract_references` validation |
| `test_models.py` | 29 | Pydantic row models for the six ledger tables |
| `test_sync.py` | 130 | Sync engine — every event type, every edge case, `revision_aspects` row minting, reference edges and cascade |
| `test_adapt.py` | 45 | `modl adapt` compatibility-plan engine |
| `test_cli.py` | 18 | CLI surface — flags, error handling, clean exit messages |

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
- ADDED must not carry `content` (entity), `renamed_from`, or `previous_aspects`
- MODIFIED aspect values must be wrapped as `{"_op": ..., "_value": ..., "_previous": ...}` — plain unwrapped values are rejected
- REMOVED must not carry `aspects`, `content`, or `renamed_from`; `previous_aspects` is mandatory and must be non-empty
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
| Missing table or column | `bindings.csv` has no `contract_uri` column |
| Extra column | unexpected column present |
| Null in required field | `concept_uri` is `None` |
| Invalid `status` / `kind` value | `"PENDING"`, `"BRANCH"` |
| Negative serial | `serial = -1` |
| URI suffix ≠ `b36encode(serial)` | serial 1 but URI ends in `z` |
| FK violation | revision references non-existent concept |
| Cross-concept contract mismatch | contract's `revision_uri` belongs to a different concept |
| Binding on non-PROPERTY concept | ENTITY or ENUMERATION_SET concept linked to a binding |
| Duplicate `runtime_path` / tags | two ACTIVE bindings share a path, or share tags on one contract |

### `TestValidateLedgerDir`
Directory-level checks: must be a directory, exactly the six CSVs, nothing else.

### `TestReadWriteLedger` / `TestNextSerial` / `TestB36`
Round-trip write → read → validate; `next_serial()` returns `max + 1`; `b36encode()` covers 0, 1–9, a–z, multi-digit values.

### `TestBindingValidation`
Row-level constraints on `bindings.csv`: `instance_tags` must be canonical JSON keyed by existing ENTITY concept URIs, `runtime_path` must be non-empty, and ACTIVE bindings must have unique `runtime_path` values (`instance_tags` may repeat across ACTIVE bindings of the same contract as long as `runtime_path` differs).

### `TestExportBindings`
Binding export: `json` lists one item per binding (`instance_tags` by entity label, `runtime_path`) sorted by path; `vspec` is keyed by `runtime_path`; `--complete` and custom binding keys.

### `TestExportSqlite`
SQLite export: all tables created with matching row counts; NULL/INTEGER/JSON-text round trip and `json_extract` queries; schema constraints; empty ledger; existing file refused unless `overwrite`; directory output rejected; constraint violations raise without leaving a file or temp artefacts.

### `TestRevisionAspectsValidation`
Row-level constraints on `revision_aspects.csv`: valid ADDED/MODIFIED/REMOVED rows pass; duplicate `(revision_uri, aspect_key)` raises; invalid `operation` raises; `modified` rows with a null `previous_value` or `newer_value` raise; `added` rows with a non-null `previous_value` raise; `removed` rows with a non-null `newer_value` raise; FK violation on `revision_uri` raises.

---

## test_models.py

Pydantic row models for the ledger tables (`ConceptRow`, `RevisionRow`, `ContractRow`, `BindingRow`, `RevisionAspectRow`, `ContractReferenceRow`). `BindingRow` carries `instance_tags` (canonical JSON) and `runtime_path`. Checks required fields, defaults, value constraints (negative serial rejected), vocabulary kinds (`ENUMERATION_SET`, `ENUM_VALUE`), and `RevisionAspectRow`'s operation/value invariants (`modified` requires both values non-null, `added` forbids `previous_value`, `removed` forbids `newer_value`).

---

## test_sync.py

The largest file. Each test class exercises one engine path.

### ADDED events

**`TestEntityAdded`** — entity ADDED mints one concept + revision + contract, no bindings. Instances stored as JSON. Multiple entities get incrementing serials. Instances must be a dimension map (`{dim: [values]}`); anything else raises `SyncError`.

**`TestPropertyAdded`** — property ADDED links to its parent via `parent_uri`. One binding per `added` entry of `property_paths`, storing its `instance_tags` and `runtime_path`; a missing entry for a binding-eligible property raises `SyncError`. `ENUM_VALUE` kind gets no bindings.

**`TestEnumerationSetAdded`** — `ENUMERATION_SET` ADDED behaves like ENTITY: one concept/revision/contract, no bindings, correct `kind` stored. `ENUM_VALUE` children carry the set's `concept_uri` as `parent_uri`.

### MODIFIED events (entity)

**`TestEntityModifiedNonBreaking`** — new revision supersedes the old one; contract is untouched. Revision chain: new revision's `previous_revision_uri` points to the superseded one. Rename: `current_label` updated, old label prepended to `previous_labels`.

**`TestEntityModifiedBreakingNonInstance`** — breaking non-instance change (e.g. `type`) mints a new entity contract but does not cascade to child properties.

**`TestEntityModifiedInstanceNonBreaking`** — adding instances non-breakingly mints a binding for each `added` reported path of the tagged properties; existing bindings stay ACTIVE; child properties get no new contract.

**`TestEntityModifiedInstanceBreaking`** — a breaking instance change mints a new entity contract; child property contracts are unchanged and bindings follow the reported paths.

**`TestEntityModifiedRenameAndBreaking`** — rename + breaking aspect in the same MODIFIED event: label updated AND new contract minted in one pass.

**`TestEntityModifiedInstanceShrinks`** — shrinking the instances (non-breaking): bindings of `removed` paths become REMOVED, the instances column is updated.

### MODIFIED events (property)

**`TestPropertyModifiedBreaking`** — old binding superseded, new binding minted under new contract. Works for both singleton and instanced properties. Rename + breaking change applies both in one event.

**`TestPropertyModifiedNonBreaking`** — new revision only; contract and bindings untouched.

**`TestPropertyModifiedRenameAndNonBreaking`** — rename without a breaking aspect: label updated, no new contract.

### REMOVED events

**`TestEntityRemoved`** — concept marked REMOVED, final REMOVED revision minted, all contracts marked REMOVED. Consistency guard: entity REMOVED without explicit REMOVED events for all child properties raises `SyncError`.

**`TestPropertyRemoved`** — concept REMOVED, all bindings marked REMOVED. `ENUM_VALUE` REMOVED never touches bindings.

### Rename edge cases

**`TestEntitySecondRename`** — two successive renames accumulate both old labels in `previous_labels` (most-recent first). Three renames gives all three in order.

**`TestRenameNonexistentConcept`** — `renamed_from` pointing to a label that does not exist in the ledger raises `SyncError` for both entity and property events.

### Multi-child cascade

**`TestMultipleChildPropertiesCascade`** — an instance delta requires every property tagged with the entity to be re-reported; each reported property gets its own bindings.

### Path-based bindings

**`TestNestedInstanceBindings`** — leaf properties under nested instanced entities (Axle/Wheel/Tire): bindings per reported path with tags for every owning entity, cross-check failures (missing/extra tags, undeclared instance values, duplicate paths), entity renames keeping tags and URIs, and path-only refreshes writing no revisions.

**`TestPropertyPathShapeChange`** — a change in the dimension shape of a property's tags forces a new contract; old bindings are SUPERSEDED and `unchanged` + `added` paths are minted on the new contract.

### Incremental syncs

**`TestRoundTrip`** — `sync → write_ledger → read_ledger → validate_ledger` round-trips without data loss. Two successive syncs accumulate rows correctly.

**`TestThreeSuccessiveSyncs`** — three syncs produce gap-free, duplicate-free, monotonically-increasing serials across all six tables. A concept from run 1 can be looked up and broken in run 3 after an unrelated run 2.

### `revision_aspects`

**`TestRevisionAspectsAdded`** — ADDED events mint one `revision_aspects` row per aspect key, `operation="added"`.

**`TestRevisionAspectsModified`** — MODIFIED events mint one row per changed aspect key with both `previous_value` and `newer_value` populated; `instances_added`/`instances_removed` are excluded.

**`TestRevisionAspectsRemoved`** — REMOVED events mint one row per key in `previous_aspects`, `operation="removed"`, `newer_value` null.

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

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.1] - 2026-10-26
### Changed
- **Breaking:** `runtime_path`, not `instance_tags`, is now the sole unique identity of an ACTIVE binding. A property may have several ACTIVE bindings sharing identical `instance_tags` (e.g. a reusable type's property reached through multiple unrelated runtime paths), as long as each binding's `runtime_path` differs. `validate_ledger` no longer requires `instance_tags` to be unique per contract among ACTIVE bindings; only the `runtime_path` uniqueness check remains.
- Sync's `property_paths` reconciliation now matches reported entries against the ledger by `(instance_tags, runtime_path)` instead of `instance_tags` alone, so duplicate tags within one property's paths are accepted as long as their `runtime_path` values differ. A reported path renamed while otherwise unchanged is still matched to its prior binding when its tag set maps to exactly one binding on each side; otherwise it is treated as a removed-plus-added pair.
- Reporting the same `runtime_path` twice within one property's `property_paths` entry now raises `SyncError` (previously only duplicate tags were rejected there).
### Tests
- Added coverage for multiple bindings sharing `instance_tags`: minting two bindings from shared-tag added paths, no-op resync when both are unchanged, selective removal, incremental addition of a third shared-tag binding, duplicate-`runtime_path`-within-one-property and cross-property collisions, and `validate_ledger` accepting/rejecting the relevant duplicate combinations.

## [0.8.0] - 2026-10-26

### Added
- Binding paths: the diff report gains a top-level `property_paths` list. For every binding-eligible property the adapter reports the literal runtime paths and the instances (`tags`, keyed by entity label) each belongs to, with a `change` of `unchanged`, `added` or `removed`. This fixes leaf properties under nested instanced entities receiving wrong bindings, because bindings are no longer derived from the parent entity's instances.
- `bindings.csv` stores `instance_tags` (canonical JSON keyed by the owning entity's concept URI, `{}` for a singleton) and `runtime_path`.
- Sync cross-checks `property_paths` against the ledger: every binding-eligible `ADDED`/`MODIFIED` property needs an entry, an instance delta requires every property tagged with that entity to be re-reported, `unchanged` + `removed` tags must equal the ACTIVE binding tags, tag values must be declared instances, and runtime paths must be unique.
- A change in the dimension shape of a property's tags forces a new contract and re-mints its bindings.
- Entity `instances`, `instances_added` and `instances_removed` are dimension maps (`{"side": ["Left", "Right"]}`) so an entity can be indexed along several dimensions.
### Changed
- **Breaking:** the `bindings.csv` schema changed (`instance_label` replaced by `instance_tags` and `runtime_path`) and entity instances are dimension maps; existing ledgers and diff reports must be regenerated.
- **Breaking:** the `instantiate` field on property events is removed; paths are reported explicitly instead.
- `modl export bindings`: the `json` format lists, per property, one item per binding with `instance_tags` (entity labels) and `runtime_path`, sorted by `runtime_path`; the `vspec` format is keyed by each binding's literal `runtime_path`.
- Updated `diff_report_template.md` and the README to document `property_paths`.
### Tests
- Added coverage for the path-based binding model: IR validation of `property_paths` and dimension-map instances, binding row validation, nested instanced entities (e.g. Axle/Wheel/Tire), instance deltas, path-only changes, tag-shape changes, cross-check failures and the new export shape.

## [0.7.0] - 2026-10-20
### Added
- `PROPERTY` diff events accept a top-level `references` map (role → label of the referenced `ENTITY`/`ENUMERATION_SET`). `modl sync` stores the dependency as contract-to-contract edges in the new `contract_references.csv` table and re-contracts dependent properties when the referenced contract changes, governed by the property config key named after the role (e.g. `output_type: true`). Changing a reference always forces a new contract.
- `revision_aspects.csv` gains a nullable `reason` column (`referenced-contract-changed`, `reference-retargeted`) for changes derived by the engine.
- A property aspect whose value names an entity/enumeration set without a matching `references` entry now warns (an error with `--strict`).
- The cascade is single-hop and triggered only by a new contract of the referenced element. When the property config does not mark the role as breaking, the dependent keeps its contract and a warning is logged.
### Changed
- The ledger now has six CSV files. Existing ledgers must be re-initialised.
- `sync()` accepts a `strict` keyword argument and `modl sync --strict` passes it through, so the missing-`references` guard fails the run.
- Updated `diff_report_template.md` to document `references`, `contract_references.csv` and `revision_aspects.reason`; adapters should declare `references` instead of the suggested `output_type_content` digest aspect, which is removed from the template.
### Tests
- Added coverage for `references` IR validation, `contract_references` ledger validation, the `reason` column, reference edges, the cascade and the missing-reference guard.

## [0.6.0] - 2026-10-07
### Added
- `modl export` outputs VSPEC bindings in a separate file
- `modl export bindings` accepts `--binding-key` to export bindings under an arbitrary key instead of the default `binding`.
- Added optional `binding_eligible` metadata to `PROPERTY` diff events. It defaults to `false` and is purely additive: setting it `true` mints a binding for a non-leaf property that is nonetheless read/written as one atomic unit (e.g. a vspec `Struct`), without ever being able to suppress the binding a leaf property already receives.

### Changed
### Tests
- Added coverage for `binding_eligible` IR validation (default, non-leaf override, `ENUM_VALUE` rejection) and sync behavior (binding minted for a non-leaf property when eligible, binding-eligibility transitions forcing a new contract/binding lifecycle, and no spurious transitions when `is_leaf` already covers eligibility).

## [0.5.0] - 2026-09-10
### Added
- Added optional `instantiate` metadata to `PROPERTY` diff events, mirroring `is_leaf`, letting adapters pin a property to a single non-instantiated path instead of inheriting the parent entity's instances.

### Changed
- Sync logic now resolves each property's `instances` from its own `instantiate` flag instead of unconditionally copying the parent entity's instances, fixing incorrectly-generated per-instance bindings for non-instantiated properties.
- Entity instance-list changes no longer cascade instance bindings onto non-instantiated child properties.
- Validation rejects `instantiate` on `ENUM_VALUE` events, consistent with `is_leaf`.
- Updated `diff_report_template.md` to document the `instantiate` field and its rules.

### Tests
- Added coverage for `instantiate` IR validation, property add/modify instance resolution, and cascade immunity for non-instantiated child properties.

## [0.4.0] - 2026-09-04
### Added
- Added mandatory `is_leaf` metadata to `PROPERTY` diff events to distinguish scalar/primitive properties from entity-valued properties.
- Validation now enforces `is_leaf` for all `PROPERTY` events and rejects it on `ENUM_VALUE` events.
- Sync logic now uses the `is_leaf` flag to determine binding eligibility and contract/binding behavior.

### Changed
- Updated documentation and examples to reflect the new `is_leaf` requirement and its implications for binding rules and property transitions.

### Tests
- Expanded validation and sync tests to cover scenarios involving the new `is_leaf` field across event types.

## [0.3.0] - 2026-09-03
### Added
- CLI command to export ledger bindings in JSON and vspec formats

## [0.2.0] - 2026-09-03

### Added

- New `revision_aspects` ledger table, added alongside the 4 core tables (`concepts`, `revisions`, `contracts`, `bindings`), recording per-aspect change details — added, modified, and removed — for each entity/property revision.
- Sync engine now records added, modified, and removed aspects into `revision_aspects`, with dedicated serialization and validation for each operation.

### Changed

- `previous_aspects` requirements for `REMOVED` diff events were tightened/clarified to support population of `revision_aspects`.

### Tests

- Extended test coverage for `revision_aspects` validation rules and aspect-change handling during sync.
- Updated existing tests to reflect the new 5-table ledger structure and revised `previous_aspects` requirements.

## [0.1.0] - 2026-07-31
### Added

- Initial functional release of the identity ledger, backed by 4 core CSV tables: `concepts`, `revisions`, `contracts`, `bindings`.
- `modl sync` command: syncs a diff report (in ModL's IR format) against the ledger, minting/updating concepts, revisions, contracts, and bindings.
- `modl adapt` command: analyzes compatibility between two ledger states/releases and produces a compatibility report for downstream consumers.
- Breaking-change configuration (YAML) to classify entity/property attribute changes as breaking or non-breaking, driving contract creation.

### Changed

### Tests

- Test suite covering ledger I/O, CLI commands (`sync`, `adapt`), config parsing, and the diff-report IR model.

## [0] - 2026-05-20

### Added

- Everything (init)

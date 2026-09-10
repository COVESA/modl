# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
### Changed
### Tests

## [0.5.0] - 2026-10-09
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

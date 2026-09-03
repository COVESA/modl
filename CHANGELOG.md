# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
### Changed
### Tests

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

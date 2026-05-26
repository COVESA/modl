# Diff Report Format — Adapter Guide

This document explains the diff report format that `modl` consumes and describes exactly what a language-specific adapter must produce to make any modeling language compatible with the ledger.

---

## What is an adapter?

`modl` is language-agnostic. It does not parse model files directly. A **language-specific adapter** is a tool (script, library, CI step) that:

1. Takes a current model snapshot, and optionally a previous one
2. Computes what changed between them (or treats everything as new when no previous snapshot exists)
3. Produces a **diff report** — a JSON file in the format described below
4. Passes that file to `modl sync --diff-report <file> ...`

The previous snapshot is **optional**. When absent, the adapter is in first-run mode: every element in the current snapshot is treated as `ADDED` and emitted with its complete `aspects` snapshot. From `modl`'s perspective the format is identical — it always receives a diff report and does not know whether it was a first run.

A typical adapter invocation:

```
adapter --curr model-v2.yaml               # first run: no prev, all ADDED
adapter --prev model-v1.yaml --curr model-v2.yaml  # subsequent runs: real diff
```

One adapter exists per modeling language (e.g., vspec, GraphQL SDL, JSON Schema). The adapter is a thin, replaceable component; `modl`'s ledger logic does not change when a new language is supported.

---

## Terminology

| IR term | Also known as |
|---|---|
| **Entity** | Container, object type, branch, class, feature of interest |
| **Property** | Field, attribute, signal, sensor, actuator, characteristic |
| **Aspect** | Any named attribute of a property that can change (output type, unit, constraints, …) |

---

## Top-level structure

```json
{
  "changes": [ <event>, <event>, ... ]
}
```

The `changes` array is an ordered list of change events. Order does not affect correctness — the sync engine processes events independently. Each event describes a change to either an **entity** or a **property**.

---

## Entity event

```json
{
  "label":        "<string>",
  "kind":         "ENTITY",
  "change_type":  "ADDED" | "REMOVED" | "MODIFIED",
  "renamed_from": "<string>" | null,
  "aspects":      { "<key>": <value>, ... },
  "content":      [ { "label": "<string>", "change_type": "ADDED" | "REMOVED" | "MODIFIED" }, ... ]
}
```

| Field | Required | Notes |
|---|---|---|
| `label` | always | The current label of the entity (after any rename). |
| `kind` | always | Must be `"ENTITY"`. |
| `change_type` | always | `ADDED`, `REMOVED`, or `MODIFIED`. |
| `renamed_from` | `MODIFIED` only | Previous label. Signals the ledger to record a rename rather than a separate removal and addition. Must be `null` or absent on `ADDED` and `REMOVED`. |
| `aspects` | `ADDED` | Full initial-state snapshot of all entity-level attributes. Empty on `REMOVED`. Delta (changed keys only) on `MODIFIED`. |
| `content` | `MODIFIED` only | Summary of which child properties changed. Each item carries `label` and `change_type`. Absent on `ADDED` and `REMOVED`. The sync engine uses this summary to record which properties were affected by an entity-level change (e.g., a breaking instance-list change that forces new variants on all children). |

### Rules

- **ADDED**: `aspects` carries the full snapshot. `content` must be absent. `renamed_from` must be absent.
- **MODIFIED**: `aspects` carries only the keys that actually changed. `content` lists affected children. `renamed_from` is set only when a rename occurred.
- **REMOVED**: `aspects` must be empty. `content` must be absent. `renamed_from` must be absent.

---

## Property event

```json
{
  "label":        "<string>",
  "parent_label": "<string>",
  "kind":         "PROPERTY",
  "change_type":  "ADDED" | "REMOVED" | "MODIFIED",
  "renamed_from": "<string>" | null,
  "aspects":      { "<key>": <value>, ... }
}
```

| Field | Required | Notes |
|---|---|---|
| `label` | always | The current label of the property. |
| `parent_label` | always | The label of the immediate parent entity. |
| `kind` | always | Must be `"PROPERTY"`. |
| `change_type` | always | `ADDED`, `REMOVED`, or `MODIFIED`. |
| `renamed_from` | `MODIFIED` only | Previous label. Must be `null` or absent on `ADDED` and `REMOVED`. |
| `aspects` | `ADDED` | Full initial-state snapshot on `ADDED`. Empty on `REMOVED`. Delta on `MODIFIED`. |

### Rules

- **ADDED**: `aspects` carries the full snapshot; `output_type` is expected to be present. `renamed_from` must be absent.
- **MODIFIED**: `aspects` carries only the keys that changed. `renamed_from` is set only when a rename occurred.
- **REMOVED**: `aspects` must be empty. `renamed_from` must be absent.

---

## Aspect keys

`aspects` is a flat `string → any` dictionary. Three keys are **canonical** — understood by `modl` regardless of configuration:

| Key | Type | Meaning |
|---|---|---|
| `output_type` | `string` | Base type name the property resolves to (e.g. `"Float"`, `"Boolean"`, `"Door"`). Does not include list or nullability modifiers. |
| `is_list` | `boolean` | `true` when the property resolves to a list of `output_type`. |
| `is_required` | `boolean` | `true` when the value is guaranteed non-null / mandatory. |

All other keys are **adapter-defined**. The adapter chooses their names. Examples: `unit`, `min`, `max`, `accuracy`, `description`. The breaking-change config references them by their exact key name.

> Adapter-defined keys in `MODIFIED` events that are not declared in the breaking-change config are treated as **non-breaking by default** and produce a warning. Pass `--strict` to `modl sync` to treat them as errors.

---

## Rename semantics

A rename is represented as a `MODIFIED` event with `renamed_from` set to the previous label. This preserves concept identity in the ledger — the concept URI does not change.

```json
{
  "label":        "Vehicle.Velocity",
  "parent_label": "Vehicle",
  "kind":         "PROPERTY",
  "change_type":  "MODIFIED",
  "renamed_from": "Vehicle.Speed",
  "aspects":      {}
}
```

If the adapter cannot detect a rename (no explicit annotation in the model), it should emit a `REMOVED` event for the old label and an `ADDED` event for the new label. The ledger will treat these as two distinct concepts with separate URIs, and concept identity is lost.

Modeling languages that support explicit rename annotations (e.g., `fka` in vspec, `@renamed` directives in GraphQL SDL) should map them to `renamed_from`.

### Rename with simultaneous attribute change

A single `MODIFIED` event can carry both `renamed_from` and a non-empty `aspects` delta when an element was renamed and had other attributes change in the same release:

```json
{
  "label":        "Vehicle.Velocity",
  "parent_label": "Vehicle",
  "kind":         "PROPERTY",
  "change_type":  "MODIFIED",
  "renamed_from": "Vehicle.Speed",
  "aspects":      { "unit": "m/s" }
}
```

The sync engine evaluates the rename and the aspect delta independently against the config. Either one may independently trigger a new variant.

---

## When a new property is added to an entity

Emit **two** events: one `MODIFIED` on the parent entity (content changed) and one `ADDED` on the new property. Each is processed independently.

```json
{
  "changes": [
    {
      "label":       "Vehicle.Door",
      "kind":        "ENTITY",
      "change_type": "MODIFIED",
      "content": [
        { "label": "Vehicle.Door.IsLocked", "change_type": "ADDED" }
      ]
    },
    {
      "label":        "Vehicle.Door.IsLocked",
      "parent_label": "Vehicle.Door",
      "kind":         "PROPERTY",
      "change_type":  "ADDED",
      "aspects": {
        "output_type": "Boolean",
        "is_list":     false,
        "is_required": false
      }
    }
  ]
}
```

---

## Complete example

The following diff report covers a range of typical changes:

```json
{
  "changes": [
    {
      "label":       "Vehicle.Window",
      "kind":        "ENTITY",
      "change_type": "ADDED",
      "aspects": { "type": "branch" }
    },
    {
      "label":        "Vehicle.Window.Position",
      "parent_label": "Vehicle.Window",
      "kind":         "PROPERTY",
      "change_type":  "ADDED",
      "aspects": {
        "output_type": "Float",
        "is_list":     false,
        "is_required": false,
        "unit":        "percent",
        "min":         0,
        "max":         100
      }
    },
    {
      "label":       "Vehicle.Door",
      "kind":        "ENTITY",
      "change_type": "MODIFIED",
      "aspects": { "instances": ["Left", "Right", "Center"] },
      "content": [
        { "label": "Vehicle.Door.IsLocked", "change_type": "ADDED" }
      ]
    },
    {
      "label":        "Vehicle.Door.IsLocked",
      "parent_label": "Vehicle.Door",
      "kind":         "PROPERTY",
      "change_type":  "ADDED",
      "aspects": { "output_type": "Boolean" }
    },
    {
      "label":        "Vehicle.Speed",
      "parent_label": "Vehicle",
      "kind":         "PROPERTY",
      "change_type":  "MODIFIED",
      "aspects": { "output_type": "Float" }
    },
    {
      "label":        "Vehicle.Velocity",
      "parent_label": "Vehicle",
      "kind":         "PROPERTY",
      "change_type":  "MODIFIED",
      "renamed_from": "Vehicle.OldSpeed",
      "aspects":      {}
    },
    {
      "label":        "Vehicle.OldFeature",
      "parent_label": "Vehicle",
      "kind":         "PROPERTY",
      "change_type":  "REMOVED"
    }
  ]
}
```

---

## Adapter implementation checklist

Use this checklist when building an adapter for a new modeling language:

- [ ] Parse both the previous and current model snapshots. **When no previous snapshot is provided (first run), treat every element as `ADDED` and emit the complete `aspects` snapshot for each entity and property — not a delta.** This is identical to the standard `ADDED` event contract and requires no special handling from `modl`.
- [ ] For each entity that exists in current but not previous: emit `ADDED` entity event with full `aspects` snapshot
- [ ] For each entity that exists in previous but not current: emit `REMOVED` entity event
- [ ] For each entity that exists in both:
  - [ ] Detect renames via explicit model annotations → emit `MODIFIED` with `renamed_from`
  - [ ] If the element was also modified in the same release, include both `renamed_from` and the changed keys in `aspects` within the same event
  - [ ] Detect changes to entity-level attributes → emit `MODIFIED` with changed keys in `aspects`
  - [ ] Detect added/removed/modified child properties → emit `MODIFIED` entity event with `content` summary **and** individual property events
- [ ] For each property that exists in current but not previous: emit `ADDED` property event with full `aspects` (include `output_type`)
- [ ] For each property that exists in previous but not current: emit `REMOVED` property event
- [ ] For each property that exists in both and has changed:
  - [ ] Detect renames → emit `MODIFIED` with `renamed_from`
  - [ ] If the element was also modified in the same release, include both `renamed_from` and the changed keys in `aspects` within the same event
  - [ ] Compute delta of changed aspect keys → emit `MODIFIED` with only changed keys in `aspects`
- [ ] Map language-specific attribute names to consistent aspect key names (e.g., vspec `datatype` → `output_type`)
- [ ] Ensure `output_type` carries the base type name only (no list brackets, no `!` suffix)
- [ ] Set `is_list` and `is_required` separately for languages that express them (e.g., GraphQL `[Type]!`)
- [ ] Output valid JSON with a top-level `"changes"` array
- [ ] Validate the output against `modl`'s schema before passing it to `modl sync`

---

## Breaking-change configuration

The breaking-change config tells `modl` which aspect keys constitute a data-contract change. The adapter does not need to know this — it reports all changes; `modl` decides which are breaking.

```yaml
namespace:
  namespace: "https://myproject.org/model/"
  prefix: "mp"

entity:
  instances: true   # breaking — triggers a new variant
  type: true        # breaking — triggers a new variant
  name: false       # renames are non-breaking; suppresses --strict warnings

property:
  output_type: true  # breaking — triggers a new variant
  unit: true         # breaking — triggers a new variant
  is_required: true  # breaking — triggers a new variant
  accuracy: true     # user-defined domain attribute; breaking
  description: false # known, non-breaking; suppresses --strict warnings
```

Each key maps to a boolean with three distinct states:

| Value | Meaning |
|---|---|
| `true` | Aspect is **breaking** — a change triggers a new variant. |
| `false` | Aspect is **known but non-breaking** — changes are accepted silently; no warning even with `--strict`. |
| *(absent)* | Aspect is **unknown** — treated as non-breaking but produces a warning (error with `--strict`). |

The reserved key `name` governs rename events (`renamed_from` non-null on a `MODIFIED` event). It never appears in `aspects` — it controls only rename classification. Canonical keys (`output_type`, `is_list`, `is_required`) are always treated as known regardless of the config.

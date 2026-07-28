# Experiment Registry and Results Dashboard

- Status: initial MVP implemented; follow-up hardening remains
- Target: controller, CLI, output sync, and explicit Web result decisions
- Last updated: 2026-07-28

## 0. Implementation Snapshot

The initial MVP now implements the product decisions that were left open in the
original proposal:

- Contract names remain unversioned; compatibility uses `schema_version: 1`.
- Plan publication remains a CLI/controller write. The live Web dashboard now
  records explicit accept/reject decisions with confirmation, reason, immutable
  IDs, and compare-and-swap protection; the bundled snapshot remains read-only.
- Eligible results are never accepted automatically.
- Native results enter through a verified output-sync receipt. Direct result
  ingestion is reserved for producers declaring `legacy_adapter` mode.
- Existing purge commands fail closed when an ingested result references a run;
  provenance tombstones remain follow-up work before those runs can be purged.
- `?view=experiments` uses live controller queries, while
  `?demo=experiments` remains an explicitly synthetic UI fixture. An empty live
  registry never falls back to demo data.

Implemented surfaces include contract normalization, immutable journal events,
the rebuildable SQLite projection, plan preview/publication, frozen run bindings,
output-sync result verification and ingestion, explicit acceptance, bounded
queries, the experiment CLI action tree, and the Results/Curves/Point Matrix/detail
web views.

The long-term design below remains the source for follow-up work. The first MVP
does not yet include registry doctor/export commands, purge-time provenance
tombstones, projection migrations/backups, browser write actions, or paginated
impact rows for exceptionally large plan previews.

## 1. Outcome

Remote Runner should add a project-scoped experiment registry that connects a
published scientific design to its logical points, exact point revisions,
explicitly bound runs, structured result candidates, and explicit accepted
results. The registry should let a researcher or agent answer questions such as
"what is complete?", "what became stale?", and "what needs rerunning?" without
scanning commands, logs, arbitrary output files, or every run manifest.

The feature is a scientific index over the existing execution lifecycle. It does
not replace that lifecycle:

| Fact | Authority |
| --- | --- |
| Submitted Git revision and frozen command | Existing queue and run manifests |
| Queue and execution lifecycle | Existing controller queue state, run state, and run events |
| Archived output existence and transfer verification | Existing output-sync completion receipt and canonical archive |
| Published design, active design revision, ingested result, and acceptance decision | Controller-owned immutable experiment journal |
| Filtered status, aggregates, curves, and point detail | Rebuildable SQLite projection |
| Browser search, selected tab, and visible pagination state | Browser only |

Timestamps are audit and display fields only. They must never choose the active
design, current point revision, accepted result, replacement result, or official
curve value.

## 2. Repository Fit

The current repository already provides the needed boundaries:

- A controller project namespace at
  `<controller.root>/projects/<project_id>/.remote-runner/`.
- Immutable run manifests plus revision-guarded run state under `runs/`.
- An append-only `runs.jsonl` event stream with event IDs.
- Controller queue job/state records and explicit run IDs.
- Open `result_intent` and `result_tags` metadata on queue and run records.
- Output-sync intents, verified completion receipts, and canonical archive paths.
- A local loopback Starlette app that relays controller data without exposing SSH
  configuration to the browser.
- A React dashboard whose operational data currently arrives as a controller
  snapshot over server-sent events.

The experiment feature should extend these boundaries rather than widen the
existing operational snapshot. Experiment list/detail queries are on-demand,
paginated controller requests. The Runs page may keep its current snapshot/SSE
path.

No repository-native task or design-proposal directory currently exists, so this
document establishes `docs/plans/` for the proposal without introducing a new
runtime convention.

## 3. Scope

### 3.1 Remote Runner owns

- The generic `experiment_plan`, `run_binding`, `experiment_result`, and
  `experiment_query` contracts.
- Contract validation, canonicalization, digests, opaque IDs, idempotency, and
  compatibility rules.
- Atomic plan preview/publication and the explicit active-design pointer.
- The controller-side immutable experiment journal and SQLite projection under
  each project namespace.
- Projection from authoritative queue/run records, run events, output-sync
  receipts, and bounded structured result manifests.
- Generic result-candidate, acceptance, stale, archive, history, and rerun
  semantics.
- Token-bounded CLI/controller/browser query APIs.
- A generic Experiments dashboard with study status, Results, Curves, Point
  Matrix, and exact point detail.
- A standard result-manifest producer contract and an explicit legacy-adapter
  ingestion boundary.

### 3.2 Project adapters own

- Compiling a project's Markdown or domain configuration into a normalized
  `experiment_plan` document.
- Defining domain components and their dependencies, such as named algorithms,
  decoders, budgets, or resolution settings.
- Mapping historical project artifacts into `experiment_result` with explicit
  provenance.
- Domain-specific migration fixtures and domain-specific metric definitions.

For the originating project, this means its Markdown-to-plan compiler,
Sharingan/Relay/Resolution dependency definitions, historical decoder mapping,
and LER migration fixtures stay outside Remote Runner core.

### 3.3 Explicitly out of scope

- Dynamic Markdown query blocks or automatic writes back into Markdown.
- Raw per-shot records, large arrays, logs, or artifact bodies in SQLite or in
  query responses.
- Inferring point identity, settings, metrics, or acceptance from commands,
  stdout, stderr, filenames, timestamps, or arbitrary tags.
- Replacing queue/run state or the canonical output archive with SQLite.
- Automatic dispatch, rerun, or stop as a side effect of plan publication.
- Silent import, silent method rename, or implicit acceptance of legacy results.
- Arbitrary SQL access from the CLI, browser, or agents.

## 4. Common Contract Rules

The four structured JSON contracts use stable, unversioned names and the
repository's existing schema metadata style:

```json
{
  "kind": "experiment_plan",
  "schema_version": 1
}
```

Contract names deliberately do not carry schema suffixes. The `schema_version`
field is storage and protocol compatibility metadata, not part of the product
vocabulary. Persisted and exchanged documents are UTF-8 JSON objects. YAML may
be a project adapter's input, but it is not a persisted contract
representation.

All contracts share these rules:

1. IDs are opaque, kind-prefixed, randomly generated identities. A study,
   design revision, point, point revision, result, acceptance, binding, and
   experiment event ID is immutable and never reused.
2. `canonical_key` is a stable CLI/config key. Study keys are unique in a
   project; point keys are unique in a study; dimension, component, and metric
   keys are unique in their declared catalogs. Existing entities must be
   addressed by ID for publication mutations; a key alone cannot silently
   retarget an existing entity.
3. `display_name` is mutable presentation data. Aliases are append-only history;
   changing a display name or adding an alias does not change point or result
   identity.
4. Digests use `sha256:<64 lowercase hex>`. The implementation must select and
   publish one canonical JSON algorithm before fixtures are frozen; RFC 8785 JCS
   is the preferred baseline. Self-digest fields are excluded from their own
   digest.
5. Identity-affecting values are normalized before hashing. JSON `NaN`,
   infinities, platform paths, locale-formatted numbers, and unordered sets are
   invalid.
6. Dimension values are bounded JSON scalars. Opaque structured parameters live
   in an explicitly bounded `parameters` object and are not queryable unless a
   future schema promotes them.
7. Audit timestamps are required where useful but never establish scientific
   precedence. Explicit IDs, digests, event sequence, and compare-and-swap
   pointers do that.
8. Unknown fields are rejected in identity-bearing objects. Extension metadata
   is allowed only inside named, size-bounded `metadata` objects.
9. Commands, logs, prepared-server payloads, SSH endpoints, and raw samples are
   not fields in any experiment contract.

The digest domains are deliberately separate:

- `plan_digest` covers the complete normalized declarative design, including
  canonical keys and presentation, but excluding publication CAS fields,
  controller-assigned opaque IDs, and `plan_digest` itself. It therefore stays
  stable when publication materializes IDs for new entities.
- `setting_digest` covers a point's normalized scientific parameters and the
  resolved digests of only the setting components it declares as dependencies.
- `point_revision_digest` covers dimensions, `setting_digest`, result
  requirements, and other identity-bearing point requirements. It excludes
  display names, aliases, ordering, and presentation.
- `binding_digest` and `manifest_digest` cover their complete normalized
  documents except their own digest fields.

ID allocation is explicit so retries and rebuilds preserve the same identities:

| Identity | Allocator |
| --- | --- |
| Run ID | Existing Remote Runner submission path |
| Study, design revision, point, point revision, acceptance, and experiment event IDs | Controller at the journal commit point |
| Binding ID | Submission client when finalizing the immutable binding |
| Result manifest and result IDs | Native producer or explicit legacy adapter |

The controller validates uniqueness and persists every allocated ID in the
immutable source record. Rebuild never generates replacement IDs.

## 5. `experiment_plan`

### 5.1 Purpose

The plan is the complete normalized input to preview and publication. A project
adapter may generate it, but Remote Runner validates it without understanding
the domain meaning of a dimension, component, or metric key.

### 5.2 Proposed shape

```json
{
  "kind": "experiment_plan",
  "schema_version": 1,
  "study": {
    "study_id": null,
    "canonical_key": "main-sweep",
    "display_name": "Main sweep",
    "aliases": [],
    "description": "Optional bounded text",
    "metadata": {}
  },
  "expected_active_design_revision_id": null,
  "dimensions": [
    {
      "key": "method",
      "display_name": "Method",
      "value_type": "string",
      "order": ["baseline", "candidate"]
    },
    {
      "key": "size",
      "display_name": "Size",
      "value_type": "integer",
      "order": [7]
    }
  ],
  "setting_components": [
    {
      "key": "algorithm",
      "digest": "sha256:...",
      "metadata": {}
    }
  ],
  "metrics": [
    {
      "key": "error_rate",
      "display_name": "Error rate",
      "value_type": "number",
      "unit": "1/round",
      "default_format": "percentage"
    }
  ],
  "points": [
    {
      "point_id": null,
      "reuse_point_revision_id": null,
      "canonical_key": "baseline-size-7",
      "display_name": "Baseline, size 7",
      "aliases": [],
      "dimensions": {
        "method": "baseline",
        "size": 7
      },
      "parameters": {},
      "setting_dependencies": ["algorithm"],
      "setting_digest": "sha256:...",
      "result_requirements": {
        "required_metrics": ["error_rate"],
        "minimum_observations": 1000,
        "required_artifact_roles": ["summary"],
        "required_checks": []
      },
      "point_revision_digest": "sha256:...",
      "metadata": {}
    }
  ],
  "presentation": {
    "primary_metric": "error_rate",
    "results": {
      "dimensions": ["method", "size"],
      "metrics": ["error_rate"]
    },
    "curves": [
      {
        "key": "error-rate-by-size",
        "display_name": "Error rate by size",
        "metric": "error_rate",
        "x_dimension": "size",
        "series_dimensions": ["method"],
        "scale": "log",
        "show_interval": true
      }
    ],
    "matrix": {
      "row_dimension": "method",
      "column_dimension": "size",
      "facet_dimensions": []
    }
  },
  "plan_digest": "sha256:..."
}
```

The sample keys are generic examples, not core vocabulary. In particular, Remote
Runner must not define LER fields. A project can declare a per-round LER metric,
block LER, logical-error count, shot count, and confidence interval using the
same metric and presentation structures.

### 5.3 Identity and revision rules

- Creating a study requires `study_id: null`, a globally unused canonical key in
  the project namespace, and `expected_active_design_revision_id: null`.
- Updating a study requires its immutable `study_id`. The canonical key must
  match the stored identity. Query commands may resolve keys for convenience,
  but publication never mutates by an ambiguous name lookup.
- A new point has `point_id: null`. An existing point must include its `point_id`;
  omission cannot create a duplicate with an existing canonical key.
- A display rename or alias addition is presentation-only and reuses the same
  point revision and accepted-result pointer.
- A change to dimensions, parameters, result requirements, or any depended-on
  setting-component digest creates a new immutable point revision.
- A changed component only affects points that list that component in
  `setting_dependencies`.
- A change to the scientific question is represented by a new study, not a
  rename or point-revision trick.
- A point omitted from a new design revision is archived in that revision. Its
  identities and history remain queryable.
- Reactivating an archived point never silently falls back to a historical
  result. The plan must set `reuse_point_revision_id` to an exact historical
  point revision, with matching digests, or publication creates a new point
  revision with no accepted result. Active points leave this field null.

### 5.4 Preview and impact

Preview is read-only. It validates and canonicalizes the complete plan, checks
the expected active revision, and returns:

- Candidate `plan_digest` and a separate `impact_digest`.
- The currently active design revision ID.
- Counts and paginated point records classified as `unchanged`, `new`, `stale`,
  or `archived`.
- Stable reason codes, changed component keys, old/new point revision IDs when
  known, and whether an exact accepted result remains reusable.
- A publication precondition containing the expected active revision, plan
  digest, and impact digest.

The impact digest covers all submitted existing-entity IDs, new-entity canonical
keys, the expected active head, plan digest, and classified impact. Preview does
not allocate durable IDs. It still detects an ID retarget even though
controller-assigned IDs are not part of the stable declarative `plan_digest`.

Classification is relative to the explicit active revision, never to the most
recent timestamp:

| Class | Meaning |
| --- | --- |
| `unchanged` | The point is in both designs and its point-revision digest is identical. |
| `new` | The point becomes active but was not in the active design, including an explicitly reactivated point. |
| `stale` | The same logical point remains active but requires a new point revision. |
| `archived` | The point is in the active design and omitted from the candidate design. |

Name-only and presentation-only changes are `unchanged` for scientific impact.
The preview response may summarize a large plan, but every affected row remains
available through bounded pagination.

### 5.5 Atomic publication

Publication includes the normalized plan, `expected_active_design_revision_id`,
preview `impact_digest`, and a caller-generated `request_id`.

Under the per-project experiment lock, the controller must:

1. Catch the SQLite projection up to the immutable journal head.
2. Recompute every supplied digest and reject tampering.
3. Compare the explicit expected active revision with the journal's current
   active revision.
4. Recompute impact and reject a preview mismatch.
5. Allocate all new opaque IDs.
6. Atomically create one immutable `design_published` journal envelope containing
   the full normalized plan, allocated identities, impact, old head, and new
   head.
7. Apply that event to SQLite in one transaction before returning the normal
   success response.

The journal event is the commit point. If the process stops after journal fsync
but before SQLite commit, retrying the same `request_id` and digest returns the
same design revision and completes projection. Reusing a request ID with a
different digest is an error. A different request with a stale expected head is
a conflict even when its plan digest matches. With the current head as its
precondition, publishing content that is already active is a no-op rather than a
new timestamp-selected revision.

Publication does not submit, cancel, stop, or rerun any workload.

## 6. `run_binding`

### 6.1 Purpose

A binding explicitly declares which exact point revisions a run may contribute
to. It is finalized at submission and stored in the immutable queue job/run
manifest path. `result_tags` remain searchable metadata but are not scientific
identity.

The CLI should accept `remote-runner run --experiment-binding FILE`, allocate the
run ID as it does today, inject the exact submitted Git revision, validate the
final document, compute its digest, and freeze it into controller records.

### 6.2 Proposed shape

```json
{
  "kind": "run_binding",
  "schema_version": 1,
  "binding_id": "binding-opaque-id",
  "run_id": "rr-0123456789abcdef",
  "source_revision": "40-character-git-sha",
  "targets": [
    {
      "study_id": "study-opaque-id",
      "origin_design_revision_id": "design-opaque-id",
      "plan_digest": "sha256:...",
      "point_id": "point-opaque-id",
      "point_revision_id": "point-revision-opaque-id",
      "point_revision_digest": "sha256:...",
      "setting_digest": "sha256:...",
      "result_group_id": "producer-stable-group-id",
      "contribution_role": "primary"
    }
  ],
  "result_manifest_relpath": "experiment-result.json",
  "expects_result_manifest": true,
  "metadata": {},
  "binding_digest": "sha256:..."
}
```

Rules:

- One binding belongs to one run but may target multiple point revisions,
  including points in different studies in the same project.
- The same point result may name several bound runs as primary, continuation, or
  replacement contributions. This provides many-to-many point/run support
  without inferring relationships from task IDs.
- A replacement relationship is explicit in result contributions; the registry
  does not assume that a later run replaces an earlier run.
- All referenced point revisions and digests must already exist. Binding to a
  draft, unknown, or digest-mismatched point fails before submission.
- At submission, every target's `origin_design_revision_id` must be that study's
  active revision and must contain the exact point revision. A concurrent design
  change rejects the submission instead of silently binding historical work.
  If the design changes after submission, the frozen binding remains valid for
  history but cannot complete the new active point revision.
- An experiment-bound result-producing run uses a directory output and a bounded
  structured manifest at `result_manifest_relpath`. Supporting or excluded runs
  may set `expects_result_manifest: false` but still retain explicit targets.
- Dispatcher retries copy the same binding bytes and digest. They do not create
  new scientific identities.

## 7. `experiment_result`

### 7.1 Purpose

The result manifest carries bounded structured scientific summaries from a
synchronized canonical output. It is the only standard producer path for new
metrics. Stdout, commands, generic tags, and arbitrary artifact discovery are
never result parsers.

### 7.2 Proposed shape

```json
{
  "kind": "experiment_result",
  "schema_version": 1,
  "manifest_id": "result-manifest-opaque-id",
  "emitter_run_id": "rr-0123456789abcdef",
  "producer": {
    "name": "project-producer",
    "version": "1.2.3",
    "mode": "native"
  },
  "results": [
    {
      "result_id": "result-opaque-id",
      "study_id": "study-opaque-id",
      "origin_design_revision_id": "design-opaque-id",
      "plan_digest": "sha256:...",
      "point_id": "point-opaque-id",
      "point_revision_id": "point-revision-opaque-id",
      "point_revision_digest": "sha256:...",
      "setting_digest": "sha256:...",
      "result_group_id": "producer-stable-group-id",
      "contributions": [
        {
          "run_id": "rr-0123456789abcdef",
          "binding_id": "binding-opaque-id",
          "binding_digest": "sha256:...",
          "role": "primary",
          "replaces_run_id": null
        }
      ],
      "metrics": [
        {
          "key": "error_rate",
          "value": 0.0012,
          "interval": {
            "lower": 0.0010,
            "upper": 0.0015,
            "level": 0.95,
            "method": "project-declared"
          }
        }
      ],
      "evidence": {
        "observation_count": 100000,
        "checks": []
      },
      "artifacts": [
        {
          "run_id": "rr-0123456789abcdef",
          "role": "summary",
          "relative_path": "summary.json",
          "sha256": "sha256:...",
          "media_type": "application/json"
        }
      ],
      "metadata": {}
    }
  ],
  "manifest_digest": "sha256:..."
}
```

### 7.3 Bounds and validation

- A manifest has fixed byte, result-count, metric-count, artifact-reference, and
  metadata limits. The initial implementation should target a maximum manifest
  size no larger than 1 MiB and use lower per-field limits where practical.
- Metric values and interval bounds are finite JSON numbers. Large arrays,
  histograms, per-shot values, traces, and samples are artifacts, not metrics.
- Metric keys must be declared by the published plan. Metric meaning and unit
  come from that exact plan and cannot be redefined by a result manifest.
  Required metrics, observation counts, artifact roles, and declared checks are
  validated against the exact point revision.
- Artifact paths are normalized relative paths under the referenced run's
  synchronized output. Path traversal, symlinks, absolute paths, and digest
  mismatches fail ingestion.
- Every contribution must match an immutable `run_binding`, and every
  contributing run used as evidence must have a succeeded authoritative state
  and a verified output-sync receipt.
- A manifest may contain results for several points. A result may cite several
  runs. A run may contribute to several results.
- `result_id` and `manifest_id` are idempotency identities. Repeating an exact ID
  and digest is a no-op; reusing an ID with different content is a conflict.
- An exact result for a historical point revision may be ingested and displayed
  in history, but it cannot satisfy the active point revision.
- A result is immutable. Correction creates a new result and, if needed, an
  explicit superseding acceptance decision.

### 7.4 Output-sync-driven ingestion

For an experiment-bound run, output synchronization should be extended as
follows:

1. Preserve the existing succeeded-state gate and rsync checksum verification.
2. On the archive target, read only the exact `result_manifest_relpath` from the
   binding. Enforce regular-file, size, JSON-schema, and path-safety checks.
3. Verify declared artifact digests at the archive target.
4. Return the bounded canonical manifest bytes or equivalent canonical JSON,
   its digest, and artifact-verification summary in the completion receipt.
5. On the controller, revalidate the receipt, run bindings, point/setting
   identities, result requirements, and contribution set.
6. Atomically append one idempotent `result_ingested` experiment-journal event
   containing the small canonical manifest plus authoritative input digests.
7. Project that event into SQLite as an unaccepted candidate.

A missing or invalid expected manifest is an explicit ingestion state, not a
failed execution rewrite. A succeeded run is not a complete point. If the
ingestor stops at any boundary, scanning the retained run records, sync receipt,
and manifest retries the same source identity safely.

New project producers should emit this standard manifest directly. Legacy data
must enter through a named adapter that emits the same contract with
`producer.mode: "legacy_adapter"` and source artifact IDs/digests in provenance.
The adapter may map known data; it may not guess acceptance or silently infer
settings.

## 8. Results and Acceptance Semantics

### 8.1 Candidate eligibility

A result is eligible for acceptance only when all of the following are true:

- The result and manifest identities are unique and digest-valid.
- Every point, point revision, plan, and setting identity matches exactly.
- Every contribution matches a frozen binding.
- Required contributing runs succeeded authoritatively.
- Required outputs were synchronized and verified.
- Required metrics, observation thresholds, artifact roles, and checks pass.
- The result intent permits candidacy.

Eligibility never implies acceptance unless an explicit product policy enables
that transition.

### 8.2 Acceptance records

Result decisions are append-only events with their own immutable IDs. Accept,
reject, revoke, and supersede operations include:

- Exact `point_revision_id` and `result_id`.
- `expected_current_acceptance_id` for compare-and-swap.
- Action, actor, reason, and policy identifier.
- Optional `supersedes_acceptance_id`.
- Journal event ID and sequence.

SQLite maintains a rebuildable `accepted_result_head` for each point revision.
Only that explicit pointer supplies the official result. A timestamp, largest
run ID, last completed run, unique-looking artifact, or last ingested candidate
must never be used as a fallback.

Changing the active design to a new point revision immediately removes the old
revision's accepted result from current queries. The old result remains visible
only in explicit history/revision scope. Revoking an acceptance leaves the point
without an accepted result until another explicit acceptance occurs.

### 8.3 Derived point status

The registry should expose a primary status plus orthogonal counts/flags. For an
active point revision, use this precedence:

1. `complete`: an explicit accepted-result head exists and remains valid.
2. `running`: at least one exact current binding is dispatching, registered, or
   running.
3. `queued`: at least one exact current binding is queued.
4. `review`: at least one eligible unaccepted candidate exists.
5. `failed`: current bindings have terminal failures and no higher-precedence
   state exists.
6. `stale`: no current accepted result exists, but a prior point revision has an
   accepted result that cannot be reused.
7. `planned`: none of the above.

An omitted point is `archived` in the current design scope. Flags such as
`has_stale_history`, `candidate_count`, `active_run_count`, and
`failed_run_count` preserve information hidden by the primary precedence.

The `rerun` query returns exact active point revision IDs and reason codes. It
does not dispatch work.

## 9. Controller Storage and Recovery

### 9.1 Layout

Add this private tree beneath each existing controller project namespace:

```text
<controller.root>/projects/<project_id>/.remote-runner/experiments/
  journal/
    <sequence>-<event_id>.json
  registry.sqlite3
  backups/
  locks/
```

The directory and files use the same private permission posture as the current
controller registry. The database never enters project Git.

### 9.2 Immutable journal

The experiment journal is authoritative only for experiment-domain facts:

- Study creation and display-name/alias history.
- Design publication and active-revision changes.
- Canonical result ingestion after external validation.
- Acceptance, revocation, and supersession decisions.
- Explicit legacy imports and any minimal provenance tombstones required before
  purging referenced run records.

Each event is one immutable, bounded JSON envelope created through a temporary
file, fsync, and no-replace rename while holding the project experiment lock.
The `journal_sequence` is monotonic inside the project namespace and the event
carries a digest of the previous journal head. Full normalized plans and bounded
result manifests may be embedded because they are rebuild inputs; commands,
logs, and raw scientific payloads may not be embedded.

This avoids an unsafe YAML/SQLite dual write. Existing execution records remain
authoritative inputs. SQLite is updated after the journal commit and can always
be replayed.

### 9.3 SQLite projection

Use Python's built-in SQLite support with foreign keys enabled, a busy timeout,
WAL mode on the controller-local filesystem, and explicit transactions. The
logical schema should include at least:

| Table group | Purpose |
| --- | --- |
| `registry_meta`, `projection_inputs` | Schema/projector versions, registry epoch, source IDs/digests, and checkpoints |
| `registry_events` | Monotonic projection change sequence and bounded change metadata |
| `studies`, `study_names`, `study_aliases`, `study_heads` | Immutable study identity/history and explicit active-design pointer |
| `design_revisions`, `design_points` | Immutable normalized plan revisions and point membership |
| `points`, `point_names`, `point_aliases` | Immutable logical identities and presentation history |
| `point_revisions`, `point_components` | Immutable requirements/digests and exact component dependencies |
| `run_bindings`, `run_binding_targets`, `run_observations` | Frozen bindings plus projected queue/run/output state |
| `results`, `result_runs`, `result_metrics`, `result_artifacts` | Immutable candidates, many-to-many provenance, bounded metrics, and artifact references |
| `acceptances`, `accepted_result_heads` | Append-only decisions and explicit current pointers |
| `point_status_projection` | Rebuildable current status/counts optimized for dashboard queries |

Only `study_heads`, `accepted_result_heads`, checkpoints, and materialized status
rows are mutable projections. Historical domain rows are insert-only. The
canonical journal remains the source for names, plans, results, and acceptances.

Metric rows store bounded canonical scalar JSON and an optional indexed numeric
projection for graphing. Artifact rows store identities, relative paths,
digests, media types, and sizes only. No artifact content or per-shot payload is
stored.

### 9.4 Projector inputs and idempotency

The projector consumes:

- Experiment journal events by immutable event ID and digest.
- Queue/run binding documents by binding ID and digest.
- Run events by existing event ID, with manifest/state snapshots used to recover
  a missing event observation.
- Output-sync completed records by run ID and receipt digest.

Validated result manifests enter the projector inside their immutable
`result_ingested` journal event. They are not projected a second time as an
independent input. The journal event retains the original manifest ID/digest and
output-sync receipt digest for replay and audit.

Every input has a unique source key and content digest in `projection_inputs`.
Within one SQLite transaction the projector inserts the input marker, updates
derived rows, and appends bounded change events. Duplicate delivery is a no-op;
same identity with a different digest is a conflict that surfaces in registry
health.

Projector ordering must not create scientific selection semantics. The journal
sequence orders explicit experiment actions; exact IDs and pointers select
scientific state. Run event order only updates the observed lifecycle of an
exact run.

The query `change_sequence` is distinct from `journal_sequence`: it also covers
projected queue, run, and output-sync observations. It is rebuildable and paired
with `registry_epoch`, so rebuilding invalidates old query cursors without
changing any scientific identity or journal order.

### 9.5 Rebuild, migrations, backup, and export

- `remote-runner experiment registry rebuild` builds a new database beside the
  current one, replays all retained inputs, validates invariants and counts, and
  atomically replaces the projection only after success.
- A rebuild assigns a new `registry_epoch`; old opaque pagination/change cursors
  fail clearly instead of returning mixed data.
- SQLite schema migrations use an explicit application schema version and
  migration tests. A pre-migration SQLite online backup is retained.
- Export writes versioned canonical journal/event data and projection metadata,
  not an undocumented SQL dump as the only recovery format.
- Backup covers the immutable experiment journal, SQLite online backup, and
  schema/version metadata. Existing run records and output-sync receipts keep
  their existing backup/retention policy.
- Projector interruption, duplicate events, a missing SQLite file, and a
  partially applied migration all require automated recovery tests.

Existing purge operations need an integration rule before implementation: a run
referenced by an ingested or accepted result must retain a minimal immutable
provenance tombstone (run ID, binding IDs/digests, terminal status, and purge
policy) before its authoritative records are removed. It must not retain the
command or logs.

## 10. `experiment_query`

### 10.1 Request

```json
{
  "kind": "experiment_query",
  "schema_version": 1,
  "operation": "point_list",
  "study": {"study_id": "study-opaque-id"},
  "revision_scope": {"active": true},
  "filters": {
    "status": ["stale", "failed"],
    "dimensions": {
      "method": ["baseline"]
    },
    "canonical_key_prefix": null
  },
  "fields": [
    "point_id",
    "point_revision_id",
    "canonical_key",
    "dimensions",
    "status",
    "stale_reason"
  ],
  "changed_since": null,
  "page": {
    "limit": 50,
    "cursor": null
  }
}
```

Supported read operations should include:

- `study_list` and compact `study_status`.
- `dashboard` for one bounded point page plus the study rail in a single
  consistency-locked read.
- `point_list` with generic dimension and status filters.
- Exact `point_detail`.
- `point_history` and explicit design/point-revision scope.
- `plan_impact` for a published revision.
- `rerun_list` with reason codes.
- `acceptance_history`.

Plan preview/publication and acceptance are separate mutation requests that use
the plan/acceptance contracts and CAS fields. They share the same controller and
local API boundary but are not disguised as read queries.

### 10.2 Response and bounds

Every response contains:

- `schema_version`, project ID, registry epoch, and current event cursor.
- Explicit study and active-design revision IDs.
- Aggregate counts or a bounded `items` page.
- `next_cursor` and `has_more` when applicable.
- A stable machine-readable error code for invalid scope, cursor expiry,
  conflict, or unavailable projection.

Token and payload controls are server-enforced:

- Compact `study_status` returns active revision metadata and aggregate counts,
  not point or run arrays.
- List operations default to 50 records and cap at 500.
- `fields` is an allow-listed projection, not a SQL fragment.
- Generic dimension filters are validated against the published dimension
  catalog. Domain keys such as `code` or `method` work because they are plan
  dimensions, not core columns.
- Cursors are opaque keyset cursors bound to registry epoch, normalized query
  digest, revision scope, sort key, and last ID.
- `changed_since` is an opaque experiment event cursor, not a timestamp.
- Exact point detail is the only default response that includes metrics, stale
  reasons, candidate/acceptance summary, and compact run references.
- Commands, full manifests, prepared-server records, logs, and artifact bodies
  are never query fields.
- A hard serialized-response byte cap stops an unexpectedly wide request and
  returns a continuation cursor or a clear validation error.

Default ordering uses explicit plan order plus immutable IDs. Official results
and curves use only the accepted-result pointer for the selected exact point
revision.

### 10.3 CLI and controller surface

Proposed public CLI:

```text
remote-runner experiment plan preview
remote-runner experiment plan publish
remote-runner experiment studies list
remote-runner experiment study status
remote-runner experiment points list
remote-runner experiment point show
remote-runner experiment point history
remote-runner experiment impact show
remote-runner experiment reruns list
remote-runner experiment acceptance accept
remote-runner experiment acceptance reject
remote-runner experiment acceptance revoke
remote-runner experiment registry doctor
remote-runner experiment registry rebuild
remote-runner experiment registry export
```

JSON remains the stable automation format. Add `--format json|table` for
equivalent bounded human output; table mode may omit fields only when it says so
and supplies the same continuation information.

The local client should call explicit controller actions such as
`experiment-query`, `experiment-plan-preview`, and `experiment-plan-publish`
through the existing trusted SSH boundary. Neither clients nor the browser open
the SQLite file directly.

## 11. Web Experiments Dashboard

### 11.1 Navigation and transport

Add a top-level Runs/Experiments navigation choice within the existing Remote
Runner product header. Keep the Simplified Chinese default, current light
operational design, WCAG 2.2 AA target, and non-color status labels.

Experiment data should use dedicated local endpoints rather than joining the
entire periodic operational snapshot:

```text
POST /api/experiments/query
POST /api/experiments/plans/preview
POST /api/experiments/plans/publish
POST /api/experiments/acceptances
GET  /api/experiments/events?after=<cursor>
```

Only read endpoints are required for the first dashboard increment. Any enabled
write endpoint must preserve the same action headers, confirmation, explicit
identity, and revision/CAS behavior as existing controller writes.

### 11.2 Study list

The Experiments first view shows a dense study table with:

- Display name and subordinate canonical key/study ID.
- Explicit active design revision.
- Counts for complete, running, failed, stale, review, and planned points.
- Last projected event cursor and refresh/health state.
- Search and server-side status/dimension filters.

It must distinguish loading, empty, stale-projection, controller-unavailable,
and partial-ingestion states.

### 11.3 Study detail

Provide four views using on-demand bounded queries:

1. **Results**: active point revisions and their accepted metrics. Historical or
   stale values are excluded by default; an explicit history toggle may show
   them greyed with reason and revision identity.
2. **Curves**: chart definitions come from `presentation.curves`. X values,
   series, scale, formatting, and interval rendering are data-driven. Only
   current accepted results enter official curves or exports.
3. **Point Matrix**: rows, columns, and facets come from generic dimensions.
   Each stable cell shows text/icon status and the primary metric when accepted.
   An accessible tabular representation is always available.
4. **Point detail**: exact point/revision identity, requirement/component digest
   changes, accepted result, bounded candidate history, stale reason, compact
   contributing run IDs/statuses, and canonical artifact references.

The metric catalog and presentation metadata supply labels, formats, units,
intervals, axes, and series. No LER, decoder, code family, or other project term
is compiled into frontend logic.

The current full snapshot SSE may continue for Runs. Experiments should use an
event-cursor refresh signal followed by bounded queries, so a large cohort does
not stream its entire point matrix on each controller probe.

## 12. Implementation Increments

### Increment 0: Freeze decisions and fixtures

- Acceptance policy and Web write scope are resolved: automatic acceptance stays
  disabled, while explicit accept/reject decisions are enabled in the live view.
- Decide whether native producers are required, rather than merely encouraged,
  to emit `experiment_result`.
- Freeze canonical JSON/digest rules and ID formats.
- Add two contract fixture domains: one scientific sweep fixture and one
  deliberately non-quantum/non-decoder fixture.

Exit: reviewed contract examples and golden digests with no runtime behavior.

### Increment 1: Contracts and validation

- Add typed validation/normalization for plans, bindings, results, queries, IDs,
  metrics, dimensions, and artifact references.
- Add schema-version rejection and forward-compatibility tests.
- Extend submission/queue/run records to freeze a binding without making SQLite
  part of run registration.

Exit: exact binding bytes survive queue, dispatch, and manifest validation.

### Increment 2: Journal, SQLite, and projector

- Add project layout, private permissions, immutable journal writer, SQLite
  schema, migrations, and idempotent projector.
- Project existing run/state/events and output-sync completion receipts.
- Add health, doctor, backup/export, and build-beside/rebuild behavior.

Exit: duplicate, reordered, and interrupted input tests converge; deleting
SQLite and rebuilding produces the same logical IDs, heads, and result state.

### Increment 3: Plan preview and publication

- Implement exact impact computation and stable reason codes.
- Implement preview pagination and plan tamper checks.
- Implement journal-head CAS publication and retry idempotency.
- Add rename, alias, component-targeted staleness, archive, reactivation, and
  concurrent-publication tests.

Exit: unchanged/new/stale/archived classifications and active pointer satisfy
the plan acceptance criteria without dispatching work.

### Increment 4: Structured ingestion and acceptance

- Extend output sync to retrieve and verify the bounded standard result
  manifest from the canonical target.
- Implement multi-run/multi-point validation, candidate ingestion, retained
  immutable history, and explicit acceptance CAS.
- Integrate referenced-run purge tombstones.
- Add explicit legacy adapter API, with no automatic artifact discovery.

Exit: only a verified exact-setting accepted result can complete an active point;
duplicates, continuation, replacement, failure, and historical results behave
idempotently.

### Increment 5: Query API and CLI

- Implement server-side filters, fields, cursors, changed-since, byte caps, and
  compact defaults.
- Add JSON/table CLI commands and stable errors.
- Add controller protocol and migration compatibility tests.

Exit: a large fixture can retrieve summary, exact stale/running subsets, point
detail, history, and rerun lists without returning full run records.

### Increment 6: Read dashboard

- Add Runs/Experiments navigation and dedicated query transport.
- Implement study list, Results, Curves, Point Matrix, and point detail.
- Add accessible empty/loading/stale/error states and current-only exports.
- Verify no domain metric is hardcoded using both fixture domains.

Exit: desktop acceptance screenshots and interaction tests show generic progress,
metrics, intervals, curves, matrix, and exact detail; stale results never enter a
current curve.

### Increment 7: Optional write dashboard and hardening

- Add only the product-approved preview/publish/accept/rerun actions.
- Reuse explicit confirmation and CAS conflict handling.
- Document operator backup, migration, rebuild, and producer/adapter workflows.
- Run the repository's full Python, type, frontend, build, and distribution
  checks.

## 13. Verification Matrix

| Requirement | Required evidence |
| --- | --- |
| Atomic preview/publish | Golden impact fixtures, plan tamper test, stale-head race test, same-request retry test |
| Identity and targeted staleness | Rename/alias test and per-component dependency matrix |
| Many-to-many binding | Multi-point run, multi-run result, continuation, replacement, and duplicate input tests |
| Exact accepted result | Mismatched plan/setting/binding, unsynced artifact, insufficient observations, revoke, and no-fallback tests |
| Rebuildability | Kill-point tests around journal/SQLite commits and delete/rebuild equivalence |
| Token-bounded query | Large cohort, field allow-list, cursor/query mismatch, changed-since, and byte-cap tests |
| Generic dashboard | Two unrelated fixture domains, current-only curve/export, keyboard and non-color status checks |
| No hidden inference | Negative tests proving commands, stdout, tags, timestamps, and old artifacts cannot create identity or acceptance |

## 14. Risks and Mitigations

- **Canonical digest drift:** freeze one algorithm with cross-language golden
  vectors before persisted contracts ship.
- **Journal/projection divergence:** journal first, idempotent projector second;
  surface projection lag and let same-request retries finish catch-up.
- **Large plans or responses:** bounded documents, paginated impacts, allow-listed
  fields, keyset cursors, and serialized byte caps.
- **Archive path or symlink attacks:** target-side regular-file/path validation,
  normalized relative references, digest checks, and no arbitrary manifest path
  discovery.
- **False scientific currency:** explicit active and accepted pointers, exact
  point revisions, and no timestamp/result fallback.
- **Purged provenance:** retain only a minimal immutable experiment tombstone
  before deleting referenced run records.
- **Schema migration during active work:** backup, forward-only tested migrations,
  projection rebuild, and controller protocol compatibility gates.
- **Dashboard domain leakage:** presentation metadata and two unrelated fixture
  domains are release gates.

## 15. Resolved MVP Product Decisions

1. **Automatic acceptance:** disabled. Only an explicit acceptance event changes
   the accepted-result head and completes a point revision.
2. **Dashboard result decisions:** the live dashboard may accept or reject an
   eligible candidate using explicit IDs, a required reason, action-header
   confirmation, and compare-and-swap fields. Other mutation stays at the
   CLI/controller boundary.
3. **Native producer requirement:** new producers emit `experiment_result` and
   use verified output-sync ingestion. Historical imports must declare
   `producer.mode: "legacy_adapter"` and use an explicit adapter.

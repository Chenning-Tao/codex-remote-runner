# Submission Choices

## Source And Preparation

Remote Runner accepts only a clean committed Git revision. Omit `--source-repo` to use
`source.local_repo`, or provide another absolute clean worktree. Prepare once for a
cohort and reuse its validated manifest when several runs share the same revision and
eligible servers.

Historical queue extension (`add-server`, `sync-pool`, and the dashboard queue editor)
has one narrow fallback that ordinary submission does not have. When no override is
supplied and configured `source.local_repo` is dirty, it may choose a clean worktree
registered by that repository's Git common directory. The candidate must share the
exact common directory, be clean, and contain every requested queued commit object.
Selection is deterministic and reports the source path, clean HEAD, and verified
revisions. An explicit `--source-repo` never falls back. If no candidate passes every
check, preparation fails closed without changing queue eligibility. This flow never
stashes, commits, resets, copies, or submits uncommitted file contents.

## Detached Submission

`run` is detached by default. A successful call returns the exact run ID and queue
record, then exits. Add `--wait` only for an explicitly requested foreground wait.
Submitting several runs should not serialize them; submit the cohort, retain every run
ID, and query or explicitly wait for those IDs later.

The controller does not poll a model. Status calls are immediate controller queries;
explicit waits use etag-based controller long polling inside one CLI process.

## Resources And Placement

Use `--min-cores N` for a generic resource constraint. Use repeated
`--candidate-server NAME` for an explicit eligible-server set. The explicit set is
stored on the queue record and remains user-editable while the run is queued.

The controller selects only from prepared eligible servers using current generic
capacity, configured slots, drains, workload class, priority, and stable name ordering.
It does not inspect workload arguments, experiment points, metrics, or result meaning.

`--server NAME` pins one user-required server. `--server all` keeps a queued automatic
pool extensible through `sync-pool`; `add-server` appends one prepared server to one
exact queued run. Test workloads use the configured test pool and its independent slot
capacity. Pool synchronization verifies every queued historical revision in the
selected clean local source before reusing it for a cohort, reports its selection, and
never removes existing candidates when preparation fails.

## Command And Environment

The submitted shell command is stored and executed unchanged. Remote Runner never
appends `--num-workers` or rewrites another workload argument. The workload receives:

- `RR_PROJECT_PYTHON`: configured project interpreter;
- `RR_ASSIGNED_CORES`: positive core allocation for the selected server;
- `RR_OUTPUT_ROOT`: selected output root for relative-output runs;
- `RR_OUTPUT_PATH`: exact resolved output path;
- `RR_OUTPUT_DIR`: parent directory of the resolved output path.

The workload owns all domain interpretation and parallelism choices.

## Output Identity

Prefer normalized relative POSIX `--output-relpath`. Every eligible server must define
an `output_root`; the controller resolves the physical path only after placement.
Output metadata is opaque JSON. Remote Runner does not infer a result, verdict, or
scientific identity from paths, metadata, commands, labels, stdout, or timestamps.

## Queue Controls

Priority and workload class are independent. Web and controller queue writes use the
queue revision, can replace the explicit eligible-server set, and reject stale changes
or runs that have entered dispatch. Batch updates preserve each run's independent
revision guard and report partial failures.

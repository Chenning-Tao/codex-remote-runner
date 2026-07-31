# Web Dashboard Design Contract

The web dashboard is an operational surface, not a marketing page. It follows
these external standards and design-system references:

- [WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/) for contrast, focus, target size,
  reflow, and non-color status cues.
- [WAI-ARIA Authoring Practices](https://www.w3.org/WAI/ARIA/apg/patterns/) for
  tabs, dialogs, tooltips, and keyboard interaction.
- [Carbon data tables](https://carbondesignsystem.com/components/data-table/usage/)
  for dense scanning, column hierarchy, and predictable row behavior.
- [GitHub Primer](https://primer.style/) for neutral application surfaces,
  control sizing, focus treatment, and restrained status color.
- [Vercel Web Interface Guidelines](https://github.com/vercel-labs/web-interface-guidelines)
  as the implementation review checklist.

## Product Rules

- The default interface language is Simplified Chinese. Product names, project
  IDs, run IDs, task IDs, server names, and user-authored workload labels remain
  untranslated.
- The first release is single-project and loopback-only. Controller write
  actions are limited to stopping one exact run and modifying selected runs
  while each queue state is `queued`.
- Top-level Runs and Experiments navigation is always available. The live
  Experiments view may accept or reject an eligible candidate after showing its
  evidence and requiring a reason. Plan publication stays at the CLI/controller
  boundary, and the synthetic experiment snapshot stays read-only.
- Live Experiments views never fall back to synthetic data. The synthetic demo
  is available only through its explicit demo URL.
- The first viewport shows the product identity, controller health, capacity,
  active work, and queued work without marketing content.
- Research and scheduling identifiers remain visible but visually subordinate to
  human-readable labels.
- A stale or failed probe preserves the last successful snapshot and states its
  age. Loading, empty, stale, disconnected, and partial-error states are distinct.
- Status is never communicated by color alone. Every state includes text and an
  icon or shape.

## Visual Rules

- The dashboard uses a light macOS application vocabulary: Apple system fonts,
  `#f5f5f7` page chrome, white grouped surfaces, `#1d1d1f` primary text, and
  `#0071e3` for interactive emphasis.
- Keep product identity, controller status, and refresh controls in the page
  header rather than reserving a separate persistent masthead.
- Use familiar product-UI patterns. PatternFly may provide interaction
  primitives, but it is not the visual authority.
- Use an 8px spacing grid, with 4px only for compact inline relationships.
- Use 6px or 8px corner radii. Do not use decorative cards, gradients, heavy
  shadows, illustrations, or oversized type.
- Default data rows are 44px high. Controls have stable dimensions and do not
  move when values update.
- Dense table body text is at least 14px and supporting text is at least 13px.
  Text smaller than 18px maintains at least 4.5:1 contrast against its surface.
- Use neutral surfaces with one blue interaction color; reserve green, amber,
  and red for semantic state.
- Use system sans-serif text and monospace only for run IDs, revisions, hosts,
  durations, and numeric telemetry.

## Responsive Rules

- Desktop shows the server/task workspace and queue side by side.
- Narrow screens switch between Servers and Queue with tabs and retain a
  horizontally scrollable table rather than removing authoritative fields.
- Details open in a side drawer on desktop and a full-width panel on narrow
  screens. The drawer is fixed to the viewport, scrolls internally, and overlays
  the workspace without changing the table layout.
- Experiment results, curves, and matrix axes are driven by the published metric,
  dimension, and presentation catalogs. Current curves include only explicitly
  accepted results for the active point revision.
- The queue-control increment is desktop-first. Mobile and narrow-screen layout
  for manual ordering, priority editing, and server selection has no acceptance
  criteria in this increment and must not block desktop delivery.

## Interaction Rules

- Search and filters affect only the browser view, never scheduler order.
- Experiment result decisions require an exact candidate ID, explicit action,
  confirmation reason, and controller compare-and-swap validation.
- Queue pagination operates on the complete active queue, uses 20 rows per page,
  and keeps the selected page in the URL.
- Rows are keyboard reachable and open the same detail surface as a
  pointer click.
- Stop requires an explicit in-panel confirmation, disables controls while the
  request is pending, and never assumes success before the controller responds.
- Server details expose controller-wide pause and resume actions. Pausing uses
  an inline confirmation that states existing work continues; both actions
  disable conflicting controls and refresh authoritative drain state before the
  panel changes status.
- Transient success notices may dismiss automatically after a readable delay and
  remain manually dismissible. Partial-failure and failure notices persist until
  the user closes them or a later action replaces them.
- Live updates use server-sent events. Reconnection is automatic and announced
  without blocking use of the last snapshot.
- Motion respects `prefers-reduced-motion`.

## Queue Control Rules

- Only records whose authoritative queue state is `queued` are editable. A task
  in `dispatching` or any terminal state rejects ordering, priority, and server
  changes.
- Every mutation identifies one exact run and includes its last observed queue
  revision. A revision conflict rejects the edit and refreshes the controller
  snapshot; the browser never merges a stale edit speculatively.
- Manual ordering uses explicit up/down controls in the queue table. One action
  moves the task by one position within its real scheduling lane, defined by the
  pair `(workload_class, queue_priority)`. Controls are disabled at lane
  boundaries and while a request is pending.
- Priority is either `urgent` or `normal`. Urgent work remains ahead of normal
  work. Changing priority places the task at the tail of the destination
  priority segment within its workload lane; users may then refine that order
  with the manual controls.
- The detail drawer edits priority and eligible servers as one save operation.
  At least one server must remain eligible.
- Queue checkboxes support selection across filtering and pagination. Batch
  editing can independently replace workload class, priority, and eligible
  servers; every unselected setting remains unchanged. A replacement server set
  offers only servers compatible with all selected tasks and the target class.
- A batch write preserves each task's independent revision guard and preparation
  lifecycle. Partial completion keeps failed tasks selected and reports their
  controller errors instead of presenting the batch as atomic.
- A pending batch keeps its original task set and draft scheduling settings stable
  across live snapshots. Identical in-flight submissions share one backend update
  so preparation reservations cannot race each other.
- Pending preparation keeps the submitted task total stable and reports completed
  preparation work against its initial total. Once preparation reaches that total,
  the status distinguishes final queue application from additional preparation.
- Server selection lists the task's prepared servers plus compatible configured
  project servers. Compatibility respects project enablement, minimum cores,
  testing-pool membership, testing slots, and portable-output requirements.
- Saving an unprepared server acquires a bounded controller reservation, pauses
  dispatch of that exact queued run, prepares its exact submitted revision by
  reusing the `add-server` pipeline, and enables it only after every requested
  preparation succeeds. The UI identifies unprepared choices and reports the
  preparation state while the request is pending.
- Historical preparation honors the Web process's explicit clean `--source-repo`.
  Without an override, a dirty configured source may use only a clean linked
  worktree registered to the same Git common directory. The backend verifies the
  exact queued revision in that source and returns an auditable source-preparation
  record; absence of a trusted source fails the edit closed.
- A preparation failure releases the reservation and preserves the prior
  priority and eligible-server selection. Successfully prepared descriptors may
  remain available for a later retry, but are not silently enabled by the failed
  edit. An abandoned reservation expires so dispatch cannot remain blocked
  indefinitely.
- Server preparation cannot bypass workload/core/output constraints or change
  the submitted revision.
- A `server_scope: all` task continues to gain newly prepared servers through
  pool synchronization until a user explicitly saves a server selection. After
  that first manual selection, later pool extensions preserve the user's chosen
  eligible set while retaining all prepared servers as available choices.
- A successful mutation refreshes the controller snapshot before the UI reports
  completion. Failure states distinguish stale revision, no-longer-editable
  work, missing work, and invalid server/priority input.
- Search, priority filters, pagination, and mobile/desktop view selection remain
  browser-only presentation state. They never change the target or meaning of a
  queue mutation.

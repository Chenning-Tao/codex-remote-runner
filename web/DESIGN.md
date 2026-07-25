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
- The first release is single-project and loopback-only. Stopping one exact run
  is its only controller write action.
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

## Interaction Rules

- Search and filters affect only the browser view, never scheduler order.
- Queue pagination operates on the complete active queue, uses 20 rows per page,
  and keeps the selected page in the URL.
- Rows are keyboard reachable and open the same detail surface as a
  pointer click.
- Stop requires an explicit in-panel confirmation, disables controls while the
  request is pending, and never assumes success before the controller responds.
- Live updates use server-sent events. Reconnection is automatic and announced
  without blocking use of the last snapshot.
- Motion respects `prefers-reduced-motion`.

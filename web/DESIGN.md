# Web Dashboard Design Contract

The web dashboard is an operational surface, not a marketing page. It follows
these external standards and design-system references:

- [WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/) for contrast, focus, target size,
  reflow, and non-color status cues.
- [WAI-ARIA Authoring Practices](https://www.w3.org/WAI/ARIA/apg/patterns/) for
  tabs, dialogs, tooltips, and keyboard interaction.
- [PatternFly](https://www.patternfly.org/) for infrastructure-console layout,
  toolbar, table, status, progress, empty-state, and drawer patterns.
- [Carbon data tables](https://carbondesignsystem.com/components/data-table/usage/)
  as a secondary reference for dense scanning and column hierarchy.

## Product Rules

- The first release is read-only, single-project, and loopback-only.
- The first viewport shows the product identity, controller health, capacity,
  active work, and queued work without marketing content.
- Research and scheduling identifiers remain visible but visually subordinate to
  human-readable labels.
- A stale or failed probe preserves the last successful snapshot and states its
  age. Loading, empty, stale, disconnected, and partial-error states are distinct.
- Status is never communicated by color alone. Every state includes text and an
  icon or shape.

## Visual Rules

- Use PatternFly tokens and components before adding local CSS.
- Use an 8px spacing grid, with 4px only for compact inline relationships.
- Use 4px or 6px corner radii. Do not use decorative cards, gradients, shadows,
  illustrations, or oversized type.
- Default data rows are 44px high. Controls have stable dimensions and do not
  move when values update.
- Dense table body text is at least 13px and supporting text is at least 10px.
  Text smaller than 18px maintains at least 4.5:1 contrast against its surface.
- Use neutral surfaces with green, amber, red, and cyan reserved for state.
- Use system sans-serif text and monospace only for run IDs, revisions, hosts,
  durations, and numeric telemetry.

## Responsive Rules

- Desktop shows server capacity and queue tables in one vertical workspace.
- Narrow screens switch between Servers and Queue with tabs and retain a
  horizontally scrollable table rather than removing authoritative fields.
- Details open in a side drawer on desktop and a full-width panel on narrow
  screens.

## Interaction Rules

- Search and filters affect only the browser view, never scheduler order.
- Rows are keyboard reachable and open the same read-only detail surface as a
  pointer click.
- Live updates use server-sent events. Reconnection is automatic and announced
  without blocking use of the last snapshot.
- Motion respects `prefers-reduced-motion`.

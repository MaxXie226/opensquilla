# Approval Card Visual Refresh

## Goal

Make the in-chat approval card feel like a calm, system-level confirmation while preserving the clarity required for destructive actions. The card should be visually lighter, faster to scan, and consistent with the existing OpenSquilla theme in light and dark modes.

## Scope

The change is limited to `opensquilla-webui/src/components/chat/ApprovalCard.vue` and its component tests. Approval data, wording, decisions, backup behavior, timers, accessibility announcements, and RPC behavior remain unchanged.

## Design direction

Use a lightweight system-confirmation treatment with `DESIGN_VARIANCE: 4`, `MOTION_INTENSITY: 2`, and `VISUAL_DENSITY: 3`.

- Replace the warning-colored outer border with a neutral hairline and a restrained layered shadow.
- Reduce the header icon and spacing while keeping the eyebrow, semantic title, and shield recognizable.
- Present the target path as a compact neutral code surface so it is clearly associated with its label.
- Turn backup and irreversible notices into compact semantic status rows. Use color as a supporting cue, not a large tinted background.
- Keep the footer attached to the card but visually quiet. Align actions to the right on desktop and stack them on narrow screens.
- Keep the primary action in the existing accent color. Render refusal as a neutral secondary action with danger emphasis on interaction rather than a large permanent red treatment.
- Preserve the existing radius and token system. Add no dependency, gradient, glass effect, or decorative animation.

## Information hierarchy

1. Approval state and action title.
2. Exact user-facing target or command.
3. Backup or irreversible-operation consequence.
4. Decision actions and optional countdown.

The target and consequence must remain readable without scrolling for the common one-target case. Long commands and detailed warnings may still use the existing bounded, scrollable body.

## States

- Standard approval: neutral card and compact warning accent.
- Destructive approval with backup: quiet positive backup indicator while the title retains the destructive action wording.
- Destructive approval without backup or with unavailable backup: compact danger indicator and explicit irreversible wording.
- Busy: existing disabled-button behavior remains.
- Error: existing inline alert remains below the actions.
- Resolved timeline outcome: unchanged except for any token consistency needed to match the refreshed card.

## Accessibility and responsive behavior

- Preserve the `role`, `aria-label`, assertive live announcement, focus target, and button semantics.
- Maintain a visible keyboard focus ring and WCAG-readable text/button contrast in both themes.
- Do not rely on color alone to distinguish backup and irreversible states; their text remains explicit.
- At widths up to 768px, actions remain full-width and stacked. Header, target, and notice spacing becomes tighter without truncating the action title or path.
- Respect reduced motion by introducing no new animation.

## Verification

- Extend component tests to assert the structural classes and preserved approval behavior.
- Run the focused approval-card tests, Web typecheck, and the full Web test suite.
- Render a real destructive approval in the packaged macOS app and inspect desktop and narrow viewport screenshots in light and dark themes.
- Confirm primary, refusal, busy, backup-enabled, backup-disabled, unavailable-backup, and resolved states remain understandable.

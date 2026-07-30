import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'
import chatViewSource from './ChatView.vue?raw'

const chatViewStyles = readFileSync(
  new URL('../styles/chat-view.css', import.meta.url),
  'utf8',
)

describe('compaction progress presentation', () => {
  it('uses an indeterminate operation state instead of a fake occupancy percentage', () => {
    expect(chatViewSource).toContain('role="progressbar"')
    expect(chatViewSource).toContain(
      `:aria-valuenow="compactStatus.status === 'completed' ? 100 : undefined"`,
    )
    expect(chatViewSource).toContain(
      `'chat-compact-status__gauge-fill--indeterminate': compactStatus.isBusy`,
    )
    expect(chatViewSource).toContain(
      `'chat-compact-status__indicator--busy': compactStatus.isBusy`,
    )
    expect(chatViewSource).not.toContain(
      `:style="compactStatus.occupancyPercent !== null`,
    )
    expect(chatViewStyles).toContain('.chat-compact-status__gauge-fill--indeterminate')
    expect(chatViewStyles).toContain('animation: compactGaugeIndeterminate 1.35s linear infinite')
    expect(chatViewStyles).not.toContain('width: 60%')
  })

  it('keeps the status compact, fills at completion, and preserves reduced motion', () => {
    expect(chatViewStyles).toMatch(
      /\.chat-compact-status\s*\{[^}]*width:\s*fit-content[^}]*border-radius:\s*var\(--radius-pill\)/s,
    )
    expect(chatViewStyles).toMatch(
      /\.chat-compact-status__gauge\s*\{[^}]*height:\s*2px/s,
    )
    expect(chatViewStyles).toMatch(
      /\.chat-compact-status__gauge-fill--done\s*\{[^}]*width:\s*100%/s,
    )
    expect(chatViewStyles).toMatch(
      /prefers-reduced-motion:\s*reduce[\s\S]*gauge-fill--indeterminate[\s\S]*animation:\s*none/,
    )
    expect(chatViewStyles).toMatch(
      /prefers-reduced-motion:\s*reduce[\s\S]*gauge-fill--indeterminate\s*\{[^}]*width:\s*100%[^}]*transform:\s*translateX\(0\)/,
    )
  })
})

import { describe, expect, it } from 'vitest'

import { effectiveComposerRunMode } from './composerRunMode'

describe('effectiveComposerRunMode', () => {
  it.each(['not_setup', 'setting_up', 'failed', 'unavailable'] as const)(
    'soft-lands a stale Safe preference in Full Access while setup is %s',
    (state) => {
      expect(effectiveComposerRunMode(
        'safe',
        { state, platform: 'win32', message: '', requiresAdmin: true },
        null,
      )).toBe('full')
    },
  )

  it('keeps Safe when setup is ready', () => {
    expect(effectiveComposerRunMode(
      'safe',
      { state: 'ready', platform: 'win32', message: '', requiresAdmin: false },
      null,
    )).toBe('safe')
  })

  it('does not invent failure while setup status is unknown', () => {
    expect(effectiveComposerRunMode('safe', null, null)).toBe('safe')
  })

  it('preserves an active task lock even if setup status changes', () => {
    expect(effectiveComposerRunMode(
      'full',
      { state: 'not_setup', platform: 'win32', message: '', requiresAdmin: true },
      'safe',
    )).toBe('safe')
  })
})

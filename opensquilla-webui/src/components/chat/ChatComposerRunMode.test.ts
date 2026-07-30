// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import ChatComposerRunMode from './ChatComposerRunMode.vue'

let unmount: (() => void) | null = null

function mount(allowedRunModes: Array<'safe' | 'full'>) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposerRunMode, {
    runMode: 'full',
    allowedRunModes,
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        chat: {
          closeComposerSettings: 'Close',
          composer: {
            runMode: 'Run mode',
            runModeSafe: 'Safe',
            runModeSafeDesc: 'Sandboxed',
            runModeFull: 'Full access',
            runModeFullDesc: 'Host access',
          },
        },
      },
    },
  }))
  app.mount(el)
  unmount = () => app.unmount()
  return el
}

afterEach(() => {
  unmount?.()
  unmount = null
  document.body.innerHTML = ''
})

describe('ChatComposerRunMode', () => {
  it('renders exactly Safe and Full', () => {
    const el = mount(['safe', 'full'])
    const radios = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')]
    expect(radios).toHaveLength(2)
    expect(radios.map(radio => radio.textContent?.trim())).toEqual([
      'SafeSandboxed',
      'Full accessHost access',
    ])
  })

  it('quietly disables Safe when the capability is unavailable', () => {
    const el = mount(['full'])
    const radios = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')]
    expect(radios[0].disabled).toBe(true)
    expect(el.querySelector('.composer-run-mode__hint')).toBeNull()
  })
})

// @vitest-environment happy-dom
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'

import i18n from '@/i18n'
import { useRpcStore } from '@/stores/rpc'
import PromptCacheKeepaliveDialog from './PromptCacheKeepaliveDialog.vue'

async function settle() {
  await Promise.resolve()
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
  vi.restoreAllMocks()
})

describe('PromptCacheKeepaliveDialog', () => {
  it('replaces a missing-session transport error with actionable copy', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const rpc = useRpcStore(pinia)
    vi.spyOn(rpc, 'call').mockRejectedValue(new Error('Session not found'))

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:draft',
    })
    app.use(pinia)
    app.use(i18n)
    app.mount(host)
    await settle()

    const error = document.body.querySelector<HTMLElement>('[role="alert"]')
    expect(error?.textContent).toContain('This session is not available yet')
    expect(document.body.textContent).not.toContain('Session not found')

    app.unmount()
  })
})

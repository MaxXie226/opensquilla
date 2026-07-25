// @vitest-environment happy-dom

import { createApp, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'
import en from '@/locales/en.json'
import type { ArtifactPayload } from '@/types/rpc'

function artifact(overrides: Partial<ArtifactPayload> = {}): ArtifactPayload {
  return {
    id: 'artifact-1',
    name: 'page.html',
    mime: 'text/html',
    download_url: '/api/v1/artifacts/artifact-1',
    ...overrides,
  }
}

async function settlePreview() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mountPanel(
  props: Record<string, unknown>,
): { element: HTMLElement; unmount: () => void } {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp(ArtifactPreviewPanel, props)
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: { en },
  }))
  app.mount(element)
  return {
    element,
    unmount: () => {
      app.unmount()
      element.remove()
    },
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ArtifactPreviewPanel', () => {
  it('renders web HTML in a script-only opaque sandbox', async () => {
    const observed: { blob?: Blob } = {}
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => {
      observed.blob = blob as Blob
      return 'about:blank#artifact-preview'
    })
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<html><body><script>document.body.textContent = "ready"</script></body></html>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))

    const mounted = mountPanel({ artifact: artifact() })
    await settlePreview()

    const frame = mounted.element.querySelector<HTMLIFrameElement>('.artifact-preview__frame--html')
    expect(frame).not.toBeNull()
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-same-origin')
    expect(frame?.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(createObjectUrl).toHaveBeenCalledOnce()
    expect(await observed.blob?.text()).toContain("connect-src 'none'")

    mounted.unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('about:blank#artifact-preview')
  })

  it('can omit its header when embedded in the workbench chrome', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      'plain text',
      { status: 200, headers: { 'Content-Type': 'text/plain' } },
    )))

    const mounted = mountPanel({
      artifact: artifact({ name: 'notes.txt', mime: 'text/plain' }),
      showHeader: false,
    })
    await settlePreview()

    expect(mounted.element.querySelector('.artifact-preview__toolbar')).toBeNull()
    expect(mounted.element.querySelector('.artifact-preview__text')?.textContent).toBe('plain text')
    mounted.unmount()
  })

  it('emits external-open and download intents without performing them', async () => {
    const onExternalOpen = vi.fn()
    const onDownload = vi.fn()
    const item = artifact({
      name: 'archive.zip',
      mime: 'application/zip',
    })
    const mounted = mountPanel({
      artifact: item,
      onDownload,
      onExternalOpen,
    })
    await settlePreview()

    const actions = [...mounted.element.querySelectorAll<HTMLButtonElement>(
      '.artifact-preview__actions button',
    )]
    expect(actions).toHaveLength(2)
    actions[0]?.click()
    actions[1]?.click()

    expect(onExternalOpen).toHaveBeenCalledWith(item)
    expect(onDownload).toHaveBeenCalledWith(item)
    mounted.unmount()
  })

  it('explains an artifact integrity failure instead of showing a generic error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 'INTEGRITY_ERROR',
      error: 'checksum mismatch',
    }), {
      status: 409,
      headers: { 'Content-Type': 'application/json' },
    })))

    const mounted = mountPanel({ artifact: artifact() })
    await settlePreview()

    expect(mounted.element.textContent).toContain('Artifact integrity check failed')
    expect(mounted.element.textContent).toContain('no longer matches its recorded checksum')
    mounted.unmount()
  })
})

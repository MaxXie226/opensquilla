// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { useVoiceInput } from './useVoiceInput'

// useToasts is a module-level singleton, so this is the same queue the
// composable writes to.
const { toasts } = useToasts()
const originalVisibilityState = Object.getOwnPropertyDescriptor(document, 'visibilityState')

// Minimal MediaRecorder stand-in: stop() synchronously emits one chunk and
// fires onstop, which is what drives useVoiceInput into transcribeChunks().
class FakeMediaRecorder {
  state: 'inactive' | 'recording' = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(public stream: unknown) {}
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
    this.onstop?.()
  }
}

class AsyncStopMediaRecorder extends FakeMediaRecorder {
  override stop() {
    this.state = 'inactive'
    setTimeout(() => {
      this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
      this.onstop?.()
    }, 0)
  }
}

function stubMedia(recorderClass: typeof FakeMediaRecorder = FakeMediaRecorder) {
  const stopTrack = vi.fn()
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] }))
  ;(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = recorderClass
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  return { getUserMedia, stopTrack }
}

function stubWindowHiddenBridge() {
  let onHidden: (() => void) | null = null
  const unsubscribe = vi.fn()
  const onWindowHidden = vi.fn((callback: () => void) => {
    onHidden = callback
    return unsubscribe
  })
  ;(window as unknown as {
    opensquillaDesktop?: { onWindowHidden: typeof onWindowHidden }
  }).opensquillaDesktop = { onWindowHidden }
  return {
    hideWindow: () => onHidden?.(),
    onWindowHidden,
    unsubscribe,
  }
}

async function waitUntil(condition: () => boolean, tries = 40) {
  for (let i = 0; i < tries; i++) {
    if (condition()) return
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

// Start then stop a recording, then wait for the async transcription to settle
// (a toast surfaced or the transcript delivered).
async function runTranscription() {
  const onText = vi.fn()
  const { cleanup, toggleVoiceInput, voiceBusy } = useVoiceInput()
  await toggleVoiceInput(onText)
  await toggleVoiceInput(onText)
  await waitUntil(
    () => !voiceBusy.value && (toasts.value.length > 0 || onText.mock.calls.length > 0),
  )
  cleanup()
  return onText
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  toasts.value = []
  delete (window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop
  stubMedia()
})

afterEach(() => {
  delete (window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop
  if (originalVisibilityState) {
    Object.defineProperty(document, 'visibilityState', originalVisibilityState)
  } else {
    delete (document as unknown as { visibilityState?: DocumentVisibilityState }).visibilityState
  }
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useVoiceInput transcription feedback', () => {
  it('surfaces an "enable in settings" toast when transcription is unavailable (503)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ error: 'audio transcription is disabled', code: 'UNAVAILABLE' }),
      })),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(i18n.global.t('chat.toast.voiceUnavailable'))
    expect(toasts.value.every(t => t.tone === 'danger')).toBe(true)
  })

  it('surfaces a generic failure toast on a provider error (502)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        json: async () => ({ error: 'ELEVENLABS_API_KEY is not set', code: 'PROVIDER_ERROR' }),
      })),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(
      i18n.global.t('chat.toast.voiceTranscribeFailed'),
    )
  })

  it('surfaces a generic failure toast when the request throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down')
      }),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(
      i18n.global.t('chat.toast.voiceTranscribeFailed'),
    )
  })

  it('inserts the transcript and raises no error toast on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ text: '  hello world  ' }),
      })),
    )

    const onText = await runTranscription()

    expect(onText).toHaveBeenCalledWith('hello world')
    expect(toasts.value).toHaveLength(0)
  })
})

describe('useVoiceInput hidden-window cancellation', () => {
  it('does not strand an old stream when stop dispatches asynchronously', async () => {
    const fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ text: 'first recording' }),
    }))
    vi.stubGlobal('fetch', fetch)
    const { getUserMedia, stopTrack } = stubMedia(AsyncStopMediaRecorder)
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceBusy, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    await toggleVoiceInput(onText)
    expect(voiceBusy.value).toBe(true)
    expect(stopTrack).toHaveBeenCalledTimes(1)

    // A rapid restart is ignored until the old onstop/transcription finishes,
    // so its stream cannot be replaced before it is released.
    await toggleVoiceInput(onText)
    expect(getUserMedia).toHaveBeenCalledTimes(1)

    await waitUntil(() => !voiceBusy.value && onText.mock.calls.length === 1)
    await toggleVoiceInput(onText)
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(voiceRecording.value).toBe(true)

    cleanup()
    expect(stopTrack).toHaveBeenCalledTimes(2)
  })

  it('cancels each recording when the desktop window is hidden without transcribing', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { stopTrack } = stubMedia()
    const bridge = stubWindowHiddenBridge()
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    expect(voiceRecording.value).toBe(true)
    bridge.hideWindow()

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()

    await toggleVoiceInput(onText)
    expect(voiceRecording.value).toBe(true)
    bridge.hideWindow()

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(2)
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
    expect(bridge.onWindowHidden).toHaveBeenCalledTimes(1)

    cleanup()
    expect(bridge.unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('keeps visibilitychange hidden as a browser fallback', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { stopTrack } = stubMedia()
    let visibilityState: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibilityState,
    })
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    visibilityState = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()

    cleanup()
  })

  it('cleanup unsubscribes and stops an active stream without transcribing', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { stopTrack } = stubMedia()
    const bridge = stubWindowHiddenBridge()
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    cleanup()
    bridge.hideWindow()

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(bridge.unsubscribe).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
  })
})

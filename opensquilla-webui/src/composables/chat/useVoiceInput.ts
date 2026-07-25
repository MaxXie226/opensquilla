import { ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'

interface TranscriptionResponse {
  text?: string
  error?: string
  code?: string
}

interface DesktopWindowVisibilityBridge {
  onWindowHidden?: (callback: () => void) => void | (() => void)
}

function authToken(): string {
  try {
    return sessionStorage.getItem('opensquilla.wsToken') || ''
  } catch {
    return ''
  }
}

export function useVoiceInput() {
  const { pushToast } = useToasts()
  const voiceBusy = ref(false)
  const voiceRecording = ref(false)
  let recorder: MediaRecorder | null = null
  let activeStream: MediaStream | null = null
  let chunks: BlobPart[] = []
  let recordingGeneration = 0
  let transcriptionController: AbortController | null = null
  let unsubscribeWindowHidden: (() => void) | null = null
  let cleanedUp = false

  async function toggleVoiceInput(onText: (text: string) => void) {
    if (voiceRecording.value) {
      stopRecording()
      return
    }
    await startRecording(onText)
  }

  async function startRecording(onText: (text: string) => void) {
    if (cleanedUp || voiceBusy.value || voiceRecording.value) return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      pushToast(i18n.global.t('chat.toast.voiceUnsupported'), { tone: 'danger' })
      return
    }
    const generation = ++recordingGeneration
    voiceBusy.value = true
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (cleanedUp || generation !== recordingGeneration) {
        stream.getTracks().forEach(track => track.stop())
        return
      }
      activeStream = stream
      chunks = []
      const mediaRecorder = new MediaRecorder(stream)
      recorder = mediaRecorder
      mediaRecorder.ondataavailable = event => {
        if (generation !== recordingGeneration || recorder !== mediaRecorder) return
        if (event.data && event.data.size > 0) chunks.push(event.data)
      }
      mediaRecorder.onstop = () => {
        if (generation !== recordingGeneration || recorder !== mediaRecorder) return
        const mime = mediaRecorder.mimeType || 'audio/webm'
        void transcribeChunks(mime, onText, generation)
      }
      mediaRecorder.start()
      voiceRecording.value = true
    } catch (err) {
      if (generation !== recordingGeneration || cleanedUp) return
      console.warn('Voice recording failed:', err instanceof Error ? err.message : String(err))
      stopTracks()
    } finally {
      if (generation === recordingGeneration) voiceBusy.value = false
    }
  }

  function stopRecording() {
    const mediaRecorder = recorder
    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      voiceRecording.value = false
      stopTracks()
      return
    }
    voiceRecording.value = false
    // MediaRecorder.stop() delivers dataavailable/onstop asynchronously in
    // browsers. Keep the input busy until that recording has handed its chunks
    // to transcription, otherwise a rapid second click can replace
    // activeStream and strand the old microphone track.
    voiceBusy.value = true
    try {
      mediaRecorder.stop()
    } catch (err) {
      if (recorder === mediaRecorder) recorder = null
      chunks = []
      voiceBusy.value = false
      console.warn('Voice recording stop failed:', err instanceof Error ? err.message : String(err))
    } finally {
      // Releasing the device does not discard the recorder's buffered final
      // data; its queued dataavailable event still owns the Blob chunks.
      stopTracks()
    }
  }

  async function transcribeChunks(
    mime: string,
    onText: (text: string) => void,
    generation: number,
  ) {
    if (generation !== recordingGeneration || cleanedUp) return
    const payload = new Blob(chunks, { type: mime })
    chunks = []
    stopTracks()
    recorder = null
    if (!payload.size) {
      if (generation === recordingGeneration) voiceBusy.value = false
      return
    }

    const controller = new AbortController()
    transcriptionController = controller
    voiceBusy.value = true
    try {
      const form = new FormData()
      form.append('file', payload, 'voice.webm')
      form.append('mime', mime)
      const headers: Record<string, string> = {}
      const token = authToken()
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetch('/api/audio/transcribe', {
        method: 'POST',
        headers,
        body: form,
        credentials: 'same-origin',
        signal: controller.signal,
      })
      const data = (await response.json().catch(() => ({}))) as TranscriptionResponse
      if (generation !== recordingGeneration || cleanedUp) return
      if (!response.ok) {
        // A 503/UNAVAILABLE means voice transcription isn't configured on the
        // backend (audio disabled or no ElevenLabs key). The mic button is
        // normally gated on readiness, so this is a race/stale-status backstop:
        // surface a visible, actionable toast instead of failing silently.
        const unavailable = response.status === 503 || data.code === 'UNAVAILABLE'
        console.warn('Voice transcription failed:', data.error || `HTTP ${response.status}`)
        pushToast(
          i18n.global.t(unavailable ? 'chat.toast.voiceUnavailable' : 'chat.toast.voiceTranscribeFailed'),
          { tone: 'danger' },
        )
        return
      }
      const text = String(data.text || '').trim()
      if (text) onText(text)
    } catch (err) {
      if (controller.signal.aborted || generation !== recordingGeneration || cleanedUp) return
      console.warn('Voice transcription failed:', err instanceof Error ? err.message : String(err))
      pushToast(i18n.global.t('chat.toast.voiceTranscribeFailed'), { tone: 'danger' })
    } finally {
      if (transcriptionController === controller) transcriptionController = null
      if (generation === recordingGeneration) voiceBusy.value = false
    }
  }

  function stopTracks() {
    if (!activeStream) return
    activeStream.getTracks().forEach(track => track.stop())
    activeStream = null
  }

  function cancelRecording() {
    recordingGeneration += 1
    chunks = []
    transcriptionController?.abort()
    transcriptionController = null
    const mediaRecorder = recorder
    recorder = null
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.ondataavailable = null
      mediaRecorder.onstop = null
      try {
        mediaRecorder.stop()
      } catch {}
    }
    voiceBusy.value = false
    voiceRecording.value = false
    stopTracks()
  }

  function onVisibilityChange() {
    if (document.visibilityState === 'hidden') cancelRecording()
  }

  if (typeof window !== 'undefined') {
    const desktop = (window as unknown as {
      opensquillaDesktop?: DesktopWindowVisibilityBridge
    }).opensquillaDesktop
    const unsubscribe = desktop?.onWindowHidden?.(cancelRecording)
    if (typeof unsubscribe === 'function') unsubscribeWindowHidden = unsubscribe
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibilityChange)
  }

  function cleanup() {
    if (cleanedUp) return
    cleanedUp = true
    try {
      unsubscribeWindowHidden?.()
    } catch {}
    unsubscribeWindowHidden = null
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
    cancelRecording()
  }

  return {
    voiceBusy,
    voiceRecording,
    toggleVoiceInput,
    cleanup,
  }
}

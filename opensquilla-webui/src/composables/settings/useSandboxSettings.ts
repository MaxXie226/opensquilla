import { computed, reactive, ref } from 'vue'

import { useRpcStore } from '@/stores/rpc'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxTokenRecord,
} from '@/types/sandbox'

export type SandboxPolicySection = 'files' | 'commands' | 'network' | 'runtimes'

function clonePolicy(policy: SandboxPolicy): SandboxPolicy {
  return JSON.parse(JSON.stringify(policy)) as SandboxPolicy
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function useSandboxSettings() {
  const rpc = useRpcStore()
  const loading = ref(false)
  const loadError = ref('')
  const capability = ref<SandboxCapabilityReport | null>(null)
  const baseline = ref<SandboxPolicy | null>(null)
  const draft = ref<SandboxPolicy | null>(null)
  const builtinDenyWritePaths = ref<string[]>([])
  const tokens = ref<SandboxTokenRecord[]>([])
  const revealedToken = ref('')
  const sectionPending = reactive<Record<SandboxPolicySection, boolean>>({
    files: false,
    commands: false,
    network: false,
    runtimes: false,
  })
  const sectionError = reactive<Record<SandboxPolicySection, string>>({
    files: '',
    commands: '',
    network: '',
    runtimes: '',
  })
  const tokenPending = ref(false)
  const tokenError = ref('')
  let saveQueue = Promise.resolve()

  const ready = computed(() => Boolean(baseline.value && draft.value))

  function sectionDirty(section: SandboxPolicySection): boolean {
    if (!baseline.value || !draft.value) return false
    return JSON.stringify(baseline.value[section]) !== JSON.stringify(draft.value[section])
  }

  async function load(): Promise<void> {
    loading.value = true
    loadError.value = ''
    try {
      await rpc.waitForConnection()
      const [capabilityPayload, policyPayload, defaultsPayload, tokenPayload] = await Promise.all([
        rpc.call<SandboxCapabilityReport>('sandbox.capability.status'),
        rpc.call<SandboxPolicy>('sandbox.policy.get'),
        rpc.call<{ builtinDenyWritePaths?: unknown }>('sandbox.policy.defaults'),
        rpc.call<{ tokens?: unknown }>('sandbox.tokens.list'),
      ])
      capability.value = capabilityPayload
      baseline.value = clonePolicy(policyPayload)
      draft.value = clonePolicy(policyPayload)
      builtinDenyWritePaths.value = Array.isArray(defaultsPayload.builtinDenyWritePaths)
        ? defaultsPayload.builtinDenyWritePaths.map(String)
        : []
      tokens.value = Array.isArray(tokenPayload.tokens)
        ? tokenPayload.tokens as SandboxTokenRecord[]
        : []
    } catch (error) {
      loadError.value = errorMessage(error)
    } finally {
      loading.value = false
    }
  }

  async function performSectionSave(section: SandboxPolicySection): Promise<void> {
    if (!baseline.value || !draft.value || !sectionDirty(section)) return
    sectionPending[section] = true
    sectionError[section] = ''
    try {
      const sectionValue = JSON.parse(JSON.stringify(draft.value[section]))
      const candidate = clonePolicy(baseline.value)
      Object.assign(candidate, { [section]: sectionValue })
      const saved = await rpc.call<SandboxPolicy>('sandbox.policy.update', {
        basePolicyVersion: baseline.value.policyVersion,
        policy: candidate,
      })
      const otherDrafts = clonePolicy(draft.value)
      baseline.value = clonePolicy(saved)
      draft.value = clonePolicy(saved)
      for (const other of ['files', 'commands', 'network', 'runtimes'] as const) {
        if (other !== section) Object.assign(draft.value, { [other]: otherDrafts[other] })
      }
    } catch (error) {
      sectionError[section] = errorMessage(error)
      throw error
    } finally {
      sectionPending[section] = false
    }
  }

  function saveSection(section: SandboxPolicySection): Promise<void> {
    const queued = saveQueue.then(() => performSectionSave(section))
    saveQueue = queued.catch(() => undefined)
    return queued
  }

  function discardSection(section: SandboxPolicySection): void {
    if (!baseline.value || !draft.value) return
    draft.value[section] = JSON.parse(JSON.stringify(baseline.value[section]))
    sectionError[section] = ''
  }

  async function createToken(name: string, hostExecute: boolean): Promise<void> {
    const cleanName = name.trim()
    if (!cleanName) return
    tokenPending.value = true
    tokenError.value = ''
    revealedToken.value = ''
    try {
      const payload = await rpc.call<{
        token: string
        record: SandboxTokenRecord
      }>('sandbox.tokens.create', {
        name: cleanName,
        hostExecute,
      })
      revealedToken.value = payload.token
      tokens.value = [payload.record, ...tokens.value]
    } catch (error) {
      tokenError.value = errorMessage(error)
      throw error
    } finally {
      tokenPending.value = false
    }
  }

  async function revokeToken(publicId: string): Promise<void> {
    tokenPending.value = true
    tokenError.value = ''
    try {
      const payload = await rpc.call<{ revoked: boolean }>('sandbox.tokens.revoke', {
        publicId,
      })
      if (payload.revoked) {
        tokens.value = tokens.value.filter(token => token.publicId !== publicId)
      }
    } catch (error) {
      tokenError.value = errorMessage(error)
      throw error
    } finally {
      tokenPending.value = false
    }
  }

  return {
    loading,
    loadError,
    capability,
    baseline,
    draft,
    ready,
    builtinDenyWritePaths,
    tokens,
    revealedToken,
    sectionPending,
    sectionError,
    tokenPending,
    tokenError,
    sectionDirty,
    load,
    saveSection,
    discardSection,
    createToken,
    revokeToken,
  }
}

import { computed, reactive, ref } from 'vue'

import { usePlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import type {
  SandboxCapabilityReport,
  SandboxLanSettings,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
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
  const platform = usePlatform()
  const loading = ref(false)
  const loadError = ref('')
  const capability = ref<SandboxCapabilityReport | null>(null)
  const baseline = ref<SandboxPolicy | null>(null)
  const draft = ref<SandboxPolicy | null>(null)
  const builtinDenyWritePaths = ref<string[]>([])
  const runtimeTarget = ref<string | null>(null)
  const runtimeVersions = ref<SandboxPolicyDefaults['runtimeVersions']>({})
  const defaultRunModeBaseline = ref<SandboxRunMode>('safe')
  const defaultRunMode = ref<SandboxRunMode>('safe')
  const defaultRunModePending = ref(false)
  const defaultRunModeError = ref('')
  const lanBaseline = ref<SandboxLanSettings | null>(null)
  const lanDraft = ref<SandboxLanSettings | null>(null)
  const lanPending = ref(false)
  const lanError = ref('')
  const lanRestartRequired = ref(false)
  const sandboxWarningSuppressed = ref(false)
  const desktopWarningPreferenceAvailable = ref(false)
  const desktopPreferencePending = ref(false)
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
      const [capabilityPayload, policyPayload, defaultsPayload, tokenPayload, runModePayload, configPayload] = await Promise.all([
        rpc.call<SandboxCapabilityReport>('sandbox.capability.status'),
        rpc.call<SandboxPolicy>('sandbox.policy.get'),
        rpc.call<Partial<SandboxPolicyDefaults>>('sandbox.policy.defaults'),
        rpc.call<{ tokens?: unknown }>('sandbox.tokens.list'),
        rpc.call<{ runMode?: unknown }>('sandbox.run_mode.preference.get'),
        rpc.call<Record<string, unknown>>('config.get'),
      ])
      capability.value = capabilityPayload
      baseline.value = clonePolicy(policyPayload)
      draft.value = clonePolicy(policyPayload)
      builtinDenyWritePaths.value = Array.isArray(defaultsPayload.builtinDenyWritePaths)
        ? defaultsPayload.builtinDenyWritePaths.map(String)
        : []
      runtimeTarget.value = typeof defaultsPayload.runtimeTarget === 'string'
        ? defaultsPayload.runtimeTarget
        : null
      runtimeVersions.value = defaultsPayload.runtimeVersions ?? {}
      const loadedRunMode: SandboxRunMode = runModePayload.runMode === 'full' ? 'full' : 'safe'
      defaultRunModeBaseline.value = loadedRunMode
      defaultRunMode.value = loadedRunMode
      const auth = configPayload.auth && typeof configPayload.auth === 'object'
        ? configPayload.auth as Record<string, unknown>
        : {}
      const lan: SandboxLanSettings = {
        listenOnLan: String(configPayload.host ?? '127.0.0.1') !== '127.0.0.1'
          && String(configPayload.host ?? '') !== '::1'
          && String(configPayload.host ?? '').toLowerCase() !== 'localhost',
        allowedClientCidrs: Array.isArray(auth.allowed_client_cidrs)
          ? auth.allowed_client_cidrs.map(String)
          : [],
      }
      lanBaseline.value = JSON.parse(JSON.stringify(lan))
      lanDraft.value = JSON.parse(JSON.stringify(lan))
      tokens.value = Array.isArray(tokenPayload.tokens)
        ? tokenPayload.tokens as SandboxTokenRecord[]
        : []
      const desktop = platform.settings
      if (typeof desktop.getDesktopPreferences === 'function') {
        desktopWarningPreferenceAvailable.value = true
        try {
          const preferences = await desktop.getDesktopPreferences()
          sandboxWarningSuppressed.value = Boolean(
            preferences.sandboxUnavailableWarningSuppressed,
          )
        } catch {
          desktopWarningPreferenceAvailable.value = false
        }
      }
    } catch (error) {
      loadError.value = errorMessage(error)
    } finally {
      loading.value = false
    }
  }

  async function refreshCapability(): Promise<void> {
    await rpc.waitForConnection()
    capability.value = await rpc.call<SandboxCapabilityReport>(
      'sandbox.capability.status',
      { refresh: true },
    )
  }

  async function saveDefaultRunMode(): Promise<void> {
    if (defaultRunMode.value === defaultRunModeBaseline.value) return
    defaultRunModePending.value = true
    defaultRunModeError.value = ''
    try {
      const payload = await rpc.call<{ runMode?: unknown }>(
        'sandbox.run_mode.preference.set',
        { runMode: defaultRunMode.value },
      )
      defaultRunModeBaseline.value = payload.runMode === 'full' ? 'full' : 'safe'
      defaultRunMode.value = defaultRunModeBaseline.value
    } catch (error) {
      defaultRunModeError.value = errorMessage(error)
      throw error
    } finally {
      defaultRunModePending.value = false
    }
  }

  function discardDefaultRunMode(): void {
    defaultRunMode.value = defaultRunModeBaseline.value
    defaultRunModeError.value = ''
  }

  function lanDirty(): boolean {
    return JSON.stringify(lanBaseline.value) !== JSON.stringify(lanDraft.value)
  }

  async function saveLan(): Promise<void> {
    if (!lanDraft.value || !lanDirty()) return
    lanPending.value = true
    lanError.value = ''
    try {
      const payload = await rpc.call<{ restartRequired?: boolean }>('config.patch', {
        patch: {
          host: lanDraft.value.listenOnLan ? '0.0.0.0' : '127.0.0.1',
          auth: {
            allowed_client_cidrs: lanDraft.value.allowedClientCidrs,
          },
        },
      })
      lanBaseline.value = JSON.parse(JSON.stringify(lanDraft.value))
      lanRestartRequired.value = Boolean(payload.restartRequired)
    } catch (error) {
      lanError.value = errorMessage(error)
      throw error
    } finally {
      lanPending.value = false
    }
  }

  function discardLan(): void {
    if (!lanBaseline.value) return
    lanDraft.value = JSON.parse(JSON.stringify(lanBaseline.value))
    lanError.value = ''
  }

  async function resetSandboxUnavailableWarning(): Promise<void> {
    const desktop = platform.settings
    if (typeof desktop.saveDesktopPreferences !== 'function') return
    desktopPreferencePending.value = true
    try {
      const preferences = await desktop.saveDesktopPreferences({
        sandboxUnavailableWarningSuppressed: false,
      })
      sandboxWarningSuppressed.value = Boolean(
        preferences.sandboxUnavailableWarningSuppressed,
      )
    } finally {
      desktopPreferencePending.value = false
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
    runtimeTarget,
    runtimeVersions,
    defaultRunMode,
    defaultRunModeBaseline,
    defaultRunModePending,
    defaultRunModeError,
    lanDraft,
    lanPending,
    lanError,
    lanRestartRequired,
    sandboxWarningSuppressed,
    desktopWarningPreferenceAvailable,
    desktopPreferencePending,
    tokens,
    revealedToken,
    sectionPending,
    sectionError,
    tokenPending,
    tokenError,
    sectionDirty,
    load,
    refreshCapability,
    saveDefaultRunMode,
    discardDefaultRunMode,
    lanDirty,
    saveLan,
    discardLan,
    resetSandboxUnavailableWarning,
    saveSection,
    discardSection,
    createToken,
    revokeToken,
  }
}

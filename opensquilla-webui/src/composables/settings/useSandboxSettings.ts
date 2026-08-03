import { computed, onScopeDispose, reactive, ref } from 'vue'

import { usePlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
  SandboxSetupState,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

export type SandboxPolicySection = 'files' | 'commands' | 'network' | 'runtimes'
export type SandboxSetupOutcome = 'idle' | 'ready' | 'cancelled' | 'failed' | 'verification_failed'

function clonePolicy(policy: SandboxPolicy): SandboxPolicy {
  return JSON.parse(JSON.stringify(policy)) as SandboxPolicy
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function normalizeSetupStatus(payload: unknown): SandboxSetupStatusPayload | null {
  if (!payload || typeof payload !== 'object') return null
  const raw = payload as Record<string, unknown>
  const state = String(raw.state || '') as SandboxSetupState
  if (!['not_setup', 'setting_up', 'ready', 'failed', 'unavailable'].includes(state)) return null
  return {
    state,
    platform: String(raw.platform || ''),
    message: String(raw.message || ''),
    requiresAdmin: raw.requiresAdmin === true || raw.requires_admin === true,
    detail: typeof raw.detail === 'string' ? raw.detail : undefined,
  }
}

export function useSandboxSettings() {
  const rpc = useRpcStore()
  const platform = usePlatform()
  const loading = ref(false)
  const capabilityLoading = ref(false)
  const capabilityCheckFailed = ref(false)
  const sandboxSetupStatus = ref<SandboxSetupStatusPayload | null>(null)
  const sandboxSetupPending = ref(false)
  const sandboxSetupOutcome = ref<SandboxSetupOutcome>('idle')
  const loadError = ref('')
  const capability = ref<SandboxCapabilityReport | null>(null)
  const baseline = ref<SandboxPolicy | null>(null)
  const draft = ref<SandboxPolicy | null>(null)
  const builtinDenyWritePaths = ref<string[]>([])
  const runtimeTarget = ref<string | null>(null)
  const runtimeVersions = ref<SandboxPolicyDefaults['runtimeVersions']>({})
  const defaultRunModeBaseline = ref<SandboxRunMode>('full')
  const defaultRunMode = ref<SandboxRunMode>('full')
  const defaultRunModePending = ref(false)
  const defaultRunModeError = ref('')
  const sandboxWarningSuppressed = ref(false)
  const desktopWarningPreferenceAvailable = ref(false)
  const desktopPreferencePending = ref(false)
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
  let saveQueue = Promise.resolve()
  let disposed = false
  let capabilityRequestGeneration = 0

  const ready = computed(() => Boolean(baseline.value && draft.value))
  const canRequestSandboxSetup = computed(() => (
    platform.capabilities.isDesktop
    && capability.value?.setupSupported !== false
    && (
      sandboxSetupStatus.value?.state === 'not_setup'
      || sandboxSetupStatus.value?.state === 'failed'
    )
  ))

  function sectionDirty(section: SandboxPolicySection): boolean {
    if (!baseline.value || !draft.value) return false
    return JSON.stringify(baseline.value[section]) !== JSON.stringify(draft.value[section])
  }

  async function load(): Promise<void> {
    loading.value = true
    loadError.value = ''
    try {
      await rpc.waitForConnection()
      const [policyPayload, defaultsPayload, runModePayload] = await Promise.all([
        rpc.call<SandboxPolicy>('sandbox.policy.get'),
        rpc.call<Partial<SandboxPolicyDefaults>>('sandbox.policy.defaults'),
        rpc.call<{ runMode?: unknown }>('sandbox.run_mode.preference.get'),
      ])
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
      void loadSandboxReadiness()
      void loadDesktopPreference()
    } catch (error) {
      loadError.value = errorMessage(error)
    } finally {
      loading.value = false
    }
  }

  async function loadCapability(forceRefresh = false): Promise<SandboxCapabilityReport | null> {
    if (disposed) return null
    const requestGeneration = ++capabilityRequestGeneration
    capabilityLoading.value = true
    capabilityCheckFailed.value = false
    try {
      await rpc.waitForConnection()
      const report = await rpc.call<SandboxCapabilityReport>(
        'sandbox.capability.status',
        forceRefresh ? { refresh: true } : undefined,
      )
      if (disposed || requestGeneration !== capabilityRequestGeneration) return null
      capability.value = report
      return report
    } catch {
      if (disposed || requestGeneration !== capabilityRequestGeneration) return null
      capability.value = null
      capabilityCheckFailed.value = true
      return null
    } finally {
      if (!disposed && requestGeneration === capabilityRequestGeneration) {
        capabilityLoading.value = false
      }
    }
  }

  async function loadSetupStatus(): Promise<SandboxSetupStatusPayload | null> {
    if (!platform.capabilities.isDesktop || disposed) return null
    try {
      await rpc.waitForConnection()
      const status = normalizeSetupStatus(await rpc.call('sandbox.setup.status'))
      if (!disposed && status) sandboxSetupStatus.value = status
      return status
    } catch {
      // Capability status remains the visible fallback for old Gateways.
      return null
    }
  }

  async function loadSandboxReadiness(): Promise<void> {
    if (!platform.capabilities.isDesktop) {
      await loadCapability()
      return
    }
    const status = await loadSetupStatus()
    if (status === null || status.state === 'ready') await loadCapability()
  }

  async function ensureSandboxSetupForSafeMode(): Promise<boolean> {
    if (!canRequestSandboxSetup.value || sandboxSetupPending.value) return false
    sandboxSetupPending.value = true
    sandboxSetupOutcome.value = 'idle'
    try {
      const status = normalizeSetupStatus(await rpc.call('sandbox.setup.ensure'))
      if (!status) {
        sandboxSetupOutcome.value = 'failed'
        return false
      }
      sandboxSetupStatus.value = status
      if (status.state !== 'ready') {
        sandboxSetupOutcome.value = status.detail?.toLowerCase().includes('cancel')
          ? 'cancelled'
          : 'failed'
        return false
      }
      const report = await loadCapability(true)
      if (!report?.available) {
        sandboxSetupOutcome.value = 'verification_failed'
        return false
      }
      sandboxSetupOutcome.value = 'ready'
      return true
    } catch {
      sandboxSetupOutcome.value = 'failed'
      return false
    } finally {
      sandboxSetupPending.value = false
    }
  }

  onScopeDispose(() => {
    disposed = true
    capabilityRequestGeneration += 1
  })

  async function loadDesktopPreference(): Promise<void> {
    const desktop = platform.settings
    if (typeof desktop.getDesktopPreferences !== 'function') return
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

  return {
    loading,
    capabilityLoading,
    capabilityCheckFailed,
    sandboxSetupStatus,
    sandboxSetupPending,
    sandboxSetupOutcome,
    canRequestSandboxSetup,
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
    sandboxWarningSuppressed,
    desktopWarningPreferenceAvailable,
    desktopPreferencePending,
    sectionPending,
    sectionError,
    sectionDirty,
    load,
    loadCapability,
    loadSetupStatus,
    ensureSandboxSetupForSafeMode,
    saveDefaultRunMode,
    discardDefaultRunMode,
    resetSandboxUnavailableWarning,
    saveSection,
    discardSection,
  }
}

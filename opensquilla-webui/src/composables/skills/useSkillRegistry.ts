import { computed, ref, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'
import {
  createSkillMutationGate,
  type SkillMutationGate,
} from '@/composables/skills/useSkillMutationGate'
import type {
  RegistryResult,
  SkillDependencyInstallOutcome,
  SkillDiagnostic,
  SkillLifecycle,
  SkillSourceResolution,
} from '@/types/skills'

interface RegistrySearchData {
  results?: RegistryResult[]
  diagnostics?: SkillDiagnostic[]
  message?: string
}

export interface InstallResult {
  success: boolean
  unchanged?: boolean
  name?: string
  message?: string
  installed?: boolean
  active?: boolean
  instruction_usable?: boolean
  installId?: string
  lifecycle?: SkillLifecycle
  resolution?: SkillSourceResolution
  diagnostics?: SkillDiagnostic[]
  rollbackPerformed?: boolean
  catalogGeneration?: number
  effectiveFrom?: 'next_turn' | 'next_start' | string
  missing_still?: {
    bins?: string[]
    env?: string[]
    env_any?: string[][]
  }
}

export type SkillInstallQueueStatus =
  | 'queued'
  | 'installing'
  | 'installed'
  | 'unchanged'
  | 'failed'

export interface SkillInstallQueueItem {
  id: string
  identifier: string
  source: string
  displayName: string
  status: SkillInstallQueueStatus
  result?: InstallResult
  error?: string
}

export type SkillInstallSource = 'clawhub' | 'github'

export interface SkillInstallActivity {
  items: SkillInstallQueueItem[]
  refreshWarning: string
}

export type SkillInstallActivities = Record<SkillInstallSource, SkillInstallActivity>

interface SkillInstallRequest {
  identifier: string
  source: string
  displayName?: string
}

const GENERIC_GITHUB_SKILL_SEGMENTS = new Set([
  'skill',
  'skills',
  'skill.md',
  'skills.md',
])

function decodedPathSegment(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function concisePathLabel(segments: string[], fallback: string): string {
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = decodedPathSegment(segments[index]).trim()
    if (!segment || GENERIC_GITHUB_SKILL_SEGMENTS.has(segment.toLowerCase())) continue
    return segment
  }
  return fallback
}

/**
 * Derive a short queue label without rewriting the exact GitHub install
 * identifier. The production resolver still receives the original reference.
 */
export function githubSkillDisplayName(identifier: string): string {
  const trimmed = identifier.trim()
  if (!trimmed) return trimmed

  try {
    const url = new URL(trimmed)
    if (url.hostname.toLowerCase() === 'github.com') {
      const segments = url.pathname.split('/').filter(Boolean)
      const repository = decodedPathSegment(segments[1] || '').replace(/\.git$/i, '')
      const treeMarker = segments.findIndex(segment =>
        segment.toLowerCase() === 'tree' || segment.toLowerCase() === 'blob')
      const skillPath = treeMarker >= 0
        ? segments.slice(treeMarker + 2)
        : segments.slice(2)
      return concisePathLabel(skillPath, repository || trimmed)
    }
  } catch {
    // Exact GitHub references also have a non-URL owner/repo@revision:path form.
  }

  const separator = trimmed.indexOf(':')
  const packageReference = separator >= 0 ? trimmed.slice(0, separator) : trimmed
  const skillPath = separator >= 0 ? trimmed.slice(separator + 1) : ''
  const revisionMarker = packageReference.lastIndexOf('@')
  const packageIdentifier = revisionMarker > packageReference.lastIndexOf('/')
    ? packageReference.slice(0, revisionMarker)
    : packageReference
  const packageSegments = packageIdentifier.split('/').filter(Boolean)
  const repository = decodedPathSegment(
    packageSegments[packageSegments.length - 1] || '',
  ).replace(/\.git$/i, '')

  return concisePathLabel(skillPath.split('/').filter(Boolean), repository || trimmed)
}

export function skillRegistryOperationKey(identifier: string, source: string): string {
  return JSON.stringify([source || 'clawhub', identifier])
}

export interface SkillRegistry {
  registryQuery: Ref<string>
  githubUrl: Ref<string>
  registryResults: Ref<RegistryResult[]>
  registryLoading: Ref<boolean>
  registryDiagnostics: Ref<SkillDiagnostic[]>
  registrySearchError: Ref<string>
  installingId: Ref<string | null>
  installActivities: Ref<SkillInstallActivities>
  runningSource: Ref<SkillInstallSource | null>
  queueRunning: ComputedRef<boolean>
  mutationBusy: ComputedRef<boolean>
  installingDepsId: Ref<string | null>
  uninstallingName: Ref<string | null>
  searchRegistry: () => Promise<void>
  installGithub: () => Promise<void>
  installSkill: (identifier: string, source: string, displayName?: string) => Promise<void>
  retryQueueItem: (id: string) => Promise<void>
  clearInstallActivity: (source: SkillInstallSource) => void
  installDeps: (name: string, installId: string) => Promise<SkillDependencyInstallOutcome>
  uninstallSkill: (name: string) => Promise<boolean>
}

export function useSkillRegistry(
  rpc: ReturnType<typeof useRpcStore>,
  loadData: () => Promise<boolean>,
  mutationGate: SkillMutationGate = createSkillMutationGate(),
): SkillRegistry {
  const { pushToast } = useToasts()
  const t = i18n.global.t
  const registryQuery = ref('')
  const githubUrl = ref('')
  const registryResults = ref<RegistryResult[]>([])
  const registryLoading = ref(false)
  const registryDiagnostics = ref<SkillDiagnostic[]>([])
  const registrySearchError = ref('')
  const installingId = ref<string | null>(null)
  const installActivities = ref<SkillInstallActivities>({
    clawhub: { items: [], refreshWarning: '' },
    github: { items: [], refreshWarning: '' },
  })
  const runningSource = ref<SkillInstallSource | null>(null)
  const queueRunning = computed(() => runningSource.value !== null)
  const mutationBusy = computed(() => mutationGate.busy.value)
  const installingDepsId = ref<string | null>(null)
  const uninstallingName = ref<string | null>(null)

  async function searchRegistry() {
    if (!registryQuery.value.trim()) return
    registryLoading.value = true
    registryResults.value = []
    registryDiagnostics.value = []
    registrySearchError.value = ''
    try {
      const data = await rpc.call<RegistrySearchData>('skills.search', {
        query: registryQuery.value.trim(),
        limit: 20,
        source: 'clawhub',
      })
      registryResults.value = data.results || []
      registryDiagnostics.value = data.diagnostics || []
      registrySearchError.value = data.message || ''
    } catch (err) {
      registrySearchError.value = (err as Error).message
      pushToast(t('cronSkills.registry.toastSearchFailed', { error: registrySearchError.value }), { tone: 'danger' })
    } finally {
      registryLoading.value = false
    }
  }

  function uniqueRequests(requests: SkillInstallRequest[]): SkillInstallRequest[] {
    const seen = new Set<string>()
    return requests.flatMap((request) => {
      const identifier = request.identifier.trim()
      const source = (request.source || 'clawhub').trim() || 'clawhub'
      if (!identifier) return []
      const key = skillRegistryOperationKey(identifier, source)
      if (seen.has(key)) return []
      seen.add(key)
      return [{ ...request, identifier, source }]
    })
  }

  function requestToQueueItem(request: SkillInstallRequest): SkillInstallQueueItem {
    return {
      id: skillRegistryOperationKey(request.identifier, request.source),
      identifier: request.identifier,
      source: request.source,
      displayName: request.displayName
        || (request.source === 'github'
          ? githubSkillDisplayName(request.identifier)
          : request.identifier),
      status: 'queued',
    }
  }

  function activitySource(source: string): SkillInstallSource {
    return source === 'github' ? 'github' : 'clawhub'
  }

  function removeSuccessfulGithubLines(items: SkillInstallQueueItem[]) {
    const successful = new Set(
      items
        .filter(item => item.source === 'github'
          && (item.status === 'installed' || item.status === 'unchanged'))
        .map(item => item.identifier),
    )
    if (!successful.size) return
    githubUrl.value = githubUrl.value
      .split(/\r?\n/)
      .filter(line => !successful.has(line.trim()))
      .join('\n')
      .trim()
  }

  async function refreshCatalogAfterBatch(
    source: SkillInstallSource,
    items: SkillInstallQueueItem[],
  ) {
    const changed = items.some(item => item.status === 'installed' || item.status === 'unchanged')
    if (!changed) return
    if (!(await loadData())) {
      const message = t('cronSkills.skillsView.reloadListFailed')
      installActivities.value[source].refreshWarning = message
      pushToast(message, { tone: 'warn' })
    }
  }

  async function processQueueItems(items: SkillInstallQueueItem[]) {
    for (const item of items) {
      item.status = 'installing'
      item.error = ''
      item.result = undefined
      installingId.value = item.id
      try {
        const res = await rpc.call<InstallResult>('skills.install', {
          identifier: item.identifier,
          source: item.source,
        })
        item.result = res
        item.displayName = res.name || item.displayName
        item.status = res.success ? (res.unchanged ? 'unchanged' : 'installed') : 'failed'
        item.error = res.success ? '' : (res.message || t('cronSkills.registry.installFailed'))
        markRegistryResultOutcome(item.identifier, item.source, res)
      } catch (err) {
        item.status = 'failed'
        item.error = (err as Error).message
      } finally {
        installingId.value = null
      }
    }
  }

  async function runNewBatch(requests: SkillInstallRequest[]) {
    const unique = uniqueRequests(requests)
    if (!unique.length) return
    if (!mutationGate.acquire('install_queue')) return
    const source = activitySource(unique[0].source)
    const items = unique.map(requestToQueueItem)
    installActivities.value[source] = { items, refreshWarning: '' }
    const activityItems = installActivities.value[source].items
    runningSource.value = source
    try {
      await processQueueItems(activityItems)
      removeSuccessfulGithubLines(activityItems)
      await refreshCatalogAfterBatch(source, activityItems)
    } finally {
      runningSource.value = null
      installingId.value = null
      mutationGate.release('install_queue')
    }
  }

  async function installGithub() {
    await runNewBatch(
      githubUrl.value
        .split(/\r?\n/)
        .map(identifier => ({ identifier, source: 'github' })),
    )
  }

  function markRegistryResultOutcome(
    identifier: string,
    source: string,
    installResult: InstallResult,
  ) {
    const installSource = source || 'clawhub'
    registryResults.value = registryResults.value.map((registryResult) => {
      const resultSource = registryResult.source || 'clawhub'
      const resultIdentifier = registryResult.installReference
        || registryResult.install_reference
        || registryResult.identifier
        || registryResult.name
      const sameSource = resultSource === installSource
      const sameIdentifier = resultIdentifier === identifier

      if (!sameSource || !sameIdentifier) return registryResult
      return {
        ...registryResult,
        installed: installResult.installed ?? installResult.success,
        lifecycle: installResult.lifecycle,
        instruction_usable: installResult.instruction_usable,
        diagnostics: installResult.diagnostics,
      }
    })
  }

  async function installSkill(identifier: string, source: string, displayName?: string) {
    await runNewBatch([{ identifier, source, displayName }])
  }

  async function retryQueueItem(id: string) {
    const source = (['clawhub', 'github'] as const).find(candidate =>
      installActivities.value[candidate].items.some(item => item.id === id))
    if (!source) return
    const item = installActivities.value[source].items.find(candidate => candidate.id === id)
    if (!item || item.status !== 'failed') return
    if (!mutationGate.acquire('install_queue')) return
    installActivities.value[source].refreshWarning = ''
    runningSource.value = source
    try {
      await processQueueItems([item])
      removeSuccessfulGithubLines([item])
      await refreshCatalogAfterBatch(source, [item])
    } finally {
      runningSource.value = null
      installingId.value = null
      mutationGate.release('install_queue')
    }
  }

  function clearInstallActivity(source: SkillInstallSource) {
    if (runningSource.value) return
    installActivities.value[source] = { items: [], refreshWarning: '' }
  }

  async function installDeps(name: string, installId: string): Promise<SkillDependencyInstallOutcome> {
    const failed = (message = ''): SkillDependencyInstallOutcome => ({
      success: false,
      complete: false,
      message,
      missingStill: { bins: [], env: [], env_any: [] },
    })
    if (!name || !installId || !mutationGate.acquire('dependency_install')) return failed()
    installingDepsId.value = installId
    try {
      const res = await rpc.call<InstallResult>('skills.deps.install', { name, install_id: installId })
      if (res.success) {
        pushToast(res.message || t('cronSkills.registry.installed'), { tone: 'ok' })
        const still = res.missing_still || {}
        const missingStill = {
          bins: still.bins || [],
          env: still.env || [],
          env_any: still.env_any || [],
        }
        const stillMissing = missingStill.bins.length
          + missingStill.env.length
          + missingStill.env_any.length
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
        return {
          success: true,
          complete: stillMissing === 0,
          message: res.message || '',
          missingStill,
        }
      }
      pushToast(res.message || t('cronSkills.registry.installFailed'), { tone: 'danger' })
      return failed(res.message || '')
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
      return failed((err as Error).message)
    } finally {
      installingDepsId.value = null
      mutationGate.release('dependency_install')
    }
  }

  async function uninstallSkill(name: string): Promise<boolean> {
    if (!name || !mutationGate.acquire('uninstall')) return false
    uninstallingName.value = name
    try {
      const res = await rpc.call<InstallResult>('skills.uninstall', { name })
      if (res.success) {
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
        return true
      }
      pushToast(res.message || t('cronSkills.registry.uninstallFailed'), { tone: 'danger' })
      return false
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
      return false
    } finally {
      uninstallingName.value = null
      mutationGate.release('uninstall')
    }
  }

  return {
    registryQuery,
    githubUrl,
    registryResults,
    registryLoading,
    registryDiagnostics,
    registrySearchError,
    installingId,
    installActivities,
    runningSource,
    queueRunning,
    mutationBusy,
    installingDepsId,
    uninstallingName,
    searchRegistry,
    installGithub,
    installSkill,
    retryQueueItem,
    clearInstallActivity,
    installDeps,
    uninstallSkill,
  }
}

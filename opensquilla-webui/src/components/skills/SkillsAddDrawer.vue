<template>
  <Teleport to="body">
    <Transition name="sk-add-drawer">
      <div v-if="open" class="sk-add-overlay" data-testid="skills-add-overlay">
        <div class="sk-add-overlay__scrim" data-testid="skills-add-scrim" @click="emit('close')" />
        <aside
          id="skills-add-drawer"
          ref="drawerRef"
          class="sk-add-drawer"
          role="dialog"
          aria-modal="true"
          aria-labelledby="skills-add-title"
        >
          <header class="sk-add-drawer__head">
            <div>
              <h2 id="skills-add-title">{{ t('cronSkills.registry.drawerTitle') }}</h2>
              <p>{{ t('cronSkills.registry.drawerSubtitle') }}</p>
            </div>
            <button
              ref="closeButtonRef"
              class="btn btn--ghost sk-add-drawer__close"
              type="button"
              :aria-label="t('common.close')"
              @click="emit('close')"
            >
              <Icon name="x" :size="18" />
            </button>
          </header>

          <div class="sk-add-drawer__body">
            <div class="sk-add-source-tabs" role="tablist" :aria-label="t('cronSkills.registry.sourceLabel')">
              <button
                id="skills-add-tab-clawhub"
                class="sk-add-source-tab"
                :class="{ 'is-active': sourceMode === 'clawhub' }"
                type="button"
                role="tab"
                :aria-selected="sourceMode === 'clawhub'"
                aria-controls="skills-add-panel-clawhub"
                @click="sourceMode = 'clawhub'"
              >
                <Icon name="download" :size="16" />
                <span>{{ t('cronSkills.registry.sourceClawHub') }}</span>
              </button>
              <button
                id="skills-add-tab-github"
                class="sk-add-source-tab"
                :class="{ 'is-active': sourceMode === 'github' }"
                type="button"
                role="tab"
                :aria-selected="sourceMode === 'github'"
                aria-controls="skills-add-panel-github"
                @click="sourceMode = 'github'"
              >
                <Icon name="share" :size="16" />
                <span>{{ t('cronSkills.registry.sourceGitHub') }}</span>
              </button>
            </div>

            <section
              v-if="sourceMode === 'github'"
              id="skills-add-panel-github"
              class="sk-add-source-panel"
              role="tabpanel"
              aria-labelledby="skills-add-tab-github"
            >
              <label class="sk-add-field-label" for="skills-add-github-input">
                {{ t('cronSkills.registry.githubReferencesLabel') }}
              </label>
              <textarea
                id="skills-add-github-input"
                :value="githubUrl"
                class="sk-add-textarea"
                rows="7"
                spellcheck="false"
                autocomplete="off"
                :disabled="queueRunning || mutationBlocked"
                :placeholder="t('cronSkills.registry.githubReferencesPlaceholder')"
                @input="emit('update:githubUrl', ($event.target as HTMLTextAreaElement).value)"
              />
              <p class="sk-add-help">{{ t('cronSkills.registry.githubReferencesHint') }}</p>
              <button
                class="btn btn--primary sk-add-primary"
                :class="{ 'sk-add-primary--busy': queueRunning }"
                data-testid="skills-install-github"
                type="button"
                :disabled="queueRunning || mutationBlocked || githubReferenceCount === 0"
                :aria-busy="queueRunning"
                @click="emit('installGithub')"
              >
                <span v-if="queueRunning" class="sk-spinner" />
                <Icon v-else name="download" :size="16" />
                <span>{{ primaryActionLabel }}</span>
              </button>
              <p class="sk-add-help sk-add-help--center">{{ t('cronSkills.registry.duplicatesSkipped') }}</p>
            </section>

            <section
              v-if="sourceMode === 'clawhub'"
              id="skills-add-panel-clawhub"
              class="sk-add-source-panel"
              role="tabpanel"
              aria-labelledby="skills-add-tab-clawhub"
            >
              <label class="sk-add-field-label" for="skills-add-clawhub-query">
                {{ t('cronSkills.registry.searchLabel') }}
              </label>
              <div class="sk-add-search-row">
                <div class="sk-add-input-wrap">
                  <Icon name="search" :size="16" />
                  <input
                    id="skills-add-clawhub-query"
                    :value="registryQuery"
                    type="search"
                    autocomplete="off"
                    :placeholder="t('cronSkills.registry.searchPlaceholder')"
                    @input="emit('update:registryQuery', ($event.target as HTMLInputElement).value)"
                    @keydown.enter="emit('search')"
                  />
                </div>
                <button
                  class="btn btn--primary"
                  type="button"
                  :disabled="loading || !registryQuery.trim()"
                  :aria-busy="loading"
                  @click="emit('search')"
                >
                  {{ loading ? t('cronSkills.registry.searchingShort') : t('cronSkills.registry.search') }}
                </button>
              </div>

              <div v-if="registrySearchError" class="sk-add-callout sk-add-callout--danger" role="alert">
                {{ registrySearchError }}
              </div>
              <div
                v-for="diagnostic in registryDiagnostics"
                :key="`${diagnostic.phase}:${diagnostic.code}:${diagnostic.message}`"
                class="sk-add-callout"
                :class="diagnostic.blocking ? 'sk-add-callout--danger' : 'sk-add-callout--warning'"
              >
                <strong>{{ diagnostic.code }}</strong>
                <span>{{ diagnostic.message }}</span>
                <small v-if="diagnostic.hint">{{ diagnostic.hint }}</small>
              </div>

              <div v-if="loading" class="sk-add-empty" role="status">
                <span class="sk-spinner" />
                <span>{{ t('cronSkills.registry.searching') }}</span>
              </div>
              <div v-else-if="results.length" class="sk-add-results">
                <article v-for="row in resultRows" :key="row.operationKey" class="sk-add-result">
                  <div class="sk-add-result__body">
                    <strong>{{ row.name }}</strong>
                    <p>{{ row.description }}</p>
                    <div class="sk-add-result__meta">
                      <span v-if="row.author">{{ row.author }}</span>
                      <span v-if="row.version">{{ row.version }}</span>
                      <span>{{ row.source }}</span>
                      <span>{{ row.trustLevel }}</span>
                      <span v-if="row.lifecycleLabel" :data-tone="row.lifecycleTone">
                        {{ row.lifecycleLabel }}
                      </span>
                    </div>
                  </div>
                  <button
                    class="btn btn--sm"
                    :class="row.installed ? 'btn--ghost' : 'btn--primary'"
                    type="button"
                    :disabled="row.installed || queueRunning || mutationBlocked"
                    @click="emit('install', row.installId, row.installSource, row.name)"
                  >
                    {{ row.installed ? t('cronSkills.registry.installed') : t('cronSkills.registry.install') }}
                  </button>
                </article>
              </div>
              <div v-else class="sk-add-empty">
                <Icon name="skills" :size="30" />
                <span>{{ t('cronSkills.registry.hintBrowse') }}</span>
              </div>
            </section>

            <section v-if="queueRows.length" class="sk-add-queue" aria-live="polite">
              <div class="sk-add-section-title">
                <h3>{{ t('cronSkills.registry.queueTitle') }}</h3>
                <span>{{ queueSummary }}</span>
              </div>
              <div v-if="queueRefreshWarning" class="sk-add-callout sk-add-callout--warning" role="status">
                {{ queueRefreshWarning }}
              </div>
              <article
                v-for="item in queueRows"
                :key="item.id"
                class="sk-add-queue-item"
                :data-status="item.status"
              >
                <span class="sk-add-queue-item__icon" aria-hidden="true">
                  <span v-if="item.status === 'installing'" class="sk-spinner" />
                  <Icon v-else-if="item.status === 'installed' || item.status === 'unchanged'" name="check" :size="18" />
                  <Icon v-else-if="item.status === 'failed'" name="info" :size="18" />
                  <Icon v-else name="clock" :size="18" />
                </span>
                <div class="sk-add-queue-item__body">
                  <div class="sk-add-queue-item__head">
                    <strong>{{ item.displayName }}</strong>
                    <span>{{ t(`cronSkills.registry.queueStatus.${item.status}`) }}</span>
                  </div>
                  <code :title="item.identifier">{{ item.identifier }}</code>
                  <p v-if="item.error" class="sk-add-queue-item__error">{{ item.error }}</p>
                  <div v-if="item.resultMeta.length" class="sk-add-queue-item__meta">
                    <span v-for="meta in item.resultMeta" :key="meta">{{ meta }}</span>
                  </div>
                  <span
                    v-if="item.lifecycleLabel"
                    class="sk-add-lifecycle"
                    :data-tone="item.lifecycleTone"
                  >{{ item.lifecycleLabel }}</span>
                  <details v-if="item.diagnostics.length" class="sk-add-diagnostics">
                    <summary>{{ t('cronSkills.registry.diagnostics', { count: item.diagnostics.length }) }}</summary>
                    <div v-for="diagnostic in item.diagnostics" :key="`${diagnostic.phase}:${diagnostic.code}`">
                      <strong>{{ diagnostic.code }}</strong>
                      <p>{{ diagnostic.message }}</p>
                      <p v-if="diagnostic.hint">{{ diagnostic.hint }}</p>
                      <pre v-if="diagnostic.details">{{ diagnosticDetails(diagnostic.details) }}</pre>
                    </div>
                  </details>
                  <button
                    v-if="item.status === 'failed'"
                    class="btn btn--ghost btn--sm sk-add-retry"
                    type="button"
                    :disabled="queueRunning || mutationBlocked"
                    @click="emit('retry', item.id)"
                  >
                    <Icon name="refresh" :size="14" />
                    <span>{{ t('cronSkills.registry.retry') }}</span>
                  </button>
                </div>
              </article>
            </section>
          </div>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, toRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import type { SkillInstallQueueItem } from '@/composables/skills/useSkillRegistry'
import { skillRegistryOperationKey } from '@/composables/skills/useSkillRegistry'
import { skillLifecyclePresentation } from '@/composables/skills/useSkillsCatalog'
import type { RegistryResult, SkillDiagnostic } from '@/types/skills'

const props = defineProps<{
  open: boolean
  registryQuery: string
  githubUrl: string
  results: RegistryResult[]
  loading: boolean
  registryDiagnostics: SkillDiagnostic[]
  registrySearchError: string
  queue: SkillInstallQueueItem[]
  queueRunning: boolean
  mutationBlocked?: boolean
  queueRefreshWarning: string
}>()

const emit = defineEmits<{
  close: []
  'update:registryQuery': [value: string]
  'update:githubUrl': [value: string]
  search: []
  installGithub: []
  install: [identifier: string, source: string, displayName: string]
  retry: [id: string]
}>()

const { t } = useI18n()
const sourceMode = ref<'github' | 'clawhub'>('github')
const drawerRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLButtonElement | null>(null)
useDialogA11y(drawerRef, toRef(props, 'open'), () => emit('close'), {
  initialFocus: closeButtonRef,
})

const githubReferences = computed(() => props.githubUrl
  .split(/\r?\n/)
  .map(value => value.trim())
  .filter(Boolean))
const githubReferenceCount = computed(() => new Set(githubReferences.value).size)
const completedCount = computed(() => props.queue.filter(item =>
  item.status === 'installed' || item.status === 'unchanged' || item.status === 'failed').length)
const currentIndex = computed(() => {
  const installing = props.queue.findIndex(item => item.status === 'installing')
  return installing >= 0 ? installing + 1 : Math.min(completedCount.value + 1, props.queue.length)
})
const primaryActionLabel = computed(() => {
  if (props.queueRunning) {
    return t('cronSkills.registry.installingProgress', {
      current: currentIndex.value,
      total: props.queue.length,
    })
  }
  return t('cronSkills.registry.installCount', { count: githubReferenceCount.value })
})
const queueSummary = computed(() => t('cronSkills.registry.queueSummary', {
  complete: completedCount.value,
  total: props.queue.length,
}))

const resultRows = computed(() => props.results.map((result) => {
  const lifecycle = result.lifecycle
  const showLifecycleWithoutInstall = lifecycle
    && (
      lifecycle.load_state === 'rejected'
      || lifecycle.load_state === 'serving_previous'
      || lifecycle.load_state === 'validated_offline'
      || lifecycle.install_state === 'missing'
      || lifecycle.install_state === 'drifted'
      || lifecycle.compatibility_state === 'unsupported'
    )
  const presentation = lifecycle && (result.installed || showLifecycleWithoutInstall)
    ? skillLifecyclePresentation({ name: result.name, lifecycle }, 'registry')
    : null
  const installId = result.installReference
    || result.install_reference
    || result.identifier
    || result.name
  const installSource = result.source || 'clawhub'
  return {
    name: result.name,
    description: (result.description || '').slice(0, 180),
    author: result.author || '',
    version: result.version || '',
    source: installSource,
    trustLevel: result.trust_level || t('cronSkills.registry.community'),
    installed: Boolean(result.installed),
    lifecycleLabel: presentation?.label || '',
    lifecycleTone: presentation?.tone || 'neutral',
    installId,
    installSource,
    operationKey: skillRegistryOperationKey(installId, installSource),
  }
}))

const queueRows = computed(() => props.queue.map((item) => {
  const lifecycle = item.result?.lifecycle
  const presentation = lifecycle
    ? skillLifecyclePresentation({ name: item.displayName, lifecycle }, 'registry')
    : null
  const resolution = item.result?.resolution
  const resultMeta = [
    item.source,
    resolution?.publisher,
    resolution?.version,
    resolution?.immutableRevision ? resolution.immutableRevision.slice(0, 10) : '',
    effectiveFromLabel(item.result?.effectiveFrom),
    typeof item.result?.catalogGeneration === 'number'
      ? t('cronSkills.registry.catalogGeneration', { generation: item.result.catalogGeneration })
      : '',
  ].filter((value): value is string => Boolean(value))
  return {
    ...item,
    lifecycleLabel: presentation?.label || '',
    lifecycleTone: presentation?.tone || 'neutral',
    diagnostics: item.result?.diagnostics || [],
    resultMeta,
  }
}))

function diagnosticDetails(details: Record<string, unknown>): string {
  try {
    return JSON.stringify(details, null, 2)
  } catch {
    return String(details)
  }
}

function effectiveFromLabel(value: string | undefined): string {
  if (!value) return ''
  if (value === 'next_turn') return t('cronSkills.registry.effectiveNextTurn')
  if (value === 'next_start') return t('cronSkills.registry.effectiveNextStart')
  return t('cronSkills.registry.effectiveFrom', { value })
}
</script>

<style scoped>
.sk-add-overlay {
  --sk-add-scrim: color-mix(in srgb, var(--scrim) 45%, transparent);

  inset: 0;
  position: fixed;
  z-index: 1200;
}

.sk-add-overlay__scrim {
  background: var(--sk-add-scrim);
  inset: 0;
  position: absolute;
}

.sk-add-drawer {
  background: var(--bg-surface);
  border-left: 1px solid var(--border);
  bottom: 0;
  box-shadow: var(--elev-3);
  color: var(--text);
  display: flex;
  flex-direction: column;
  max-width: 100vw;
  position: absolute;
  right: 0;
  top: 0;
  width: 460px;
}

.sk-add-drawer__head {
  align-items: flex-start;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: 24px;
}

.sk-add-drawer__head h2 {
  font-size: 1.25rem;
  margin: 0;
}

.sk-add-drawer__head p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 6px 0 0;
}

.sk-add-drawer__close {
  flex: 0 0 auto;
  padding: 7px;
}

.sk-add-drawer__body {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: var(--sp-4);
  min-height: 0;
  overflow-y: auto;
  padding: 20px 24px 32px;
}

.sk-add-source-tabs {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  grid-template-columns: 1fr 1fr;
  padding: 3px;
}

.sk-add-source-tab {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 650;
  gap: 7px;
  justify-content: center;
  min-height: 36px;
  padding: 7px 10px;
}

.sk-add-source-tab.is-active {
  background: var(--bg-surface);
  border-color: var(--accent);
  color: var(--accent);
}

.sk-add-source-tab:focus-visible,
.sk-add-textarea:focus,
.sk-add-input-wrap:focus-within {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-add-source-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sk-add-field-label {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 650;
}

.sk-add-textarea {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  min-height: 170px;
  padding: 12px;
  resize: vertical;
  width: 100%;
}

.sk-add-textarea:disabled {
  opacity: .65;
}

.sk-add-help {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  margin: 0;
}

.sk-add-help--center {
  text-align: center;
}

.sk-add-primary {
  justify-content: center;
  margin-top: var(--sp-2);
  width: 100%;
}

.sk-add-primary--busy:disabled[aria-busy="true"] {
  opacity: 1;
}

.sk-add-search-row {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: 1fr auto;
}

.sk-add-input-wrap {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-dim);
  display: flex;
  gap: 8px;
  padding: 0 10px;
}

.sk-add-input-wrap input:not([type="radio"]):not([type="checkbox"]) {
  background: transparent;
  border: 0;
  box-shadow: none;
  color: var(--text);
  min-width: 0;
  outline: 0;
  padding: 8px 0;
  width: 100%;
}

.sk-add-callout {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--border));
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-xs);
  gap: 3px;
  padding: 10px 12px;
}

.sk-add-callout--danger {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
}

.sk-add-callout strong {
  color: var(--text);
}

.sk-add-empty {
  align-items: center;
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  justify-content: center;
  min-height: 120px;
  padding: var(--sp-4);
  text-align: center;
}

.sk-add-results,
.sk-add-queue {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sk-add-result {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: 12px;
}

.sk-add-result__body {
  min-width: 0;
}

.sk-add-result__body strong {
  display: block;
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

.sk-add-result__body p {
  color: var(--text-muted);
  display: -webkit-box;
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin: 4px 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.sk-add-result__meta,
.sk-add-queue-item__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.sk-add-result__meta span,
.sk-add-queue-item__meta span,
.sk-add-lifecycle {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 1px 5px;
}

.sk-add-section-title {
  align-items: baseline;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  margin-top: var(--sp-2);
  padding-bottom: 8px;
}

.sk-add-section-title h3 {
  font-size: var(--fs-sm);
  margin: 0;
}

.sk-add-section-title span {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.sk-add-queue-item {
  align-items: flex-start;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: auto 1fr;
  padding: 13px;
}

.sk-add-queue-item[data-status="failed"] {
  background: color-mix(in srgb, var(--danger) 6%, var(--bg));
  border-color: color-mix(in srgb, var(--danger) 32%, var(--border));
}

.sk-add-queue-item[data-status="installed"] .sk-add-queue-item__icon,
.sk-add-queue-item[data-status="unchanged"] .sk-add-queue-item__icon {
  color: var(--ok);
}

.sk-add-queue-item[data-status="failed"] .sk-add-queue-item__icon,
.sk-add-queue-item[data-status="failed"] .sk-add-queue-item__head span,
.sk-add-queue-item__error {
  color: var(--danger);
}

.sk-add-queue-item__icon {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  height: 24px;
  justify-content: center;
  width: 24px;
}

.sk-add-queue-item__body {
  min-width: 0;
}

.sk-add-queue-item__head {
  align-items: baseline;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.sk-add-queue-item__head strong {
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

.sk-add-queue-item__head span {
  color: var(--text-muted);
  flex: 0 0 auto;
  font-size: var(--fs-xs);
}

.sk-add-queue-item__body > code {
  color: var(--text-dim);
  display: block;
  font-size: 10px;
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sk-add-queue-item__error {
  font-size: var(--fs-xs);
  margin: 7px 0;
}

.sk-add-queue-item__meta,
.sk-add-lifecycle {
  margin-top: 7px;
}

.sk-add-lifecycle[data-tone="success"] { border-color: color-mix(in srgb, var(--ok) 45%, var(--border)); color: var(--ok); }
.sk-add-lifecycle[data-tone="info"] { border-color: color-mix(in srgb, var(--info) 45%, var(--border)); color: var(--info); }
.sk-add-lifecycle[data-tone="warning"] { border-color: color-mix(in srgb, var(--warn) 45%, var(--border)); color: var(--warn); }
.sk-add-lifecycle[data-tone="danger"] { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); color: var(--danger); }

.sk-add-diagnostics {
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin-top: 9px;
  padding-top: 7px;
}

.sk-add-diagnostics summary {
  color: var(--text);
  cursor: pointer;
}

.sk-add-diagnostics div {
  margin-top: 8px;
}

.sk-add-diagnostics p {
  margin: 3px 0;
}

.sk-add-diagnostics pre {
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
  font-size: 10px;
  margin: 5px 0 0;
  max-height: 160px;
  overflow: auto;
  padding: 7px;
  white-space: pre-wrap;
}

.sk-add-retry {
  margin-top: 8px;
}

.sk-add-drawer-enter-active,
.sk-add-drawer-leave-active {
  transition: opacity var(--dur-base) var(--ease-standard);
}

.sk-add-drawer-enter-active .sk-add-drawer,
.sk-add-drawer-leave-active .sk-add-drawer {
  transition: transform var(--dur-base) var(--ease-standard);
}

.sk-add-drawer-enter-from,
.sk-add-drawer-leave-to {
  opacity: 0;
}

.sk-add-drawer-enter-from .sk-add-drawer,
.sk-add-drawer-leave-to .sk-add-drawer {
  transform: translateX(100%);
}

@media (max-width: 720px) {
  .sk-add-drawer {
    border-left: 0;
    height: 100dvh;
    width: 100vw;
  }

  .sk-add-drawer__head {
    padding: 18px;
  }

  .sk-add-drawer__body {
    padding: 16px 18px 28px;
  }
}
</style>

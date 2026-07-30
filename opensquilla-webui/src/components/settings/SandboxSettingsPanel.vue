<template>
  <section class="sandbox-settings" aria-labelledby="sandbox-settings-title">
    <header class="sandbox-settings__header">
      <div>
        <p class="sandbox-settings__eyebrow">{{ t('settings.sandbox.eyebrow') }}</p>
        <h3 id="sandbox-settings-title">{{ t('settings.sandbox.title') }}</h3>
        <p>{{ t('settings.sandbox.subtitle') }}</p>
      </div>
      <span
        v-if="capability"
        class="sandbox-settings__status"
        :class="{ 'is-ready': capability.available }"
      >
        {{ capability.available ? t('settings.sandbox.available') : t('settings.sandbox.unavailable') }}
      </span>
    </header>

    <div v-if="loading" class="sandbox-settings__state" role="status">
      {{ t('shared.loading') }}
    </div>
    <div v-else-if="loadError" class="sandbox-settings__state" role="alert">
      <span>{{ loadError }}</span>
      <button type="button" class="btn" @click="load">{{ t('settings.sandbox.actions.retry') }}</button>
    </div>

    <template v-else-if="draft">
      <article class="sandbox-card">
        <div class="sandbox-card__head">
          <div>
            <h4>{{ t('settings.sandbox.files.title') }}</h4>
            <p>{{ t('settings.sandbox.files.description') }}</p>
          </div>
          <span class="sandbox-card__tag">{{ t('settings.sandbox.files.readsAllowed') }}</span>
        </div>

        <div class="sandbox-rule-list" data-testid="builtin-file-rules">
          <div v-for="path in builtinDenyWritePaths" :key="path" class="sandbox-rule">
            <code>{{ path }}</code>
            <span>{{ t('settings.sandbox.builtin') }}</span>
          </div>
          <div
            v-for="(_path, index) in draft.files.customDenyWritePaths"
            :key="`custom-${index}`"
            class="sandbox-rule sandbox-rule--editable"
          >
            <input v-model="draft.files.customDenyWritePaths[index]" :aria-label="t('settings.sandbox.files.customPath')" />
            <button type="button" class="btn btn--ghost" @click="removeAt(draft.files.customDenyWritePaths, index)">
              {{ t('settings.sandbox.actions.remove') }}
            </button>
          </div>
        </div>
        <div class="sandbox-inline-form">
          <input
            v-model="newFilePath"
            :placeholder="t('settings.sandbox.files.pathPlaceholder')"
            @keydown.enter.prevent="addTextRule(draft.files.customDenyWritePaths, newFilePath, value => { newFilePath = value })"
          />
          <button
            type="button"
            class="btn"
            @click="addTextRule(draft.files.customDenyWritePaths, newFilePath, value => { newFilePath = value })"
          >
            {{ t('settings.sandbox.actions.add') }}
          </button>
        </div>

        <div class="sandbox-option">
          <div>
            <strong>{{ t('settings.sandbox.files.backupTitle') }}</strong>
            <p>{{ t('settings.sandbox.files.backupDescription') }}</p>
          </div>
          <label class="sandbox-switch">
            <input v-model="draft.files.recursiveDeleteBackupEnabled" type="checkbox" />
            <span aria-hidden="true"></span>
          </label>
        </div>
        <label class="sandbox-field sandbox-field--compact">
          <span>{{ t('settings.sandbox.files.quota') }}</span>
          <input v-model.number="backupQuotaGiB" type="number" min="0.1" step="0.5" />
          <span>GiB</span>
        </label>
        <p class="sandbox-warning">{{ t('settings.sandbox.files.recursiveWarning') }}</p>
        <SectionActions
          :dirty="sectionDirty('files')"
          :pending="sectionPending.files"
          :error="sectionError.files"
          @save="void saveSection('files')"
          @discard="discardSection('files')"
        />
      </article>

      <article class="sandbox-card">
        <div class="sandbox-card__head">
          <div>
            <h4>{{ t('settings.sandbox.commands.title') }}</h4>
            <p>{{ t('settings.sandbox.commands.description') }}</p>
          </div>
        </div>
        <label class="sandbox-field">
          <span>{{ t('settings.sandbox.commands.systemTools') }}</span>
          <select v-model="draft.commands.systemTools">
            <option value="auto">{{ t('settings.sandbox.commands.systemToolsAuto') }}</option>
            <option value="prompt">{{ t('settings.sandbox.commands.systemToolsPrompt') }}</option>
            <option value="disabled">{{ t('settings.sandbox.commands.systemToolsDisabled') }}</option>
          </select>
        </label>

        <RuleEditor
          v-model="approvalPrefix"
          :title="t('settings.sandbox.commands.approvalPrefixes')"
          :placeholder="t('settings.sandbox.commands.prefixPlaceholder')"
          :rules="draft.commands.requireApprovalPrefixes"
          @add="addPrefix(draft.commands.requireApprovalPrefixes, approvalPrefix, value => { approvalPrefix = value })"
          @remove="removeAt(draft.commands.requireApprovalPrefixes, $event)"
        />
        <RuleEditor
          v-model="autoPrefix"
          :title="t('settings.sandbox.commands.autoPrefixes')"
          :placeholder="t('settings.sandbox.commands.prefixPlaceholder')"
          :rules="draft.commands.autoAllowPrefixes"
          @add="addPrefix(draft.commands.autoAllowPrefixes, autoPrefix, value => { autoPrefix = value })"
          @remove="removeAt(draft.commands.autoAllowPrefixes, $event)"
        />
        <SectionActions
          :dirty="sectionDirty('commands')"
          :pending="sectionPending.commands"
          :error="sectionError.commands"
          @save="void saveSection('commands')"
          @discard="discardSection('commands')"
        />
      </article>

      <article class="sandbox-card">
        <div class="sandbox-card__head">
          <div>
            <h4>{{ t('settings.sandbox.network.title') }}</h4>
            <p>{{ t('settings.sandbox.network.description') }}</p>
          </div>
        </div>
        <div class="sandbox-option">
          <div>
            <strong>{{ t('settings.sandbox.network.blockAll') }}</strong>
            <p>{{ t('settings.sandbox.network.blockAllDescription') }}</p>
          </div>
          <label class="sandbox-switch">
            <input v-model="draft.network.blockAllNetwork" type="checkbox" />
            <span aria-hidden="true"></span>
          </label>
        </div>
        <TextRuleEditor
          v-model="allowDomain"
          :title="t('settings.sandbox.network.allowDomains')"
          placeholder="api.example.com"
          :rules="draft.network.allowDomains"
          @add="addTextRule(draft.network.allowDomains, allowDomain, value => { allowDomain = value })"
          @remove="removeAt(draft.network.allowDomains, $event)"
        />
        <TextRuleEditor
          v-model="denyDomain"
          :title="t('settings.sandbox.network.denyDomains')"
          placeholder="telemetry.example.com"
          :rules="draft.network.denyDomains"
          @add="addTextRule(draft.network.denyDomains, denyDomain, value => { denyDomain = value })"
          @remove="removeAt(draft.network.denyDomains, $event)"
        />
        <SectionActions
          :dirty="sectionDirty('network')"
          :pending="sectionPending.network"
          :error="sectionError.network"
          @save="void saveSection('network')"
          @discard="discardSection('network')"
        />
      </article>

      <article class="sandbox-card">
        <div class="sandbox-card__head">
          <div>
            <h4>{{ t('settings.sandbox.runtimes.title') }}</h4>
            <p>{{ t('settings.sandbox.runtimes.description') }}</p>
          </div>
          <label class="sandbox-switch">
            <input v-model="draft.runtimes.enabled" type="checkbox" />
            <span aria-hidden="true"></span>
          </label>
        </div>
        <div class="sandbox-runtime-grid">
          <label><span>Python</span><input v-model="draft.runtimes.python" type="checkbox" :disabled="!draft.runtimes.enabled" /></label>
          <label><span>Node.js</span><input v-model="draft.runtimes.node" type="checkbox" :disabled="!draft.runtimes.enabled" /></label>
          <label><span>Git Bash</span><input v-model="draft.runtimes.gitBash" type="checkbox" :disabled="!draft.runtimes.enabled" /></label>
        </div>
        <SectionActions
          :dirty="sectionDirty('runtimes')"
          :pending="sectionPending.runtimes"
          :error="sectionError.runtimes"
          @save="void saveSection('runtimes')"
          @discard="discardSection('runtimes')"
        />
      </article>

      <article class="sandbox-card">
        <div class="sandbox-card__head">
          <div>
            <h4>{{ t('settings.sandbox.lan.title') }}</h4>
            <p>{{ t('settings.sandbox.lan.description') }}</p>
          </div>
        </div>
        <div class="sandbox-lan-rules">
          <p><strong>{{ t('settings.sandbox.lan.guest') }}</strong>{{ t('settings.sandbox.lan.guestDescription') }}</p>
          <p><strong>{{ t('settings.sandbox.lan.authenticated') }}</strong>{{ t('settings.sandbox.lan.authenticatedDescription') }}</p>
        </div>
        <div class="sandbox-token-create">
          <input v-model="tokenName" :placeholder="t('settings.sandbox.lan.tokenName')" />
          <label>
            <input v-model="tokenHostExecute" type="checkbox" />
            {{ t('settings.sandbox.lan.hostExecute') }}
          </label>
          <button
            type="button"
            class="btn btn--primary"
            :disabled="tokenPending || !tokenName.trim()"
            data-testid="create-sandbox-token"
            @click="createNamedToken"
          >
            {{ t('settings.sandbox.lan.createToken') }}
          </button>
        </div>
        <div v-if="revealedToken" class="sandbox-token-secret" data-testid="revealed-sandbox-token">
          <strong>{{ t('settings.sandbox.lan.copyNow') }}</strong>
          <code>{{ revealedToken }}</code>
          <button type="button" class="btn" @click="copyToken">{{ t('settings.sandbox.actions.copy') }}</button>
        </div>
        <div class="sandbox-token-list">
          <div v-for="token in tokens" :key="token.publicId" class="sandbox-token-row">
            <div>
              <strong>{{ token.name }}</strong>
              <code>{{ token.publicId }}</code>
              <small>{{ token.capabilities.includes('host.execute') ? t('settings.sandbox.lan.fullCapable') : t('settings.sandbox.lan.safeOnly') }}</small>
            </div>
            <button type="button" class="btn btn--ghost" :disabled="tokenPending" @click="void revokeToken(token.publicId)">
              {{ t('settings.sandbox.lan.revoke') }}
            </button>
          </div>
        </div>
        <p v-if="tokenError" class="sandbox-error" role="alert">{{ tokenError }}</p>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { useSandboxSettings } from '@/composables/settings/useSandboxSettings'

const { t } = useI18n()
const {
  loading,
  loadError,
  capability,
  draft,
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
} = useSandboxSettings()

const newFilePath = ref('')
const approvalPrefix = ref('')
const autoPrefix = ref('')
const allowDomain = ref('')
const denyDomain = ref('')
const tokenName = ref('')
const tokenHostExecute = ref(true)

const backupQuotaGiB = computed({
  get: () => Number(((draft.value?.files.backupQuotaBytes ?? 3 * 1024 ** 3) / 1024 ** 3).toFixed(2)),
  set: (value: number) => {
    if (!draft.value || !Number.isFinite(value)) return
    draft.value.files.backupQuotaBytes = Math.max(1, Math.round(value * 1024 ** 3))
  },
})

function removeAt<T>(values: T[], index: number): void {
  values.splice(index, 1)
}

function addTextRule(values: string[], raw: string, clear: (value: string) => void): void {
  const value = raw.trim()
  if (value && !values.includes(value)) values.push(value)
  clear('')
}

function addPrefix(values: string[][], raw: string, clear: (value: string) => void): void {
  const prefix = raw.trim().split(/\s+/).filter(Boolean)
  if (prefix.length && !values.some(value => JSON.stringify(value) === JSON.stringify(prefix))) {
    values.push(prefix)
  }
  clear('')
}

async function createNamedToken(): Promise<void> {
  const name = tokenName.value.trim()
  if (!name) return
  try {
    await createToken(name, tokenHostExecute.value)
    tokenName.value = ''
  } catch {
    // The composable exposes the error next to the token controls.
  }
}

async function copyToken(): Promise<void> {
  if (!revealedToken.value) return
  await navigator.clipboard?.writeText(revealedToken.value)
}

const SectionActions = defineComponent({
  props: {
    dirty: { type: Boolean, required: true },
    pending: { type: Boolean, required: true },
    error: { type: String, required: true },
  },
  emits: ['save', 'discard'],
  setup(props, { emit }) {
    return () => h('div', { class: 'sandbox-actions' }, [
      props.error ? h('p', { class: 'sandbox-error', role: 'alert' }, props.error) : null,
      h('span', { class: 'sandbox-actions__spacer' }),
      h('button', {
        type: 'button',
        class: 'btn',
        disabled: !props.dirty || props.pending,
        onClick: () => emit('discard'),
      }, t('common.discard')),
      h('button', {
        type: 'button',
        class: 'btn btn--primary',
        disabled: !props.dirty || props.pending,
        'data-testid': 'save-sandbox-section',
        onClick: () => emit('save'),
      }, props.pending ? t('settings.sandbox.actions.saving') : t('common.save')),
    ])
  },
})

const RuleEditor = defineComponent({
  props: {
    modelValue: { type: String, required: true },
    title: { type: String, required: true },
    placeholder: { type: String, required: true },
    rules: { type: Array as () => string[][], required: true },
  },
  emits: ['update:modelValue', 'add', 'remove'],
  setup(props, { emit }) {
    return () => h('div', { class: 'sandbox-editor' }, [
      h('strong', props.title),
      ...props.rules.map((rule, index) => h('div', { class: 'sandbox-rule sandbox-rule--editable' }, [
        h('code', rule.join(' ')),
        h('button', { type: 'button', class: 'btn btn--ghost', onClick: () => emit('remove', index) }, t('settings.sandbox.actions.remove')),
      ])),
      h('div', { class: 'sandbox-inline-form' }, [
        h('input', {
          value: props.modelValue,
          placeholder: props.placeholder,
          onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLInputElement).value),
          onKeydown: (event: KeyboardEvent) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              emit('add')
            }
          },
        }),
        h('button', { type: 'button', class: 'btn', onClick: () => emit('add') }, t('settings.sandbox.actions.add')),
      ]),
    ])
  },
})

const TextRuleEditor = defineComponent({
  props: {
    modelValue: { type: String, required: true },
    title: { type: String, required: true },
    placeholder: { type: String, required: true },
    rules: { type: Array as () => string[], required: true },
  },
  emits: ['update:modelValue', 'add', 'remove'],
  setup(props, { emit }) {
    return () => h('div', { class: 'sandbox-editor' }, [
      h('strong', props.title),
      ...props.rules.map((rule, index) => h('div', { class: 'sandbox-rule sandbox-rule--editable' }, [
        h('code', rule),
        h('button', { type: 'button', class: 'btn btn--ghost', onClick: () => emit('remove', index) }, t('settings.sandbox.actions.remove')),
      ])),
      h('div', { class: 'sandbox-inline-form' }, [
        h('input', {
          value: props.modelValue,
          placeholder: props.placeholder,
          onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLInputElement).value),
          onKeydown: (event: KeyboardEvent) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              emit('add')
            }
          },
        }),
        h('button', { type: 'button', class: 'btn', onClick: () => emit('add') }, t('settings.sandbox.actions.add')),
      ]),
    ])
  },
})

onMounted(() => void load())
</script>

<style scoped>
.sandbox-settings {
  display: grid;
  gap: 1rem;
  max-width: 920px;
  margin: 0 auto;
  padding-bottom: 1rem;
}

.sandbox-settings__header,
.sandbox-card__head,
.sandbox-option,
.sandbox-actions,
.sandbox-token-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.sandbox-settings__eyebrow {
  margin: 0 0 0.25rem;
  color: var(--accent);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.sandbox-settings h3,
.sandbox-settings h4,
.sandbox-settings p {
  margin: 0;
}

.sandbox-settings__header p:last-child,
.sandbox-card__head p,
.sandbox-option p {
  margin-top: 0.3rem;
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.45;
}

.sandbox-settings__status,
.sandbox-card__tag,
.sandbox-rule > span {
  flex: 0 0 auto;
  padding: 0.2rem 0.55rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  font-size: 0.7rem;
}

.sandbox-settings__status.is-ready {
  border-color: color-mix(in srgb, var(--ok) 45%, var(--border));
  color: var(--ok);
}

.sandbox-settings__state,
.sandbox-card {
  padding: 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
}

.sandbox-card {
  display: grid;
  gap: 0.9rem;
}

.sandbox-rule-list,
.sandbox-token-list,
.sandbox-lan-rules,
.sandbox-editor {
  display: grid;
  gap: 0.45rem;
}

.sandbox-rule {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  min-height: 36px;
  padding: 0.4rem 0.55rem;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
}

.sandbox-rule code,
.sandbox-token-secret code {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--text);
  font-size: 0.74rem;
}

.sandbox-rule > code,
.sandbox-rule > input {
  flex: 1;
}

.sandbox-inline-form,
.sandbox-token-create,
.sandbox-token-secret,
.sandbox-field,
.sandbox-runtime-grid {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.sandbox-inline-form input,
.sandbox-token-create > input,
.sandbox-rule input,
.sandbox-field select {
  flex: 1;
  min-width: 0;
}

.sandbox-switch {
  position: relative;
  display: inline-flex;
}

.sandbox-switch input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.sandbox-switch span {
  width: 38px;
  height: 22px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  cursor: pointer;
}

.sandbox-switch span::after {
  display: block;
  width: 16px;
  height: 16px;
  margin: 2px;
  border-radius: var(--radius-full);
  background: var(--text-muted);
  content: '';
  transition: transform var(--dur-fast) var(--ease-standard);
}

.sandbox-switch input:checked + span {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 20%, var(--bg-surface));
}

.sandbox-switch input:checked + span::after {
  background: var(--accent);
  transform: translateX(16px);
}

.sandbox-field {
  justify-content: flex-start;
}

.sandbox-field--compact input {
  width: 100px;
}

.sandbox-warning {
  padding: 0.65rem 0.75rem;
  border-inline-start: 3px solid var(--warn);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  color: var(--text-muted);
  font-size: 0.76rem;
  line-height: 1.45;
}

.sandbox-runtime-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.sandbox-runtime-grid label {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.7rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.sandbox-actions__spacer {
  flex: 1;
}

.sandbox-error {
  color: var(--danger);
  font-size: 0.75rem;
}

.sandbox-lan-rules p {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.sandbox-lan-rules strong {
  margin-inline-end: 0.4rem;
  color: var(--text);
}

.sandbox-token-create {
  flex-wrap: wrap;
}

.sandbox-token-secret {
  align-items: flex-start;
  padding: 0.75rem;
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-surface));
}

.sandbox-token-secret code {
  flex: 1;
  user-select: all;
}

.sandbox-token-row > div {
  display: grid;
  gap: 0.2rem;
}

.sandbox-token-row small {
  color: var(--text-muted);
}

@media (max-width: 720px) {
  .sandbox-settings__header,
  .sandbox-card__head,
  .sandbox-option {
    align-items: flex-start;
  }

  .sandbox-runtime-grid {
    grid-template-columns: 1fr;
  }

  .sandbox-token-create,
  .sandbox-inline-form {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>

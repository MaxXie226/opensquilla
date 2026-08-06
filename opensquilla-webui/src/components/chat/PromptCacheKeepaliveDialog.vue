<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="keepalive-overlay" @click="close">
        <section
          ref="dialogRef"
          class="keepalive-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="prompt-cache-keepalive-title"
          @click.stop
        >
          <header class="keepalive-dialog__header">
            <div>
              <h2 id="prompt-cache-keepalive-title">{{ t('chat.promptCacheKeepalive.title') }}</h2>
              <p>{{ t('chat.promptCacheKeepalive.description') }}</p>
            </div>
            <button
              ref="closeButtonRef"
              type="button"
              class="keepalive-dialog__close"
              :aria-label="t('common.close')"
              @click="close"
            >
              <Icon name="x" :size="18" />
            </button>
          </header>

          <div class="keepalive-dialog__body">
            <p v-if="loading" class="keepalive-dialog__muted">{{ t('chat.loadingSession') }}</p>
            <template v-else>
              <label class="keepalive-dialog__toggle">
                <input v-model="draftEnabled" type="checkbox" />
                <span>
                  <strong>{{ t('chat.promptCacheKeepalive.enable') }}</strong>
                  <small>{{ t('chat.promptCacheKeepalive.enableHint') }}</small>
                </span>
              </label>

              <div class="keepalive-dialog__timing" :class="{ 'is-disabled': !draftEnabled }">
                <label class="keepalive-dialog__field">
                  <strong>{{ t('chat.promptCacheKeepalive.ttlMinutes') }}</strong>
                  <span class="keepalive-dialog__input-wrap">
                    <input
                      v-model.number="draftTtlMinutes"
                      data-testid="prompt-cache-keepalive-ttl"
                      type="number"
                      min="5"
                      max="1440"
                      step="1"
                      :disabled="!draftEnabled"
                    />
                    <span>{{ t('chat.promptCacheKeepalive.minutesUnit') }}</span>
                  </span>
                  <small>{{ t('chat.promptCacheKeepalive.ttlHint') }}</small>
                </label>

                <label class="keepalive-dialog__field">
                  <strong>{{ t('chat.promptCacheKeepalive.idleTimeoutMinutes') }}</strong>
                  <span class="keepalive-dialog__input-wrap">
                    <input
                      v-model.number="draftIdleTimeoutMinutes"
                      data-testid="prompt-cache-keepalive-idle-timeout"
                      type="number"
                      min="5"
                      max="1440"
                      step="1"
                      :disabled="!draftEnabled"
                    />
                    <span>{{ t('chat.promptCacheKeepalive.minutesUnit') }}</span>
                  </span>
                  <small>{{ t('chat.promptCacheKeepalive.idleTimeoutHint') }}</small>
                </label>
              </div>

              <p v-if="draftEnabled && !validIdleTimeout" class="keepalive-dialog__validation" role="alert">
                {{ t('chat.promptCacheKeepalive.idleTimeoutInvalid', { minutes: probeMinutes }) }}
              </p>

              <div
                v-if="!draftEnabled || validConfig"
                class="keepalive-dialog__plan"
                :class="{ 'is-disabled': !draftEnabled }"
                role="note"
              >
                <Icon name="clock" :size="17" />
                <span>
                  <strong>{{ t('chat.promptCacheKeepalive.planTitle') }}</strong>
                  <small>{{ t('chat.promptCacheKeepalive.planSummary', {
                    interval: probeMinutes,
                    duration: draftIdleTimeoutMinutes,
                    count: estimatedProbeCount,
                  }) }}</small>
                </span>
              </div>

              <div class="keepalive-dialog__warning" role="note">
                <Icon name="info" :size="17" />
                <span>{{ t('chat.promptCacheKeepalive.costWarning') }}</span>
              </div>

              <dl v-if="status" class="keepalive-dialog__status">
                <div>
                  <dt>{{ t('chat.promptCacheKeepalive.statusLabel') }}</dt>
                  <dd>{{ statusText }}</dd>
                </div>
                <div v-if="status.provider || status.model">
                  <dt>{{ t('chat.promptCacheKeepalive.targetLabel') }}</dt>
                  <dd>{{ [status.provider, status.model].filter(Boolean).join(' / ') }}</dd>
                </div>
                <div v-if="status.lastCacheHitTokens > 0">
                  <dt>{{ t('chat.promptCacheKeepalive.lastHitLabel') }}</dt>
                  <dd>{{ status.lastCacheHitTokens }}</dd>
                </div>
                <div v-if="status.idleExpiresAt">
                  <dt>{{ t('chat.promptCacheKeepalive.autoPauseLabel') }}</dt>
                  <dd>{{ formatTime(status.idleExpiresAt) }}</dd>
                </div>
              </dl>
            </template>
            <p v-if="error" class="keepalive-dialog__error" role="alert">{{ error }}</p>
          </div>

          <footer class="keepalive-dialog__footer">
            <button type="button" class="btn btn--ghost" :disabled="saving" @click="close">
              {{ t('common.cancel') }}
            </button>
            <button
              type="button"
              class="btn btn--primary"
              :disabled="loading || saving || !validConfig"
              @click="save"
            >
              {{ saving ? t('chat.saving') : t('common.save') }}
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { useRpcStore } from '@/stores/rpc'

interface KeepaliveStatus {
  enabled: boolean
  ttlSeconds: number
  intervalSeconds: number
  idleTimeoutSeconds?: number
  idleExpiresAt?: number | null
  state: string
  reason?: string | null
  hasSnapshot: boolean
  lastCacheHitTokens: number
  provider?: string | null
  model?: string | null
}

const props = defineProps<{ open: boolean; sessionKey: string }>()
const emit = defineEmits<{ close: [] }>()
const { t } = useI18n()
const rpc = useRpcStore()
const dialogRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLElement | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const status = ref<KeepaliveStatus | null>(null)
const draftEnabled = ref(false)
const draftTtlMinutes = ref(5)
const draftIdleTimeoutMinutes = ref(60)

const validTtl = computed(() => (
  Number.isInteger(draftTtlMinutes.value)
  && draftTtlMinutes.value >= 5
  && draftTtlMinutes.value <= 1440
))
const probeMinutes = computed(() => Math.max(1, Math.round(draftTtlMinutes.value * 0.8)))
const validIdleTimeout = computed(() => (
  Number.isInteger(draftIdleTimeoutMinutes.value)
  && draftIdleTimeoutMinutes.value >= 5
  && draftIdleTimeoutMinutes.value <= 1440
  && draftIdleTimeoutMinutes.value * 60 > draftTtlMinutes.value * 60 * 0.8
))
const validConfig = computed(() => !draftEnabled.value || (
  validTtl.value && validIdleTimeout.value
))
const estimatedProbeCount = computed(() => {
  if (!validTtl.value || !Number.isFinite(draftIdleTimeoutMinutes.value)) return 0
  const intervalSeconds = draftTtlMinutes.value * 60 * 0.8
  return Math.max(0, Math.floor((draftIdleTimeoutMinutes.value * 60 - 1) / intervalSeconds))
})
const statusText = computed(() => {
  if (!status.value) return ''
  const key = `chat.promptCacheKeepalive.states.${status.value.state}`
  const translated = t(key)
  return translated === key ? status.value.state : translated
})

function close() {
  if (!saving.value) emit('close')
}

function formatTime(value: number): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

async function load() {
  if (!props.open || !props.sessionKey) return
  loading.value = true
  error.value = ''
  try {
    const next = await rpc.call<KeepaliveStatus>(
      'sessions.promptCacheKeepalive.status',
      { key: props.sessionKey },
    )
    status.value = next
    draftEnabled.value = next.enabled
    draftTtlMinutes.value = Math.max(5, Math.round(next.ttlSeconds / 60))
    draftIdleTimeoutMinutes.value = Math.max(
      5,
      Math.round((next.idleTimeoutSeconds || 3_600) / 60),
    )
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause)
    error.value = /session not found/i.test(detail)
      ? t('chat.promptCacheKeepalive.sessionUnavailable')
      : detail
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!validConfig.value) return
  saving.value = true
  error.value = ''
  try {
    const ttlSeconds = Number.isInteger(draftTtlMinutes.value)
      ? Math.round(draftTtlMinutes.value * 60)
      : (status.value?.ttlSeconds || 300)
    const idleTimeoutSeconds = Number.isInteger(draftIdleTimeoutMinutes.value)
      ? Math.round(draftIdleTimeoutMinutes.value * 60)
      : (status.value?.idleTimeoutSeconds || 3_600)
    status.value = await rpc.call<KeepaliveStatus>(
      'sessions.promptCacheKeepalive.set',
      {
        key: props.sessionKey,
        enabled: draftEnabled.value,
        ttlSeconds,
        idleTimeoutSeconds,
      },
    )
    emit('close')
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause)
    error.value = /session not found/i.test(detail)
      ? t('chat.promptCacheKeepalive.sessionUnavailable')
      : detail
  } finally {
    saving.value = false
  }
}

watch(() => [props.open, props.sessionKey] as const, ([isOpen]) => {
  if (isOpen) void nextTick(load)
}, { immediate: true })

useDialogA11y(dialogRef, computed(() => props.open), close, {
  initialFocus: closeButtonRef,
})
</script>

<style scoped>
.keepalive-overlay {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: var(--sp-4);
  position: fixed;
  z-index: 1100;
}

.keepalive-dialog {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
  max-width: 520px;
  width: 100%;
}

.keepalive-dialog__header,
.keepalive-dialog__footer {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-4) var(--sp-5);
}

.keepalive-dialog__header { border-bottom: 1px solid var(--border); }
.keepalive-dialog__footer { border-top: 1px solid var(--border); justify-content: flex-end; }
.keepalive-dialog__header h2 { font-size: var(--fs-lg); margin: 0; }
.keepalive-dialog__header p { color: var(--text-muted); font-size: var(--fs-sm); margin: var(--sp-1) 0 0; }
.keepalive-dialog__close { background: none; border: 0; color: var(--text-muted); cursor: pointer; padding: var(--sp-1); }
.keepalive-dialog__body { display: grid; gap: var(--sp-4); padding: var(--sp-5); }
.keepalive-dialog__toggle { align-items: flex-start; background: var(--bg-surface-2); border: 1px solid var(--border); border-radius: var(--radius-md); display: flex; gap: var(--sp-3); padding: var(--sp-3); }
.keepalive-dialog__toggle input { margin-top: 3px; }
.keepalive-dialog__toggle span,
.keepalive-dialog__toggle small { display: block; }
.keepalive-dialog__toggle small,
.keepalive-dialog__field small,
.keepalive-dialog__muted { color: var(--text-muted); font-size: var(--fs-xs); }
.keepalive-dialog__timing { display: grid; gap: var(--sp-3); grid-template-columns: repeat(2, minmax(0, 1fr)); }
.keepalive-dialog__timing.is-disabled,
.keepalive-dialog__plan.is-disabled { opacity: var(--state-disabled-opacity); }
.keepalive-dialog__field { display: grid; gap: var(--sp-2); min-width: 0; }
.keepalive-dialog__field strong { font-size: var(--fs-sm); font-weight: 600; }
.keepalive-dialog__input-wrap { align-items: center; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); display: flex; overflow: hidden; }
.keepalive-dialog__input-wrap input { background: transparent; border: 0; border-radius: 0; color: var(--text); min-width: 0; padding: 8px 10px; width: 100%; }
.keepalive-dialog__input-wrap input:focus { box-shadow: none; }
.keepalive-dialog__input-wrap:focus-within { border-color: var(--accent); box-shadow: var(--focus-ring); }
.keepalive-dialog__input-wrap > span { color: var(--text-dim); flex: 0 0 auto; font-size: var(--fs-xs); padding-right: var(--sp-3); }
.keepalive-dialog__plan { align-items: flex-start; background: var(--bg-surface-2); border: 1px solid var(--border); border-radius: var(--radius-md); color: var(--text-muted); display: flex; gap: var(--sp-2); padding: var(--sp-3); }
.keepalive-dialog__plan > span { display: grid; gap: var(--sp-1); }
.keepalive-dialog__plan strong { color: var(--text); font-size: var(--fs-sm); }
.keepalive-dialog__plan small { color: var(--text-muted); font-size: var(--fs-xs); }
.keepalive-dialog__warning { align-items: flex-start; background: color-mix(in srgb, var(--warn) 10%, transparent); border-radius: var(--radius-sm); color: var(--text-muted); display: flex; font-size: var(--fs-sm); gap: var(--sp-2); padding: var(--sp-3); }
.keepalive-dialog__validation { color: var(--danger); font-size: var(--fs-xs); margin: calc(var(--sp-2) * -1) 0 0; }
.keepalive-dialog__status { display: grid; gap: var(--sp-2); margin: 0; }
.keepalive-dialog__status div { display: flex; gap: var(--sp-3); justify-content: space-between; }
.keepalive-dialog__status dt { color: var(--text-muted); }
.keepalive-dialog__status dd { margin: 0; text-align: right; }
.keepalive-dialog__error { color: var(--danger); font-size: var(--fs-sm); margin: 0; }
.modal-enter-active, .modal-leave-active { transition: opacity var(--dur-base); }
.modal-enter-from, .modal-leave-to { opacity: 0; }

@media (max-width: 560px) {
  .keepalive-dialog__timing { grid-template-columns: 1fr; }
}
</style>

<script setup lang="ts">
import { computed } from 'vue'

import type { UiButtonSize, UiButtonVariant } from './button.js'

const props = withDefaults(defineProps<{
  variant?: UiButtonVariant
  size?: UiButtonSize
  type?: 'button' | 'submit' | 'reset'
  disabled?: boolean
  busy?: boolean
  block?: boolean
}>(), {
  variant: 'secondary',
  size: 'medium',
  type: 'button',
  disabled: false,
  busy: false,
  block: false,
})

const inert = computed(() => props.disabled || props.busy)
</script>

<template>
  <button
    :class="[
      'osq-button',
      `osq-button--${variant}`,
      `osq-button--${size}`,
      { 'osq-button--block': block },
    ]"
    :type="type"
    :disabled="inert"
    :aria-busy="busy || undefined"
  >
    <span v-if="busy" class="osq-button__spinner" aria-hidden="true" />
    <span class="osq-button__content"><slot /></span>
  </button>
</template>

<style>
.osq-button {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  color: var(--text);
  cursor: pointer;
  display: inline-flex;
  font-family: var(--font-sans);
  font-weight: 600;
  gap: var(--sp-2);
  justify-content: center;
  line-height: 1;
  transition:
    background var(--transition),
    border-color var(--transition),
    color var(--transition),
    transform var(--transition);
}

.osq-button:not(:disabled):hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
}

.osq-button:not(:disabled):active {
  transform: translateY(0.5px);
}

.osq-button:focus-visible {
  box-shadow: var(--focus-ring);
  outline: none;
}

.osq-button:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.osq-button--primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-foreground);
}

.osq-button--primary:not(:disabled):hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}

.osq-button--ghost {
  background: transparent;
  border-color: transparent;
  color: var(--text-muted);
}

.osq-button--danger {
  background: var(--danger);
  border-color: var(--danger);
  color: var(--accent-foreground);
}

.osq-button--small {
  font-size: var(--fs-xs);
  min-height: 30px;
  padding: 0 var(--sp-3);
}

.osq-button--medium {
  font-size: var(--fs-sm);
  min-height: 36px;
  padding: 0 var(--sp-4);
}

.osq-button--large {
  font-size: var(--fs-md);
  min-height: 44px;
  padding: 0 var(--sp-5);
}

.osq-button--block {
  width: 100%;
}

.osq-button__spinner {
  animation: osq-button-spin var(--dur-pulse) linear infinite;
  border: 2px solid currentColor;
  border-radius: var(--radius-pill);
  border-top-color: transparent;
  height: 1em;
  width: 1em;
}

@keyframes osq-button-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .osq-button,
  .osq-button__spinner {
    animation: none;
    transition: none;
  }
}
</style>

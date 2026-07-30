<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  checked?: boolean
  disabled?: boolean
  busy?: boolean
  label?: string
  caption?: string
  ariaLabel?: string
  name?: string
  id?: string
}>(), {
  checked: false,
  disabled: false,
  busy: false,
})

const emit = defineEmits<{
  change: [boolean]
  'update:checked': [boolean]
}>()

const inert = computed(() => props.disabled || props.busy)

function setValue(value: boolean): void {
  emit('change', value)
  emit('update:checked', value)
}

function onChange(event: Event): void {
  setValue((event.target as HTMLInputElement).checked)
}

function onEnter(event: KeyboardEvent): void {
  if (inert.value) return
  event.preventDefault()
  setValue(!props.checked)
}
</script>

<template>
  <label
    v-if="label"
    class="osq-switch-row"
    :class="{ 'osq-switch-row--busy': busy }"
  >
    <span class="osq-switch-row__text">
      <strong>{{ label }}</strong>
      <small v-if="caption">{{ caption }}</small>
    </span>
    <input
      :id="id"
      class="osq-switch control-switch"
      type="checkbox"
      role="switch"
      :name="name"
      :checked="checked"
      :aria-checked="checked ? 'true' : 'false'"
      :disabled="inert"
      :aria-busy="busy || undefined"
      :aria-label="ariaLabel"
      @change="onChange"
      @keydown.enter="onEnter"
    >
  </label>
  <input
    v-else
    :id="id"
    class="osq-switch control-switch"
    type="checkbox"
    role="switch"
    :name="name"
    :checked="checked"
    :aria-checked="checked ? 'true' : 'false'"
    :disabled="inert"
    :aria-busy="busy || undefined"
    :aria-label="ariaLabel"
    @change="onChange"
    @keydown.enter="onEnter"
  >
</template>

<style>
.osq-switch {
  appearance: none;
  background: var(--bg-hover);
  border: none;
  border-radius: 999px;
  cursor: pointer;
  flex-shrink: 0;
  height: 20px;
  margin: 0;
  position: relative;
  transition: background var(--dur-base) var(--ease-standard);
  width: 36px;
}

.osq-switch::before {
  background: var(--bg-surface);
  border-radius: 999px;
  box-shadow: 0 1px 3px var(--shadow-color);
  content: '';
  height: 14px;
  left: 3px;
  position: absolute;
  top: 3px;
  transition: transform var(--dur-base) var(--ease-standard);
  width: 14px;
}

.osq-switch:checked {
  background: var(--accent);
}

.osq-switch:checked::before {
  transform: translateX(16px);
}

.osq-switch:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.osq-switch:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.osq-switch-row {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text);
  cursor: pointer;
  display: flex;
  gap: 0.75rem;
  justify-content: space-between;
  min-height: 42px;
  padding: 0.5rem 0.625rem;
  text-align: left;
  width: 100%;
}

.osq-switch-row:hover {
  background: var(--bg-surface);
  border-color: var(--border-focus);
}

.osq-switch-row--busy {
  cursor: wait;
  opacity: 0.62;
}

.osq-switch-row--busy .osq-switch:disabled {
  opacity: 1;
}

.osq-switch-row__text strong {
  display: block;
  font-size: 0.8125rem;
}

.osq-switch-row__text small {
  color: var(--text-muted);
  display: block;
  font-size: 0.6875rem;
  margin-top: 1px;
}

@media (prefers-reduced-motion: reduce) {
  .osq-switch,
  .osq-switch::before {
    transition: none;
  }
}

@media (max-width: 768px) {
  .osq-switch {
    height: 26px;
    width: 44px;
  }

  .osq-switch::before {
    height: 20px;
    width: 20px;
  }

  .osq-switch:checked::before {
    transform: translateX(18px);
  }
}
</style>

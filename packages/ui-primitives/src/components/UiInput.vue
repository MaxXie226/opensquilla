<script setup lang="ts">
import { computed, useAttrs, useId } from 'vue'

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<{
  modelValue?: string | number
  id?: string
  label?: string
  description?: string
  error?: string
  type?: 'text' | 'email' | 'password' | 'number' | 'search' | 'url'
  disabled?: boolean
  required?: boolean
}>(), {
  modelValue: '',
  type: 'text',
  disabled: false,
  required: false,
})

const emit = defineEmits<{
  'update:modelValue': [string | number]
  change: [Event]
}>()

const attrs = useAttrs()
const generatedId = useId()
const inputId = computed(() => props.id ?? `osq-input-${generatedId}`)
const descriptionId = computed(() =>
  props.description ? `${inputId.value}-description` : undefined,
)
const errorId = computed(() => props.error ? `${inputId.value}-error` : undefined)
const describedBy = computed(() =>
  [attrs['aria-describedby'], descriptionId.value, errorId.value]
    .filter(Boolean)
    .join(' ') || undefined,
)

function onInput(event: Event): void {
  const input = event.target as HTMLInputElement
  const value =
    props.type === 'number' && input.value !== ''
      ? input.valueAsNumber
      : input.value
  emit('update:modelValue', value)
}
</script>

<template>
  <label class="osq-input-field" :for="inputId">
    <span v-if="label" class="osq-input-field__label">
      {{ label }}<span v-if="required" aria-hidden="true"> *</span>
    </span>
    <input
      v-bind="attrs"
      :id="inputId"
      class="osq-input"
      :class="{ 'osq-input--invalid': Boolean(error) }"
      :type="type"
      :value="modelValue"
      :disabled="disabled"
      :required="required"
      :aria-invalid="error ? 'true' : undefined"
      :aria-describedby="describedBy"
      @input="onInput"
      @change="emit('change', $event)"
    >
    <span
      v-if="description"
      :id="descriptionId"
      class="osq-input-field__description"
    >{{ description }}</span>
    <span
      v-if="error"
      :id="errorId"
      class="osq-input-field__error"
      role="alert"
    >{{ error }}</span>
  </label>
</template>

<style>
.osq-input-field {
  color: var(--text);
  display: grid;
  font-family: var(--font-sans);
  gap: var(--sp-1);
}

.osq-input-field__label {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.osq-input {
  appearance: none;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text);
  font: inherit;
  min-height: 38px;
  padding: 0 var(--sp-3);
  transition:
    background var(--transition),
    border-color var(--transition),
    box-shadow var(--transition);
  width: 100%;
}

.osq-input::placeholder {
  color: var(--text-dim);
}

.osq-input:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.osq-input:focus-visible {
  border-color: var(--border-focus);
  box-shadow: var(--focus-ring);
  outline: none;
}

.osq-input:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.osq-input--invalid {
  border-color: var(--danger);
}

.osq-input-field__description,
.osq-input-field__error {
  font-size: var(--fs-xs);
  line-height: 1.4;
}

.osq-input-field__description {
  color: var(--text-muted);
}

.osq-input-field__error {
  color: var(--danger);
}
</style>

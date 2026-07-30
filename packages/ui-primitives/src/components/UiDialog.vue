<script setup lang="ts">
import {
  nextTick,
  onBeforeUnmount,
  useId,
  ref,
  watch,
  type ComponentPublicInstance,
} from 'vue'

import {
  isTopmostDialog,
  registerDialog,
  unregisterDialog,
} from './dialogStack.js'

const props = withDefaults(defineProps<{
  open: boolean
  title?: string
  description?: string
  closeOnBackdrop?: boolean
  closeOnEscape?: boolean
  initialFocus?: 'first' | 'dialog'
  teleportTo?: string
}>(), {
  closeOnBackdrop: true,
  closeOnEscape: true,
  initialFocus: 'first',
  teleportTo: 'body',
})

const emit = defineEmits<{
  close: []
}>()

const dialog = ref<HTMLElement | null>(null)
const token = Symbol('ui-dialog')
const titleId = `osq-dialog-title-${useId()}`
const descriptionId = `osq-dialog-description-${useId()}`
let invoker: HTMLElement | null = null

const focusableSelector = [
  'button:not([disabled]):not([tabindex="-1"])',
  'a[href]:not([tabindex="-1"])',
  'input:not([disabled]):not([tabindex="-1"])',
  'select:not([disabled]):not([tabindex="-1"])',
  'textarea:not([disabled]):not([tabindex="-1"])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

function requestClose(): void {
  if (isTopmostDialog(token)) emit('close')
}

function onBackdrop(event: MouseEvent): void {
  if (props.closeOnBackdrop && event.target === event.currentTarget) requestClose()
}

function onKeydown(event: KeyboardEvent): void {
  if (!props.open || !isTopmostDialog(token) || event.defaultPrevented) return
  if (event.key === 'Escape' && props.closeOnEscape) {
    event.preventDefault()
    event.stopPropagation()
    requestClose()
    return
  }
  if (event.key !== 'Tab' || !dialog.value) return

  const focusables = Array.from(
    dialog.value.querySelectorAll<HTMLElement>(focusableSelector),
  )
  if (focusables.length === 0) {
    event.preventDefault()
    dialog.value.focus()
    return
  }
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement
  const inside = active instanceof Node && dialog.value.contains(active)
  if (event.shiftKey && (!inside || active === first)) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && (!inside || active === last)) {
    event.preventDefault()
    first?.focus()
  }
}

watch(
  () => props.open,
  (open, wasOpen) => {
    if (open && !wasOpen) {
      invoker =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null
      registerDialog(token)
      document.addEventListener('keydown', onKeydown)
      void nextTick(() => {
        const target =
          props.initialFocus === 'dialog'
            ? dialog.value
            : dialog.value?.querySelector<HTMLElement>(focusableSelector)
              ?? dialog.value
        target?.focus()
      })
    } else if (!open && wasOpen) {
      const wasTopmost = unregisterDialog(token)
      document.removeEventListener('keydown', onKeydown)
      if (wasTopmost && invoker && document.contains(invoker)) invoker.focus()
      invoker = null
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  const wasTopmost = unregisterDialog(token)
  document.removeEventListener('keydown', onKeydown)
  if (wasTopmost && invoker && document.contains(invoker)) invoker.focus()
  invoker = null
})

function setDialogRef(instance: Element | ComponentPublicInstance | null): void {
  dialog.value = instance instanceof HTMLElement ? instance : null
}
</script>

<template>
  <Teleport :to="teleportTo">
    <Transition name="osq-dialog">
      <div
        v-if="open"
        class="osq-dialog-overlay"
        data-osq-dialog-overlay
        @click="onBackdrop"
      >
        <section
          :ref="setDialogRef"
          class="osq-dialog"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="titleId"
          :aria-describedby="description ? descriptionId : undefined"
          tabindex="-1"
          @click.stop
        >
          <header class="osq-dialog__header">
            <h2 :id="titleId" class="osq-dialog__title">
              <slot name="title">{{ title }}</slot>
            </h2>
          </header>
          <p
            v-if="description"
            :id="descriptionId"
            class="osq-dialog__description"
          >{{ description }}</p>
          <div class="osq-dialog__body">
            <slot />
          </div>
          <footer v-if="$slots.footer" class="osq-dialog__footer">
            <slot name="footer" />
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style>
.osq-dialog-overlay {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: var(--sp-4);
  position: fixed;
  z-index: 1100;
}

.osq-dialog {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--elev-3);
  color: var(--text);
  max-height: min(90vh, 760px);
  max-width: 560px;
  overflow: auto;
  padding: var(--sp-5);
  width: min(100%, 560px);
}

.osq-dialog:focus-visible {
  box-shadow: var(--elev-3), var(--focus-ring);
  outline: none;
}

.osq-dialog__header,
.osq-dialog__body,
.osq-dialog__footer {
  min-width: 0;
}

.osq-dialog__title {
  font-family: var(--font-display);
  font-size: var(--fs-lg);
  line-height: 1.25;
  margin: 0;
}

.osq-dialog__description {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin: var(--sp-2) 0 0;
}

.osq-dialog__body {
  margin-top: var(--sp-4);
}

.osq-dialog__footer {
  display: flex;
  gap: var(--sp-3);
  justify-content: flex-end;
  margin-top: var(--sp-5);
}

.osq-dialog-enter-active,
.osq-dialog-leave-active {
  transition: opacity var(--dur-base) var(--ease-standard);
}

.osq-dialog-enter-from,
.osq-dialog-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .osq-dialog-enter-active,
  .osq-dialog-leave-active {
    transition: none;
  }
}
</style>

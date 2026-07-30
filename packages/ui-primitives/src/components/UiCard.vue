<script setup lang="ts">
withDefaults(defineProps<{
  as?: string
  interactive?: boolean
  padded?: boolean
}>(), {
  as: 'section',
  interactive: false,
  padded: true,
})
</script>

<template>
  <component
    :is="as"
    class="osq-card"
    :class="{
      'osq-card--interactive': interactive,
      'osq-card--padded': padded,
    }"
  >
    <header v-if="$slots.header" class="osq-card__header">
      <slot name="header" />
    </header>
    <div class="osq-card__body">
      <slot />
    </div>
    <footer v-if="$slots.footer" class="osq-card__footer">
      <slot name="footer" />
    </footer>
  </component>
</template>

<style>
.osq-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: var(--elev-1);
  color: var(--text);
  overflow: hidden;
}

.osq-card--interactive {
  transition:
    border-color var(--transition),
    box-shadow var(--transition),
    transform var(--transition);
}

.osq-card--interactive:hover {
  border-color: var(--border-strong);
  box-shadow: var(--elev-1-hover);
  transform: translateY(-1px);
}

.osq-card--padded .osq-card__body,
.osq-card__header,
.osq-card__footer {
  padding: var(--sp-4);
}

.osq-card__header {
  border-bottom: 1px solid var(--hairline);
}

.osq-card__footer {
  border-top: 1px solid var(--hairline);
}

@media (prefers-reduced-motion: reduce) {
  .osq-card--interactive {
    transition: none;
  }
}
</style>

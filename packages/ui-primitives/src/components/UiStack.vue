<script setup lang="ts">
import type {
  UiStackAlign,
  UiStackDirection,
  UiStackGap,
  UiStackJustify,
} from './stack.js'

withDefaults(defineProps<{
  as?: string
  direction?: UiStackDirection
  gap?: UiStackGap
  align?: UiStackAlign
  justify?: UiStackJustify
  wrap?: boolean
}>(), {
  as: 'div',
  direction: 'column',
  gap: 'md',
  align: 'stretch',
  justify: 'start',
  wrap: false,
})
</script>

<template>
  <component
    :is="as"
    :class="[
      'osq-stack',
      `osq-stack--${direction}`,
      `osq-stack--gap-${gap}`,
      `osq-stack--align-${align}`,
      `osq-stack--justify-${justify}`,
      { 'osq-stack--wrap': wrap },
    ]"
  >
    <slot />
  </component>
</template>

<style>
.osq-stack { display: flex; }
.osq-stack--row { flex-direction: row; }
.osq-stack--column { flex-direction: column; }
.osq-stack--wrap { flex-wrap: wrap; }
.osq-stack--gap-none { gap: 0; }
.osq-stack--gap-xs { gap: var(--sp-1); }
.osq-stack--gap-sm { gap: var(--sp-2); }
.osq-stack--gap-md { gap: var(--sp-3); }
.osq-stack--gap-lg { gap: var(--sp-4); }
.osq-stack--gap-xl { gap: var(--sp-5); }
.osq-stack--align-start { align-items: flex-start; }
.osq-stack--align-center { align-items: center; }
.osq-stack--align-end { align-items: flex-end; }
.osq-stack--align-stretch { align-items: stretch; }
.osq-stack--align-baseline { align-items: baseline; }
.osq-stack--justify-start { justify-content: flex-start; }
.osq-stack--justify-center { justify-content: center; }
.osq-stack--justify-end { justify-content: flex-end; }
.osq-stack--justify-between { justify-content: space-between; }
.osq-stack--justify-around { justify-content: space-around; }
.osq-stack--justify-evenly { justify-content: space-evenly; }
</style>

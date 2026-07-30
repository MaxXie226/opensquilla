<script setup lang="ts">
import { ref } from 'vue'

import {
  UiButton,
  UiCard,
  UiDialog,
  UiInput,
  UiStack,
  UiSwitch,
} from '../dist/index.js'

const dialogOpen = ref(false)
const name = ref('')
const enabled = ref(true)
</script>

<template>
  <main id="fixture" aria-label="UI primitives fixture">
    <UiCard>
      <template #header>
        <h1>Public UI primitives</h1>
      </template>
      <UiStack gap="lg">
        <UiStack direction="row" gap="sm" wrap>
          <UiButton id="primary" variant="primary">Primary</UiButton>
          <UiButton variant="secondary">Secondary</UiButton>
          <UiButton variant="ghost">Ghost</UiButton>
          <UiButton variant="danger">Danger</UiButton>
          <UiButton busy>Busy</UiButton>
        </UiStack>
        <UiInput
          v-model="name"
          label="Workspace"
          description="Use a public, product-neutral label."
          error="Example validation message"
          placeholder="project"
        />
        <UiSwitch
          v-model:checked="enabled"
          label="Shared capability"
          caption="Native and product behavior stay outside this package."
        />
        <UiButton id="open-dialog" @click="dialogOpen = true">
          Open dialog
        </UiButton>
      </UiStack>
    </UiCard>

    <UiDialog
      :open="dialogOpen"
      title="Confirm action"
      description="Keyboard focus remains inside this dialog."
      @close="dialogOpen = false"
    >
      <p>Shared dialog body</p>
      <template #footer>
        <UiButton id="dialog-cancel" variant="ghost" @click="dialogOpen = false">
          Cancel
        </UiButton>
        <UiButton id="dialog-confirm" variant="primary" @click="dialogOpen = false">
          Confirm
        </UiButton>
      </template>
    </UiDialog>
  </main>
</template>

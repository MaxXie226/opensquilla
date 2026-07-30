import {
  createApp,
  h,
  nextTick,
  ref,
  type App,
  type Component,
} from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  UiButton,
  UiDialog,
  UiInput,
  UiStack,
  UiSwitch,
} from '../src/index.js'

const mounted: Array<{ app: App; host: HTMLElement }> = []

function mount(component: Component, props: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(component, props) })
  app.mount(host)
  mounted.push({ app, host })
  return host
}

afterEach(() => {
  for (const { app, host } of mounted.splice(0)) {
    app.unmount()
    host.remove()
  }
  document.body.replaceChildren()
})

describe('public UI primitives', () => {
  it('keeps a busy button inert and exposes busy state', async () => {
    const click = vi.fn()
    const host = mount(UiButton, {
      busy: true,
      onClick: click,
    })
    const button = host.querySelector('button')

    button?.click()
    await nextTick()
    expect(click).not.toHaveBeenCalled()
    expect(button?.disabled).toBe(true)
    expect(button?.getAttribute('aria-busy')).toBe('true')
  })

  it('connects input labels, descriptions, errors, and v-model updates', async () => {
    const update = vi.fn()
    const host = mount(UiInput, {
      label: 'Workspace',
      description: 'Public path',
      error: 'Required',
      modelValue: '',
      'onUpdate:modelValue': update,
    })
    const input = host.querySelector('input')

    expect(host.querySelector('label')?.getAttribute('for')).toBe(input?.id)
    expect(input?.getAttribute('aria-invalid')).toBe('true')
    expect(input?.getAttribute('aria-describedby')?.split(' ')).toHaveLength(2)
    if (input) {
      input.value = 'project'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    await nextTick()
    expect(update).toHaveBeenCalledWith('project')
  })

  it('preserves native switch semantics and Enter parity', async () => {
    const change = vi.fn()
    const host = mount(UiSwitch, {
      checked: false,
      ariaLabel: 'Enable',
      onChange: change,
    })
    const input = host.querySelector<HTMLInputElement>('input')

    input?.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
    }))
    await nextTick()
    expect(input?.getAttribute('role')).toBe('switch')
    expect(input?.getAttribute('aria-checked')).toBe('false')
    expect(change).toHaveBeenCalledWith(true)
  })

  it('maps layout props to stable public classes', () => {
    const host = mount(UiStack, {
      direction: 'row',
      gap: 'lg',
      align: 'center',
      justify: 'between',
      wrap: true,
    })
    const stack = host.firstElementChild

    expect(stack?.classList.contains('osq-stack--row')).toBe(true)
    expect(stack?.classList.contains('osq-stack--gap-lg')).toBe(true)
    expect(stack?.classList.contains('osq-stack--align-center')).toBe(true)
    expect(stack?.classList.contains('osq-stack--justify-between')).toBe(true)
    expect(stack?.classList.contains('osq-stack--wrap')).toBe(true)
  })

  it('focuses the dialog, traps Tab, closes on Escape, and restores focus', async () => {
    const invoker = document.createElement('button')
    invoker.textContent = 'Open'
    document.body.append(invoker)
    invoker.focus()

    const open = ref(true)
    const close = vi.fn(() => {
      open.value = false
    })
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp({
      render: () =>
        h(
          UiDialog,
          {
            open: open.value,
            title: 'Confirm',
            description: 'Continue?',
            onClose: close,
          },
          {
            default: () => h('p', 'Body'),
            footer: () => [
              h('button', { id: 'cancel' }, 'Cancel'),
              h('button', { id: 'continue' }, 'Continue'),
            ],
          },
        ),
    })
    app.mount(host)
    mounted.push({ app, host })

    await nextTick()
    await nextTick()
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    const first = document.querySelector<HTMLButtonElement>('#cancel')
    const last = document.querySelector<HTMLButtonElement>('#continue')
    expect(dialog?.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(first)

    last?.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    }))
    expect(document.activeElement).toBe(first)

    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    }))
    await nextTick()
    await new Promise((resolve) => setTimeout(resolve, 250))
    expect(close).toHaveBeenCalledOnce()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(invoker)
  })

  it('restores focus when an open dialog is unmounted', async () => {
    const invoker = document.createElement('button')
    invoker.textContent = 'Open'
    document.body.append(invoker)
    invoker.focus()

    const host = mount(UiDialog, {
      open: true,
      title: 'Unmounted dialog',
    })
    await nextTick()
    await nextTick()
    expect(document.activeElement).not.toBe(invoker)

    const mountedIndex = mounted.findIndex((entry) => entry.host === host)
    expect(mountedIndex).toBeGreaterThanOrEqual(0)
    mounted[mountedIndex]?.app.unmount()
    mounted.splice(mountedIndex, 1)
    host.remove()

    expect(document.activeElement).toBe(invoker)
  })
})

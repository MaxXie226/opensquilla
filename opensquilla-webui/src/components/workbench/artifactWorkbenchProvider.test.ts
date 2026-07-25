import { describe, expect, it, vi } from 'vitest'
import type {
  NativeWorkbenchApi,
  Platform,
} from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import { createArtifactPreviewWorkbenchItem } from '@/workbench/artifactItems'
import type {
  WorkbenchPanelRenderState,
  WorkbenchRuntimeContext,
} from '@/workbench/types'
import { createArtifactWorkbenchDefinitions } from './artifactWorkbenchProvider'

const artifact: ArtifactPayload = {
  id: 'artifact-1',
  name: 'preview.html',
  mime: 'text/html',
  size: 128,
  download_url: '/api/v1/artifacts/artifact-1',
}

describe('artifact Workbench provider', () => {
  it('owns native surface actions, events, visibility, and render state', async () => {
    const createSurface = vi.fn(async () => ({ ok: true }))
    const setSurfaceRect = vi.fn(async () => ({ ok: true }))
    const activateSurface = vi.fn(async () => ({ ok: true }))
    const destroySurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface,
      setSurfaceRect,
      activateSurface,
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const context: WorkbenchRuntimeContext = {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    }
    const reload = vi.fn(async () => undefined)
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definitions = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      currentSessionId: () => 'session-a',
      platform: {
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      pushToast: vi.fn(),
      t: key => key,
    })
    const definition = definitions.find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, context)
    await runtime.setComponentHandle?.({ reload })
    await runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: {
        artifact,
        data: new TextEncoder().encode('<img src="./missing.png">').buffer,
        hasRelativeResources: true,
        mime: 'text/html',
        relativeResourceCount: 1,
        sessionKey: 'session-a',
      },
    }, item)
    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: true,
    }, item)

    expect(createSurface).toHaveBeenCalledWith(expect.objectContaining({
      surfaceId: item.id,
      payload: expect.objectContaining({ allowRemoteResources: false }),
    }))
    expect(activateSurface).toHaveBeenCalledWith(item.id)
    expect(renderState).toMatchObject({
      missingResources: true,
      nativeSurfaceState: 'loading',
      remoteResourcesEnabled: false,
    })

    await runtime.performAction?.('refresh', item)
    expect(reload).toHaveBeenCalledOnce()
    await runtime.performAction?.('toggle-remote-resources', item)
    expect(createSurface).toHaveBeenLastCalledWith(expect.objectContaining({
      payload: expect.objectContaining({ allowRemoteResources: true }),
    }))

    await runtime.handleNativeSurfaceEvent?.({
      version: 1,
      surfaceId: item.id,
      type: 'error',
    }, item)
    expect(renderState.nativeSurfaceState).toBe('crashed')
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: item.id, visible: false }),
    )

    const presentation: WorkbenchPanelRenderState = {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    }
    expect(definition.getToolbarItems?.(item, presentation)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: 'missing-resources', kind: 'status' }),
        expect.objectContaining({
          id: 'toggle-remote-resources',
          kind: 'action',
          pressed: true,
        }),
      ]),
    )

    await runtime.dispose?.('closed')
    expect(destroySurface).toHaveBeenCalledWith(item.id)
  })

  it('silently discards a pending native create after its item closes', async () => {
    const createControl: {
      resolve: ((result: { ok: boolean }) => void) | null
    } = { resolve: null }
    const createSurface = vi.fn(() => new Promise<{ ok: boolean }>(resolve => {
      createControl.resolve = resolve
    }))
    const destroySurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface,
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    let itemOpen = true
    const pushToast = vi.fn()
    const context: WorkbenchRuntimeContext = {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => itemOpen,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    }
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      currentSessionId: () => 'session-a',
      platform: {
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, context)
    const creating = runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: {
        artifact,
        data: new TextEncoder().encode('<p>preview</p>').buffer,
        hasRelativeResources: false,
        mime: 'text/html',
        relativeResourceCount: 0,
        sessionKey: 'session-a',
      },
    }, item)
    await vi.waitFor(() => expect(createSurface).toHaveBeenCalledOnce())
    itemOpen = false
    createControl.resolve?.({ ok: true })
    await creating

    expect(destroySurface).toHaveBeenCalledWith(item.id)
    expect(pushToast).not.toHaveBeenCalled()
    expect(renderState.nativeSurfaceState).not.toBe('crashed')
  })
})

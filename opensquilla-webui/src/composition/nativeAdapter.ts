import {
  createNativeCapabilityAdapter,
  createWebNativeCapabilityAdapter,
  type NativeCapabilityAdapter,
  type NativeCapabilityInvocation,
  type NativeCapabilityResult,
} from '@opensquilla/ui-foundation'
import type { Platform } from '@/platform'

export const PUBLIC_WEB_UI_NATIVE_CAPABILITIES = [
  'opensquilla.host.gateway-status',
  'opensquilla.host.gateway-reveal-log',
  'opensquilla.host.gateway-retry',
  'opensquilla.host.cli-invocation',
  'opensquilla.host.open-artifact',
  'opensquilla.host.choose-project-directory',
  'opensquilla.host.desktop-settings',
  'opensquilla.host.desktop-preferences',
  'opensquilla.host.onboarding',
  'opensquilla.host.updates',
  'opensquilla.host.native-workbench',
  'opensquilla.host.os-locale',
  'opensquilla.host.native-theme',
] as const

type PublicWebUiNativeCapability = typeof PUBLIC_WEB_UI_NATIVE_CAPABILITIES[number]

interface CapabilityRequest {
  readonly action?: string
  readonly payload?: unknown
}

function requestObject(value: unknown): CapabilityRequest {
  return value !== null && typeof value === 'object'
    ? value as CapabilityRequest
    : {}
}

function success<T>(value: T): NativeCapabilityResult<T> {
  return { ok: true, value }
}

function unavailable(
  capability: PublicWebUiNativeCapability,
  message: string,
): NativeCapabilityResult {
  return {
    ok: false,
    error: {
      code: 'unavailable',
      capability,
      message,
    },
  }
}

function desktopCapabilities(platform: Platform): PublicWebUiNativeCapability[] {
  const capabilities: PublicWebUiNativeCapability[] = [
    'opensquilla.host.gateway-status',
    'opensquilla.host.updates',
    'opensquilla.host.os-locale',
    'opensquilla.host.native-theme',
  ]
  if (platform.gateway.revealLog) capabilities.push('opensquilla.host.gateway-reveal-log')
  if (platform.gateway.retryStartup) capabilities.push('opensquilla.host.gateway-retry')
  if (platform.gateway.getCliInvocation) capabilities.push('opensquilla.host.cli-invocation')
  if (platform.files.openArtifact) capabilities.push('opensquilla.host.open-artifact')
  if (platform.files.chooseProjectDirectory) {
    capabilities.push('opensquilla.host.choose-project-directory')
  }
  if (platform.settings.getDesktopSettings) {
    capabilities.push('opensquilla.host.desktop-settings')
  }
  if (platform.settings.getDesktopPreferences) {
    capabilities.push('opensquilla.host.desktop-preferences')
  }
  if (platform.onboarding.getDefaults) capabilities.push('opensquilla.host.onboarding')
  if (platform.workbench.native) capabilities.push('opensquilla.host.native-workbench')
  return capabilities
}

async function invokeDesktopCapability(
  platform: Platform,
  capability: PublicWebUiNativeCapability,
  requestValue: unknown,
): Promise<NativeCapabilityResult> {
  const request = requestObject(requestValue)
  switch (capability) {
    case 'opensquilla.host.gateway-status':
      return success(await platform.gateway.getStatus())
    case 'opensquilla.host.gateway-reveal-log':
      return platform.gateway.revealLog
        ? success(await platform.gateway.revealLog())
        : unavailable(capability, 'Gateway log reveal is unavailable')
    case 'opensquilla.host.gateway-retry':
      return platform.gateway.retryStartup
        ? success(await platform.gateway.retryStartup())
        : unavailable(capability, 'Gateway restart is unavailable')
    case 'opensquilla.host.cli-invocation':
      return platform.gateway.getCliInvocation
        ? success(await platform.gateway.getCliInvocation())
        : unavailable(capability, 'CLI invocation discovery is unavailable')
    case 'opensquilla.host.open-artifact':
      return platform.files.openArtifact
        ? success(await platform.files.openArtifact(
            request.payload as Parameters<NonNullable<typeof platform.files.openArtifact>>[0],
          ))
        : unavailable(capability, 'Native artifact opening is unavailable')
    case 'opensquilla.host.choose-project-directory':
      return platform.files.chooseProjectDirectory
        ? success(await platform.files.chooseProjectDirectory(
            request.payload as Parameters<
              NonNullable<typeof platform.files.chooseProjectDirectory>
            >[0],
          ))
        : unavailable(capability, 'Native directory selection is unavailable')
    case 'opensquilla.host.desktop-settings':
      if (request.action === 'get' && platform.settings.getDesktopSettings) {
        return success(await platform.settings.getDesktopSettings())
      }
      if (request.action === 'save' && platform.settings.saveDesktopSettings) {
        return success(await platform.settings.saveDesktopSettings(
          request.payload as Parameters<
            NonNullable<typeof platform.settings.saveDesktopSettings>
          >[0],
        ))
      }
      if (request.action === 'reset' && platform.settings.resetDesktopSettings) {
        return success(await platform.settings.resetDesktopSettings())
      }
      return unavailable(capability, `Desktop settings action "${request.action || ''}" is unavailable`)
    case 'opensquilla.host.desktop-preferences':
      if (request.action === 'get' && platform.settings.getDesktopPreferences) {
        return success(await platform.settings.getDesktopPreferences())
      }
      if (request.action === 'save' && platform.settings.saveDesktopPreferences) {
        return success(await platform.settings.saveDesktopPreferences(
          request.payload as Parameters<
            NonNullable<typeof platform.settings.saveDesktopPreferences>
          >[0],
        ))
      }
      return unavailable(
        capability,
        `Desktop preferences action "${request.action || ''}" is unavailable`,
      )
    case 'opensquilla.host.onboarding':
      if (request.action === 'get' && platform.onboarding.getDefaults) {
        return success(await platform.onboarding.getDefaults())
      }
      if (request.action === 'save' && platform.onboarding.save) {
        return success(await platform.onboarding.save(request.payload))
      }
      if (request.action === 'cancel' && platform.onboarding.cancel) {
        return success(await platform.onboarding.cancel())
      }
      return unavailable(capability, `Onboarding action "${request.action || ''}" is unavailable`)
    case 'opensquilla.host.updates':
      if (request.action === 'check') return success(await platform.updates.check())
      if (request.action === 'download') return success(await platform.updates.download())
      if (request.action === 'relaunch') return success(await platform.updates.relaunch())
      if (request.action === 'dismiss') return success(await platform.updates.dismiss())
      return success(await platform.updates.getState())
    case 'opensquilla.host.native-workbench':
      return unavailable(
        capability,
        'Native Workbench uses its versioned surface API service',
      )
    case 'opensquilla.host.os-locale':
      return success(await platform.getOsLocale())
    case 'opensquilla.host.native-theme':
      return success(await platform.setNativeTheme(
        request.payload as Parameters<Platform['setNativeTheme']>[0],
      ))
  }
}

export function createPublicWebUiNativeAdapter(
  platform: Platform,
): NativeCapabilityAdapter {
  if (platform.id === 'web') return createWebNativeCapabilityAdapter()
  const capabilities = desktopCapabilities(platform)
  return createNativeCapabilityAdapter({
    bridgeVersion: 'desktop-platform-v1',
    capabilities,
    async invoke<T = unknown>(
      { capability, request }: NativeCapabilityInvocation,
    ): Promise<NativeCapabilityResult<T>> {
      return await invokeDesktopCapability(
        platform,
        capability as PublicWebUiNativeCapability,
        request,
      ) as NativeCapabilityResult<T>
    },
  })
}

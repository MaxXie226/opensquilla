import type { SandboxRunMode, SandboxSetupStatusPayload } from '@/types/sandbox'

export function effectiveComposerRunMode(
  preference: SandboxRunMode,
  setupStatus: Pick<SandboxSetupStatusPayload, 'state'> | null,
  activeLock: SandboxRunMode | null,
): SandboxRunMode {
  if (activeLock) return activeLock
  if (preference === 'safe' && setupStatus && setupStatus.state !== 'ready') return 'full'
  return preference
}

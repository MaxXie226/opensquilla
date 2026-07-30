import { describe, expect, it, vi } from 'vitest'

import { createGatewayQuery } from '../src/index.js'

describe('GatewayQuery', () => {
  it('uses only the injected client and keeps cached data during refresh failures', async () => {
    const client = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn()
        .mockResolvedValueOnce({ value: 1 })
        .mockRejectedValueOnce(new Error('synthetic refresh failure')),
    }
    const onError = vi.fn()
    const query = createGatewayQuery<{ value: number }>({
      client,
      method: 'status',
      params: () => ({ scope: 'synthetic' }),
      onError,
    })

    await expect(query.execute()).resolves.toEqual({ value: 1 })
    expect(query.snapshot).toEqual({
      data: { value: 1 },
      error: null,
      loading: false,
    })

    await expect(query.refresh()).resolves.toBeNull()
    expect(query.snapshot.data).toEqual({ value: 1 })
    expect(query.snapshot.error?.message).toBe('synthetic refresh failure')
    expect(onError).toHaveBeenCalledOnce()
    expect(client.call).toHaveBeenCalledWith(
      'status',
      { scope: 'synthetic' },
      { signal: expect.any(AbortSignal) },
    )
  })

  it('ignores stale and post-disposal state updates', async () => {
    let resolveFirst: ((value: string) => void) | undefined
    const client = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn()
        .mockImplementationOnce(() => new Promise<string>((resolve) => {
          resolveFirst = resolve
        }))
        .mockResolvedValueOnce('newest'),
    }
    const query = createGatewayQuery<string>({ client, method: 'status' })

    const first = query.execute()
    await query.execute()
    resolveFirst?.('stale')
    await first
    expect(query.snapshot.data).toBe('newest')

    query.dispose()
    expect(await query.execute()).toBeNull()
    expect(query.snapshot.data).toBe('newest')
  })

  it('aborts an owned connection wait when disposed', async () => {
    let observedSignal: AbortSignal | undefined
    const client = {
      waitForConnection: vi.fn(
        async (_timeout?: number, signal?: AbortSignal) => {
          observedSignal = signal
          await new Promise<void>((_resolve, reject) => {
            signal?.addEventListener(
              'abort',
              () => reject(new Error('synthetic abort')),
              { once: true },
            )
          })
        },
      ),
      call: vi.fn(),
    }
    const onError = vi.fn()
    const query = createGatewayQuery({ client, method: 'status', onError })
    const running = query.execute()

    query.dispose()

    await expect(running).resolves.toBeNull()
    expect(observedSignal?.aborted).toBe(true)
    expect(client.call).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })
})

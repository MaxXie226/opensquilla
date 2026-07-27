import assert from 'node:assert/strict'
import test from 'node:test'

import { GatewayHttpClient, GatewayHttpError } from '../dist/index.js'

test('normalizes base paths, headers, JSON bodies, and successful responses', async () => {
  const calls = []
  const client = new GatewayHttpClient({
    baseUrl: 'https://gateway.test/',
    headers: { authorization: 'Bearer <synthetic>' },
    fetch: async (input, init) => {
      calls.push({ input, init })
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
  })

  assert.deepEqual(await client.post('/api/check', { value: 1 }), { ok: true })
  assert.equal(calls[0].input, 'https://gateway.test/api/check')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers.get('authorization'), 'Bearer <synthetic>')
  assert.equal(calls[0].init.headers.get('content-type'), 'application/json')
  assert.equal(calls[0].init.body, '{"value":1}')
})

test('maps non-success responses to one structured HTTP error', async () => {
  const client = new GatewayHttpClient({
    baseUrl: 'https://gateway.test',
    fetch: async () =>
      new Response(
        JSON.stringify({
          error: {
            code: 'UNAUTHORIZED',
            message: 'Token rejected',
            retryable: false,
          },
        }),
        {
          status: 401,
          headers: { 'content-type': 'application/json' },
        },
      ),
  })

  await assert.rejects(client.get('/api/private'), (error) => {
    assert.ok(error instanceof GatewayHttpError)
    assert.equal(error.status, 401)
    assert.equal(error.code, 'UNAUTHORIZED')
    assert.equal(error.message, 'Token rejected')
    assert.equal(error.retryable, false)
    return true
  })
})

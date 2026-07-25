import { describe, expect, it } from 'vitest'
import {
  buildImagePayload,
  imageModelForDisplay,
  imageModelRefForPayload,
  parseImageFallbacks,
  useSetupCapabilitiesForm,
} from './useSetupCapabilitiesForm'

const imageProviders = [
  {
    providerId: 'openai',
    envKey: 'OPENAI_API_KEY',
    requiresApiKey: true,
    defaultBaseUrl: 'https://api.openai.com/v1',
    defaultModel: 'gpt-image-1',
  },
  {
    providerId: 'openrouter',
    envKey: 'OPENROUTER_API_KEY',
    requiresApiKey: true,
    defaultBaseUrl: 'https://openrouter.ai/api/v1',
    defaultModel: 'google/gemini-3.1-flash-image-preview',
  },
]

const baseValues = {
  providerId: 'openrouter',
  enabled: true,
  primary: 'google/gemini-3.1-flash-image-preview',
  apiKey: '',
  apiKeyEnv: '',
  baseUrl: '',
  size: '1024x1024',
  outputFormat: 'png',
  fallbacks: '',
}

describe('image model references', () => {
  it('removes exactly one routing prefix from the provider-local model name', () => {
    expect(imageModelForDisplay(
      'openrouter',
      'openrouter/google/gemini-3.1-flash-image-preview',
    )).toBe('google/gemini-3.1-flash-image-preview')
    expect(imageModelForDisplay(
      'openrouter',
      'openrouter/openrouter/auto',
    )).toBe('openrouter/auto')
  })

  it('adds exactly one provider prefix to the RPC primary reference', () => {
    expect(imageModelRefForPayload(
      'openrouter',
      'google/gemini-3.1-flash-image-preview',
    )).toBe('openrouter/google/gemini-3.1-flash-image-preview')
    expect(imageModelRefForPayload(
      'openrouter',
      'openrouter/google/gemini-3.1-flash-image-preview',
    )).toBe('openrouter/google/gemini-3.1-flash-image-preview')
    expect(imageModelRefForPayload(
      'openrouter',
      'openrouter/auto',
    )).toBe('openrouter/openrouter/auto')
    expect(imageModelRefForPayload(
      'openai',
      'openai/gpt-image-1',
    )).toBe('openai/gpt-image-1')
  })
})

describe('parseImageFallbacks', () => {
  it('splits on commas and newlines, canonicalizes OpenRouter auto, and drops empties', () => {
    expect(parseImageFallbacks('a/b, c/d\n , e/f')).toEqual(['a/b', 'c/d', 'e/f'])
    expect(parseImageFallbacks('openrouter/auto')).toEqual(['openrouter/openrouter/auto'])
    expect(parseImageFallbacks('   ')).toEqual([])
  })
})

describe('buildImagePayload', () => {
  it('normalizes primary and includes size, format, and explicitly edited fallbacks', () => {
    const payload = buildImagePayload({
      ...baseValues,
      size: '1536x1024',
      outputFormat: 'webp',
      fallbacks: 'openai/gpt-image-1, openrouter/google/gemini-2.5-flash-image',
    }, new Set(['fallbacks'] as const))

    expect(payload).toMatchObject({
      primary: 'openrouter/google/gemini-3.1-flash-image-preview',
      size: '1536x1024',
      outputFormat: 'webp',
      fallbacks: ['openai/gpt-image-1', 'openrouter/google/gemini-2.5-flash-image'],
    })
  })

  it('keeps a pasted canonical primary reference canonical in the payload', () => {
    const payload = buildImagePayload({
      ...baseValues,
      primary: 'openrouter/google/gemini-3.1-flash-image-preview',
    })

    expect(payload.primary).toBe('openrouter/google/gemini-3.1-flash-image-preview')
  })

  it('omits untouched base URL and fallbacks so a save preserves persisted values', () => {
    const payload = buildImagePayload({
      ...baseValues,
      baseUrl: 'https://openrouter.example.test/v1',
      fallbacks: 'openai/gpt-image-1',
    })

    expect(payload).not.toHaveProperty('baseUrl')
    expect(payload).not.toHaveProperty('fallbacks')
  })

  it('sends explicit empty values when base URL and fallbacks are cleared', () => {
    const payload = buildImagePayload(
      baseValues,
      new Set(['baseUrl', 'fallbacks'] as const),
    )

    expect(payload.baseUrl).toBe('')
    expect(payload.fallbacks).toEqual([])
    expect(payload).not.toHaveProperty('clearFallbacks')

    const explicitClear = buildImagePayload(
      baseValues,
      new Set(['fallbacks'] as const),
      { clearFallbacks: true },
    )
    expect(explicitClear.clearFallbacks).toBe(true)
  })

  it('does not send a fallback clear flag when a replacement is nonempty', () => {
    const payload = buildImagePayload({
      ...baseValues,
      fallbacks: 'openai/gpt-image-1',
    }, new Set(['fallbacks'] as const), { clearFallbacks: true })

    expect(payload.fallbacks).toEqual(['openai/gpt-image-1'])
    expect(payload).not.toHaveProperty('clearFallbacks')
  })

  it('never sends both direct and env credentials from inconsistent input', () => {
    const payload = buildImagePayload({
      ...baseValues,
      apiKey: 'sk-direct',
      apiKeyEnv: 'OPENROUTER_API_KEY',
    }, new Set(['apiKey', 'apiKeyEnv'] as const))

    expect(payload.apiKey).toBe('sk-direct')
    expect(payload).not.toHaveProperty('apiKeyEnv')
    expect(payload.credentialMode).toBe('direct')
  })

  it('adds a credential mode only when a credential field was edited', () => {
    const untouched = buildImagePayload({
      ...baseValues,
      apiKey: 'sk-untracked',
      apiKeyEnv: 'UNTRACKED_ENV',
    })
    expect(untouched).not.toHaveProperty('credentialMode')
    expect(untouched).not.toHaveProperty('apiKey')
    expect(untouched).not.toHaveProperty('apiKeyEnv')

    const env = buildImagePayload({
      ...baseValues,
      apiKeyEnv: 'OPENROUTER_API_KEY',
    }, new Set(['apiKeyEnv'] as const))
    expect(env).toMatchObject({
      credentialMode: 'env',
      apiKeyEnv: 'OPENROUTER_API_KEY',
    })
  })
})

describe('useSetupCapabilitiesForm image hydration', () => {
  it('uses the primary provider over a stale credential-provider status', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openai',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.selectedImageProvider.value).toBe('openrouter')
    expect(form.imagePrimaryValue.value).toBe('google/gemini-3.1-flash-image-preview')
    expect(form.imagePayload().primary)
      .toBe('openrouter/google/gemini-3.1-flash-image-preview')
  })

  it('keeps a redacted saved key as boolean state without filling the key input', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: {
          openrouter: {
            api_key: '[redacted]',
            api_key_env: 'STALE_OPENROUTER_ENV',
          },
        },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.imageKeyConfiguredValue.value).toBe(true)
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imageApiKeyEnvValue.value).toBe('')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')
    expect(form.imagePayload()).not.toHaveProperty('apiKeyEnv')
    expect(form.imagePayload()).not.toHaveProperty('credentialMode')
  })
})

describe('useSetupCapabilitiesForm provider drafts', () => {
  it('restores each provider model and endpoint across the 0.5.0 event order', () => {
    const form = useSetupCapabilitiesForm()
    const openrouter = imageProviders[1]

    form.initImageFromConfig({
      image_generation: {
        providers: {
          openai: { base_url: 'https://saved.openai.example/v1' },
          openrouter: { base_url: 'https://saved.openrouter.example/v1' },
        },
      },
    }, {
      imageGenerationProvider: 'openai',
      imageGenerationPrimary: 'openai/gpt-image-1',
    }, imageProviders)
    form.updateField('image', 'primary', 'custom-openai-image')

    // The old panel first emitted updateField(provider), then this dedicated
    // event. Both now go through the same idempotent switch operation.
    form.updateField('image', 'provider', 'openrouter')
    expect(form.imagePrimaryValue.value).toBe('google/gemini-3.1-flash-image-preview')
    expect(form.imageBaseUrlValue.value).toBe('https://saved.openrouter.example/v1')
    form.onImageProviderChange(openrouter)

    expect(form.selectedImageProvider.value).toBe('openrouter')
    expect(form.imagePrimaryValue.value).toBe('google/gemini-3.1-flash-image-preview')
    expect(form.imageBaseUrlValue.value).toBe('https://saved.openrouter.example/v1')

    form.updateField('image', 'provider', 'openai')
    expect(form.imagePrimaryValue.value).toBe('custom-openai-image')
    expect(form.imageBaseUrlValue.value).toBe('https://saved.openai.example/v1')
  })

  it('does not carry a transient pasted key to another provider or back again', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openai',
      imageGenerationPrimary: 'openai/gpt-image-1',
    }, imageProviders)
    form.updateField('image', 'apiKey', 'sk-transient')
    expect(form.imagePayload().apiKey).toBe('sk-transient')

    form.onImageProviderChange(imageProviders[1])
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')

    form.onImageProviderChange(imageProviders[0])
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')
  })

  it('keeps direct and env credential edits mutually exclusive in both directions', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    form.updateField('image', 'apiKey', 'sk-direct')
    expect(form.imageApiKeyEnvValue.value).toBe('')
    expect(form.imagePayload()).toMatchObject({
      apiKey: 'sk-direct',
      credentialMode: 'direct',
    })
    expect(form.imagePayload()).not.toHaveProperty('apiKeyEnv')

    form.updateField('image', 'apiKeyEnv', 'CUSTOM_OPENROUTER_KEY')
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imagePayload()).toMatchObject({
      apiKeyEnv: 'CUSTOM_OPENROUTER_KEY',
      credentialMode: 'env',
    })
    expect(form.imagePayload()).not.toHaveProperty('apiKey')

    form.updateField('image', 'apiKey', '')
    expect(form.imagePayload()).toMatchObject({ credentialMode: 'direct' })
    expect(form.imagePayload()).not.toHaveProperty('apiKey')
  })

  it('distinguishes untouched optional fields from explicit clearing', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        fallbacks: ['openai/gpt-image-1'],
        providers: {
          openrouter: { base_url: 'https://saved.openrouter.example/v1' },
        },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.imagePayload()).not.toHaveProperty('baseUrl')
    expect(form.imagePayload()).not.toHaveProperty('fallbacks')

    form.updateField('image', 'baseUrl', '')
    form.updateField('image', 'fallbacks', '')
    expect(form.imagePayload()).toMatchObject({
      baseUrl: '',
      fallbacks: [],
      clearFallbacks: true,
    })

    form.onImageProviderChange(imageProviders[0])
    expect(form.imagePayload()).toMatchObject({ fallbacks: [], clearFallbacks: true })
    expect(form.imagePayload()).not.toHaveProperty('baseUrl')
  })

  it('does not mark an initially empty fallback field as an explicit clear', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)
    form.updateField('image', 'fallbacks', '')

    expect(form.imagePayload().fallbacks).toEqual([])
    expect(form.imagePayload()).not.toHaveProperty('clearFallbacks')
  })
})

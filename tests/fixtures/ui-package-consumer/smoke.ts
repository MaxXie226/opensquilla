import {
  CLIENT_CONTRACT_DIGEST,
  CLIENT_MAX_PROTOCOL,
  CLIENT_MIN_PROTOCOL,
} from '@opensquilla/client-sdk'
import {
  NATIVE_CAPABILITY_API_VERSION,
  UI_COMPOSITION_API_VERSION,
  createOpenSquillaApp,
  createWebNativeCapabilityAdapter,
  type FeatureModuleContract,
} from '@opensquilla/ui-foundation'
import {
  UiButton,
  UiCard,
  UiDialog,
  UiInput,
  UiStack,
  UiSwitch,
} from '@opensquilla/ui-primitives'
import {
  PUBLIC_THEME_IDS,
  THEME_TOKEN_NAMES,
  type ThemeTokenName,
} from '@opensquilla/ui-tokens'

const communityFeature: FeatureModuleContract = {
  id: 'community.chat',
  apiVersion: UI_COMPOSITION_API_VERSION,
}
const independentProductFeature: FeatureModuleContract = {
  id: 'product.private-example',
  apiVersion: UI_COMPOSITION_API_VERSION,
  optionalCapabilities: ['native.window.lifecycle'],
}

void createOpenSquillaApp({
  features: [communityFeature],
  native: createWebNativeCapabilityAdapter(),
})
void createOpenSquillaApp({
  features: [independentProductFeature],
  native: createWebNativeCapabilityAdapter(),
})
void [
  CLIENT_CONTRACT_DIGEST,
  CLIENT_MIN_PROTOCOL,
  CLIENT_MAX_PROTOCOL,
  NATIVE_CAPABILITY_API_VERSION,
  PUBLIC_THEME_IDS,
  THEME_TOKEN_NAMES,
  UiButton,
  UiCard,
  UiDialog,
  UiInput,
  UiStack,
  UiSwitch,
]

const token: ThemeTokenName = 'accent'
void token

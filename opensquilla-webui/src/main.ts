import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { createPublicWebUiRouter } from './router'
import i18n from './i18n'
import { getPlatform } from './platform'
import {
  PUBLIC_WEB_UI_COMPOSITION_KEY,
  createPublicWebUiComposition,
  getPublicWebUiRuntimeState,
} from './composition/root'
import 'katex/dist/katex.min.css'
import './assets/fonts.css'
import '@opensquilla/ui-tokens/foundation.css'
import '@opensquilla/ui-tokens/themes.css'
import '@opensquilla/ui-primitives/styles.css'
import './assets/base.css'
import './styles/control-visual-system.css'
import './styles/route-fx.css'
import './styles/chat-markdown.css'
import './styles/chat-shared.css'
import './styles/apple-modern.css'

async function bootstrap(): Promise<void> {
  const app = createApp(App)
  const pinia = createPinia()
  const platform = getPlatform()
  app.use(pinia)
  app.use(i18n)

  const composition = await createPublicWebUiComposition({ pinia, platform })
  const router = createPublicWebUiRouter(composition, platform)
  const { appStore, rpcStore } = getPublicWebUiRuntimeState(composition)
  app.provide(PUBLIC_WEB_UI_COMPOSITION_KEY, composition)
  app.use(router)

  appStore.initTheme()
  rpcStore.init()
  router.afterEach(() => {
    rpcStore.applyLinkTokenFromUrl()
  })

  // Resolve + load the active locale before mounting so the first paint is
  // already in the right language (no English flash). Preserve the historical
  // mount-on-locale-failure behavior: locale loading is not allowed to turn a
  // usable community console into a blank startup error.
  try {
    await appStore.initLocale()
  } finally {
    app.mount('#app')
  }

  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      void composition.dispose()
    })
  }
}

void bootstrap().catch((error: unknown) => {
  console.error('OpenSquilla WebUI composition failed to start', error)
  const root = document.getElementById('app')
  if (!root) return
  root.textContent = 'OpenSquilla could not start. Reload the page or update the client.'
  root.setAttribute('role', 'alert')
})

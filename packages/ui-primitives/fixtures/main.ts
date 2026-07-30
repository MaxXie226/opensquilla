import { createApp } from 'vue'

import '../../ui-tokens/dist/foundation.css'
import '../../ui-tokens/dist/themes.css'
import '../dist/styles.css'
import './fixture.css'
import FixtureApp from './FixtureApp.vue'

createApp(FixtureApp).mount('#app')

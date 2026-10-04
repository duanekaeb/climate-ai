import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { installSessionHandlers, useAuth } from './stores/auth'
import './style.css'

const pinia = createPinia()
const app = createApp(App).use(pinia)
installSessionHandlers(router)
// Sign back in from the refresh cookie while the app mounts; the first route guard waits for it.
void useAuth(pinia).bootstrap()
app.use(router).mount('#app')

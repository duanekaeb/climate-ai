import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuth } from '@/stores/auth'

// meta.nav: 'primary' = bottom bar on phones / top of the sidebar; 'more' = under More.
export const routes: RouteRecordRaw[] = [
  { path: '/', name: 'live', component: () => import('@/views/LiveView.vue'), meta: { title: 'Live', nav: 'primary', icon: 'live' } },
  { path: '/rooms', name: 'rooms', component: () => import('@/views/RoomsView.vue'), meta: { title: 'Rooms', nav: 'primary', icon: 'rooms' } },
  { path: '/runtime', name: 'runtime', component: () => import('@/views/RuntimeView.vue'), meta: { title: 'Runtime', nav: 'primary', icon: 'runtime' } },
  { path: '/results', name: 'results', component: () => import('@/views/ResultsView.vue'), meta: { title: 'Did it work?', nav: 'primary', icon: 'results' } },
  { path: '/coupling', name: 'coupling', component: () => import('@/views/CouplingView.vue'), meta: { title: 'Floor coupling', nav: 'more', icon: 'coupling' } },
  { path: '/experiments', name: 'experiments', component: () => import('@/views/ExperimentsView.vue'), meta: { title: 'Experiments', nav: 'more', icon: 'experiments' } },
  { path: '/model', name: 'model', component: () => import('@/views/ModelView.vue'), meta: { title: 'Model', nav: 'more', icon: 'model' } },
  { path: '/ask', name: 'ask', component: () => import('@/views/AskView.vue'), meta: { title: 'Ask Claude', nav: 'more', icon: 'ask' } },
  { path: '/guardrails', name: 'guardrails', component: () => import('@/views/GuardrailsView.vue'), meta: { title: 'Guardrails', nav: 'more', icon: 'guardrails' } },
  { path: '/reports', name: 'reports', component: () => import('@/views/ReportsView.vue'), meta: { title: 'Reports', nav: 'more', icon: 'reports' } },
  { path: '/setup', name: 'setup', component: () => import('@/views/SetupView.vue'), meta: { title: 'Setup', nav: 'more', icon: 'setup' } },
  { path: '/security', name: 'security', component: () => import('@/views/SecurityView.vue'), meta: { title: 'Security', nav: 'more', icon: 'lock' } },
  { path: '/more', name: 'more', component: () => import('@/views/MoreView.vue'), meta: { title: 'More' } },
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { title: 'Sign in', public: true } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({ history: createWebHistory(), routes })

// The first navigation waits for bootstrap() (refresh cookie -> access token), so a reload or
// the iOS app relaunch lands where it was instead of on the sign-in page.
router.beforeEach(async (to) => {
  const auth = useAuth()
  if (!auth.loaded) await auth.bootstrap()
  if (to.meta.public) return true
  if (!auth.signedIn) return { path: '/login', query: { next: to.fullPath } }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${String(to.meta.title)} · Climate AI` : 'Climate AI'
})

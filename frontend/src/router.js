import { createRouter, createWebHashHistory } from 'vue-router'
import CandidateView from './views/CandidateView.vue'
import AdminView from './views/AdminView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/c' },
    { path: '/c', component: CandidateView },
    { path: '/admin', component: AdminView }
  ]
})

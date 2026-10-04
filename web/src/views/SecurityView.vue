<script setup lang="ts">
// Security (More -> Security): change the owner password, see and sign out signed-in
// devices, create and revoke API tokens for services, and read the recent audit trail.
// Spec: docs/specs/users-and-tokens.md.
import { onMounted } from 'vue'
import ApiTokensCard from '@/components/security/ApiTokensCard.vue'
import AuditLogCard from '@/components/security/AuditLogCard.vue'
import ChangePasswordCard from '@/components/security/ChangePasswordCard.vue'
import DevicesCard from '@/components/security/DevicesCard.vue'
import { useSecurity } from '@/stores/security'

const security = useSecurity()

onMounted(() => {
  security.loadSessions()
  security.loadTokens()
  security.loadAudit()
})
</script>

<template>
  <div class="grid gap-4 lg:grid-cols-2 lg:items-start">
    <div class="space-y-4">
      <DevicesCard />
      <ChangePasswordCard />
    </div>
    <div class="space-y-4">
      <ApiTokensCard />
    </div>
    <div class="lg:col-span-2">
      <AuditLogCard />
    </div>
  </div>
</template>

<script setup lang="ts">
// Small inline icon set (stroke icons, 24x24). Add paths here rather than pulling an icon library.
const props = defineProps<{ name: string; size?: number }>()
const PATHS: Record<string, string> = {
  live: 'M3 12h4l3-8 4 16 3-8h4',
  rooms: 'M3 10.5 12 3l9 7.5V21H3zM9 21v-6h6v6',
  runtime: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
  results: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  coupling: 'M4 18h16M4 18V8l8-5 8 5v10M9 13h6',
  experiments: 'M9 3h6M10 3v6L4 19a1 1 0 0 0 .9 1.5h14.2A1 1 0 0 0 20 19l-6-10V3',
  model: 'M4 7h16M4 12h10M4 17h7M18 14l3 3-3 3',
  ask: 'M21 12a8 8 0 0 1-11.8 7L4 20l1.2-4.4A8 8 0 1 1 21 12z',
  guardrails: 'M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z',
  reports: 'M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6',
  setup: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5 5 3.6 3.6M20.4 20.4 19 19M5 19l-1.4 1.4M20.4 3.6 19 5M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z',
  person: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0',
  sleep: 'M4 12h6l-6 7h6M14 5h5l-5 6h5',
  flame: 'M12 22a7 7 0 0 0 7-7c0-4-3-6-4-10-2 2-3 4-3 6-1-1-2-2-2-4-2 2-5 5-5 8a7 7 0 0 0 7 7z',
  snow: 'M12 2v20M4.9 4.9l14.2 14.2M2 12h20M4.9 19.1 19.1 4.9',
  fan: 'M12 12c0-4 1-8 4-8s3 4-4 8zm0 0c4 0 8 1 8 4s-4 3-8-4zm0 0c0 4-1 8-4 8s-3-4 4-8zm0 0c-4 0-8-1-8-4s4-3 8 4z',
  alert: 'M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  check: 'M20 6 9 17l-5-5',
  x: 'M18 6 6 18M6 6l12 12',
  refresh: 'M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5',
  lock: 'M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4M12 15v2',
  key: 'M14.5 9.5a4 4 0 1 0-1.4 3L21 20.4M17 17l2-2M19 19l2-2',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  phone: 'M8 2h8a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM11 18h2',
  laptop: 'M5 5h14v10H5zM2 19h20',
}
</script>

<template>
  <svg :width="props.size ?? 20" :height="props.size ?? 20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path :d="PATHS[props.name] ?? PATHS.more" />
  </svg>
</template>

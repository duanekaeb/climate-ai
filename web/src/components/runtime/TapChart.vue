<script setup lang="ts">
// EChart plus "tap a category": emits the x-category index under a click or tap anywhere
// inside the plot area (not only on a bar, which is too small to hit on a phone).
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { getInstanceByDom, type ECharts, type EChartsCoreOption, type ElementEvent } from 'echarts/core'
import EChart from '@/components/EChart.vue'

const props = withDefaults(defineProps<{ option: EChartsCoreOption; height?: string }>(), { height: '260px' })
const emit = defineEmits<{ tap: [index: number] }>()

const wrap = ref<HTMLDivElement | null>(null)
let chart: ECharts | undefined

function onClick(e: ElementEvent) {
  if (!chart) return
  const px: [number, number] = [e.offsetX, e.offsetY]
  if (!chart.containPixel({ gridIndex: 0 }, px)) return
  const v = chart.convertFromPixel({ gridIndex: 0 }, px)
  const x = Array.isArray(v) ? v[0] : v
  if (typeof x === 'number' && Number.isFinite(x)) emit('tap', Math.round(x))
}

onMounted(() => {
  // EChart (a child) has already initialised its instance on the inner element.
  const el = wrap.value?.firstElementChild
  if (el instanceof HTMLElement) chart = getInstanceByDom(el)
  chart?.getZr().on('click', onClick)
})
onBeforeUnmount(() => {
  chart?.getZr().off('click', onClick)
  chart = undefined
})
</script>

<template>
  <div ref="wrap" class="min-w-0 cursor-pointer"><EChart :option="props.option" :height="props.height" /></div>
</template>

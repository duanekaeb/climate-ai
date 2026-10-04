<script setup lang="ts">
// Minimal ECharts wrapper: tree-shaken imports, resizes with its container, re-renders on
// option or theme change. Pass a full EChartsOption; colors should come from cssVar().
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, ScatterChart, HeatmapChart, CustomChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
  MarkAreaComponent,
  DataZoomComponent,
  VisualMapComponent,
  DatasetComponent,
} from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

echarts.use([
  BarChart, LineChart, ScatterChart, HeatmapChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, MarkAreaComponent,
  DataZoomComponent, VisualMapComponent, DatasetComponent, SVGRenderer,
])

const props = withDefaults(defineProps<{ option: echarts.EChartsCoreOption; height?: string }>(), { height: '260px' })
const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let ro: ResizeObserver | null = null
let mo: MutationObserver | null = null

function render() {
  if (!el.value) return
  if (!chart) chart = echarts.init(el.value, undefined, { renderer: 'svg' })
  chart.setOption(props.option, true)
}

onMounted(() => {
  render()
  ro = new ResizeObserver(() => chart?.resize())
  if (el.value) ro.observe(el.value)
  // re-render when the dark class flips so cssVar() colors update
  mo = new MutationObserver(() => render())
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})
watch(() => props.option, render, { deep: true })
onBeforeUnmount(() => {
  ro?.disconnect()
  mo?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="el" :style="{ height, width: '100%' }" class="min-w-0" />
</template>

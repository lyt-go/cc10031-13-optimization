<template>
  <section class="domain-panel" data-testid="calibration-panel">
    <header class="panel-head">
      <div>
        <h3>批量标定续做</h3>
        <p class="page-desc">
          勾选待标定测点一次提交。每台独立计算，缺源或规则刚改的测点单独列出，可按键续做，已成功的不重算。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reload-points" @click="loadPoints">刷新测点</button>
        <button
          class="btn primary"
          type="button"
          data-testid="run-batch"
          :disabled="!selected.length || running"
          @click="runBatch(false)"
        >
          {{ running ? '计算中…' : `提交批量标定（${selected.length}）` }}
        </button>
        <button
          v-if="batch"
          class="btn"
          type="button"
          data-testid="resume-batch"
          :disabled="running"
          @click="runBatch(true)"
        >
          续做本批次
        </button>
      </div>
    </header>

    <p v-if="pointError" class="error-text">{{ pointError }}</p>

    <div class="point-picker">
      <label v-for="item in points" :key="item.point" class="point-chip" :class="{ 'is-picked': selected.includes(item.point) }">
        <input v-model="selected" type="checkbox" :value="item.point" :data-testid="`pick-${item.point}`" />
        <span class="point-name">{{ item.point }}</span>
        <span class="mini-tag" :class="sourceClass(item.source_status)">源：{{ item.source_status }}</span>
        <span class="mini-tag" :class="item.usable_version_count ? 'ok' : 'muted'">
          可用版本 {{ item.usable_version_count }}
        </span>
        <span v-if="item.adopted_scale !== null" class="mini-tag muted">系数 {{ item.adopted_scale }}</span>
      </label>
      <p v-if="!points.length" class="empty-state">暂无标定记录对应的测点，请先在下方列表登记。</p>
    </div>

    <aside v-if="batch" class="batch-card" data-testid="batch-report">
      <div class="batch-head">
        <strong>批次 {{ batch.batch_key }}</strong>
        <span class="muted">第 {{ batch.run_count }} 次执行 · 更新于 {{ batch.updated_at }}</span>
        <span class="mini-tag ok" v-for="(value, key) in batch.summary.counts" :key="key">{{ key }} {{ value }}</span>
      </div>

      <div class="batch-columns">
        <div class="batch-col">
          <h4>完整数据</h4>
          <ul>
            <li v-for="item in batch.summary.complete_data" :key="item.point" :data-testid="`complete-${item.point}`">
              <strong>{{ item.point }}</strong>
              系数 {{ item.scale }} → 首值 {{ item.value_sample }}
              <span class="muted">（{{ item.records }} 条，版本 {{ item.version_key }}）</span>
            </li>
            <li v-if="!batch.summary.complete_data.length" class="muted">无</li>
          </ul>
        </div>
        <div class="batch-col">
          <h4>可继续的范围</h4>
          <ul>
            <li v-for="item in batch.summary.resumable_scope" :key="item.point" :data-testid="`resume-${item.point}`">
              <strong>{{ item.point }}</strong>
              <span class="mini-tag warn">{{ item.status }}</span>
              <span class="muted">{{ item.reason }}</span>
            </li>
            <li v-if="!batch.summary.resumable_scope.length" class="muted">无</li>
          </ul>
        </div>
        <div class="batch-col">
          <h4>需复核记录</h4>
          <ul>
            <li v-for="item in batch.summary.review_records" :key="item.point" :data-testid="`review-${item.point}`">
              <strong>{{ item.point }}</strong>
              <span class="mini-tag danger">需复核</span>
              <span class="muted">{{ item.reason }}</span>
            </li>
            <li v-if="!batch.summary.review_records.length" class="muted">无</li>
          </ul>
        </div>
      </div>

      <table class="data-table compact">
        <thead>
          <tr>
            <th>测点</th><th>本次结果</th><th>系数</th><th>首值</th><th>条数</th>
            <th>版本</th><th>源文件</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in batch.results" :key="item.point" :data-testid="`batch-${item.point}`">
            <td>{{ item.point }}</td>
            <td><span class="mini-tag" :class="statusClass(item.status)">{{ item.status }}</span></td>
            <td>
              {{ item.scale ?? '—' }}
              <span
                v-if="item.adopted_scale !== null && item.adopted_scale !== undefined && item.adopted_scale !== item.scale"
                class="muted"
              >（获准 {{ item.adopted_scale }}）</span>
            </td>
            <td>{{ item.value_sample ?? '—' }}</td>
            <td>{{ item.records }}</td>
            <td class="mono">{{ item.version_key || '—' }}</td>
            <td class="mono">{{ item.source_status }}</td>
            <td class="muted">{{ item.message }}</td>
          </tr>
        </tbody>
      </table>
    </aside>

    <aside v-if="versions" class="versions-card" data-testid="versions">
      <div class="batch-head">
        <strong>{{ versions.point }} 的可用版本</strong>
        <span class="mini-tag" :class="versions.usable_version_count === 1 ? 'ok' : 'danger'">
          当前可用 {{ versions.usable_version_count }} 份
        </span>
        <button class="link" type="button" @click="versions = null">收起</button>
      </div>
      <p v-if="versions.active_version" class="muted" data-testid="active-version">
        当前版本 {{ versions.active_version.version_key }}：系数 {{ versions.active_version.scale }}，
        首值 {{ versions.active_version.value_sample }}，{{ versions.active_version.records }} 条。
      </p>
      <table class="data-table compact">
        <thead>
          <tr><th>版本</th><th>标定记录</th><th>系数</th><th>首值</th><th>状态</th><th>取代</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in versions.versions" :key="item.version_key">
            <td class="mono">{{ item.version_key }}</td>
            <td>{{ item.calibrate_id }}</td>
            <td>{{ item.scale }}</td>
            <td>{{ item.value_sample }}</td>
            <td><span class="mini-tag" :class="item.state === 'active' ? 'ok' : 'muted'">{{ item.state }}</span></td>
            <td class="mono">{{ item.supersedes || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </aside>

    <p v-if="batchMessage" class="batch-message" data-testid="batch-message">{{ batchMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  listCalibrationPoints,
  pointVersions,
  runBatchCalibration,
} from '@/api/domain'
import type { BatchResult, CalibrationPoint, PointVersions } from '@/api/domain'

const emit = defineEmits<{ (event: 'batch-done', payload: BatchResult): void }>()

const points = ref<CalibrationPoint[]>([])
const selected = ref<string[]>([])
const batch = ref<BatchResult | null>(null)
const versions = ref<PointVersions | null>(null)
const running = ref(false)
const batchMessage = ref('')
const pointError = ref('')

function sourceClass(status: string) {
  if (status === '可读') return 'ok'
  return 'warn'
}

function statusClass(status: string) {
  if (status === '成功' || status === '已就绪') return 'ok'
  if (status === '缺源' || status === '待标定') return 'warn'
  if (status === '需复核' || status === '规则无效') return 'danger'
  return 'muted'
}

async function loadPoints() {
  pointError.value = ''
  try {
    points.value = await listCalibrationPoints()
    selected.value = selected.value.filter((point) =>
      points.value.some((item) => item.point === point))
  } catch (error) {
    pointError.value = error instanceof Error ? error.message : '测点读取失败'
  }
}

async function runBatch(resume: boolean) {
  if (!selected.value.length) return
  running.value = true
  batchMessage.value = ''
  pointError.value = ''
  try {
    const result = await runBatchCalibration(
      selected.value, resume && batch.value ? batch.value.batch_key : null)
    batch.value = result
    batchMessage.value = result.message || ''
    await loadPoints()
    const target = result.results.find((item) => item.version_key)
    if (target) versions.value = await pointVersions(target.point)
    emit('batch-done', result)
  } catch (error) {
    pointError.value = error instanceof Error ? error.message : '批量标定失败'
  } finally {
    running.value = false
  }
}

onMounted(loadPoints)
</script>

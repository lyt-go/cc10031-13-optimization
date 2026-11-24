<template>
  <section class="domain-panel" data-testid="align-panel">
    <header class="panel-head">
      <div>
        <h3>多测点时间对齐</h3>
        <p class="page-desc">
          先发起对齐并冻结版本快照，提交时按各测点最新获准的标定版本重算。
          每个时格记录实际采用的标定版本，提交后可逐格复验取值与出处。
        </p>
      </div>
      <div class="page-actions">
        <label class="inline-field">
          <span>对齐规则</span>
          <select v-model.number="alignId" data-testid="align-rule">
            <option v-for="rule in alignRules" :key="rule.id" :value="rule.id">
              {{ rule.规则编号 }}（粒度 {{ rule.对齐粒度 }}）
            </option>
          </select>
        </label>
        <button
          class="btn"
          type="button"
          data-testid="propose-align"
          :disabled="!selected.length || proposing"
          @click="propose"
        >
          {{ proposing ? '发起中…' : `发起对齐（${selected.length} 点）` }}
        </button>
        <button
          class="btn primary"
          type="button"
          data-testid="submit-align"
          :disabled="!draft || !selected.length || submitting"
          @click="submit"
        >
          {{ submitting ? '提交中…' : '提交这次对齐' }}
        </button>
      </div>
    </header>

    <div class="point-picker">
      <label v-for="item in points" :key="item.point" class="point-chip" :class="{ 'is-picked': selected.includes(item.point) }">
        <input v-model="selected" type="checkbox" :value="item.point" :data-testid="`align-pick-${item.point}`" />
        <span class="point-name">{{ item.point }}</span>
        <span class="mini-tag" :class="item.usable_version_count ? 'ok' : 'warn'">
          {{ item.usable_version_count ? '有可用标定' : '缺标定产物' }}
        </span>
      </label>
    </div>

    <p v-if="draft" class="draft-note" data-testid="align-draft">
      已发起 {{ draft.draft_id }}，快照：{{ snapshotText }}
    </p>

    <p v-if="errorMessage" class="error-text" data-testid="align-error">{{ errorMessage }}</p>
    <p v-if="message" class="batch-message" data-testid="align-message">{{ message }}</p>

    <aside v-if="result" class="batch-card" data-testid="align-result">
      <div class="batch-head">
        <strong>提交结果 {{ result.run_id }}</strong>
        <span class="muted">粒度 {{ result.align_granularity }} · {{ result.aligned_count }} 条</span>
        <span class="muted">时格 {{ result.grid.start }} ~ {{ result.grid.end }}（{{ result.grid.slot_count }} 格）</span>
      </div>
      <p v-if="result.revalidated.length" class="review-line" data-testid="align-revalidated">
        <span class="mini-tag danger">版本变化</span>
        <span v-for="item in result.revalidated" :key="item.point">
          {{ item.point }}：发起时系数 {{ item.before.scale }} → 提交按 {{ item.after.scale }} 重算
        </span>
      </p>
      <p v-if="result.excluded.length" class="review-line" data-testid="align-excluded">
        <span class="mini-tag warn">未纳入</span>
        <span v-for="item in result.excluded" :key="item.point">
          {{ item.point }}（{{ item.status }}：{{ item.reason }}）
        </span>
      </p>
      <ul class="grid-list">
        <li v-for="item in result.points" :key="item.point" :data-testid="`align-point-${item.point}`">
          <strong>{{ item.point }}</strong>
          系数 {{ item.scale }} · 首值 {{ item.value_sample }} · 版本
          <code>{{ item.version_key }}</code>
          <span v-if="item.version_changed_since_draft" class="mini-tag warn">按最新获准版本</span>
        </li>
      </ul>
    </aside>

    <aside v-if="latest" class="slots-card" data-testid="align-slots">
      <div class="batch-head">
        <strong>已提交对齐结果</strong>
        <span class="muted">运行 {{ latest.run_id }} · {{ latest.submitted_at }}</span>
        <button class="link" type="button" @click="loadLatest">刷新</button>
      </div>
      <table class="data-table compact">
        <thead>
          <tr>
            <th>时格（{{ latest.align_granularity }}）</th>
            <th v-for="item in latest.points" :key="item.point">{{ item.point }} 取值 / 出处</th>
            <th>复验</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="slot in latest.slots" :key="slot.aligned_timestamp" :data-testid="`slot-${slot.aligned_timestamp}`">
            <td class="mono">{{ slot.aligned_timestamp }}</td>
            <td v-for="item in latest.points" :key="item.point" class="mono">
              <template v-if="slot.points[item.point]">
                {{ slot.points[item.point].value }}
                <span class="muted">
                  ×{{ slot.points[item.point].scale }}
                  · 记录 {{ slot.points[item.point].calibrate_id }}
                  · {{ shortVersion(slot.points[item.point].calibrate_version) }}
                </span>
              </template>
              <template v-else>—</template>
            </td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`verify-${slot.aligned_timestamp}`"
                @click="verify(slot.aligned_timestamp)"
              >
                复验此时格
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="verification" class="verify-card" data-testid="verify-result">
        <p>
          <strong>{{ verification.ok ? '取值与出处一致' : '存在不一致' }}</strong>
          <span class="muted">（时格 {{ verification.alignedTimestamp }}）</span>
          <span v-if="verification.stale.length" class="mini-tag warn">
            有更新获准版本：{{ verification.stale.join('、') }}
          </span>
        </p>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>测点</th><th>结果取值</th><th>按出处重算</th><th>标定记录</th>
              <th>版本</th><th>取值一致</th><th>出处一致</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="check in verification.checks" :key="check.point" :data-testid="`verify-${check.point}`">
              <td>{{ check.point }}</td>
              <td>{{ check.value }}</td>
              <td>{{ check.recomputed_value }}</td>
              <td>{{ check.calibrate_id }}（×{{ check.scale }}）</td>
              <td class="mono">{{ shortVersion(check.calibrate_version) }}</td>
              <td><span class="mini-tag" :class="check.value_ok ? 'ok' : 'danger'">{{ check.value_ok ? '一致' : '不一致' }}</span></td>
              <td><span class="mini-tag" :class="check.provenance_ok ? 'ok' : 'danger'">{{ check.provenance_ok ? '一致' : '不一致' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </aside>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  latestAlignment,
  listCalibrationPoints,
  proposeAlignment,
  submitAlignment,
  verifyAlignment,
} from '@/api/domain'
import type {
  AlignDraft,
  AlignLatest,
  AlignSubmission,
  CalibrationPoint,
  VerifyCheck,
} from '@/api/domain'
import { listEntries } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const points = ref<CalibrationPoint[]>([])
const selected = ref<string[]>([])
const alignRules = ref<EntryRow[]>([])
const alignId = ref<number>(1)
const draft = ref<AlignDraft | null>(null)
const result = ref<AlignSubmission | null>(null)
const latest = ref<AlignLatest | null>(null)
const proposing = ref(false)
const submitting = ref(false)
const errorMessage = ref('')
const message = ref('')
const verification = ref<{
  ok: boolean
  alignedTimestamp: string
  checks: VerifyCheck[]
  stale: string[]
} | null>(null)

const snapshotText = computed(() =>
  (draft.value?.version_snapshot || [])
    .map((item) => `${item.point} 系数 ${item.scale ?? '—'}`)
    .join('，') || '—')

function shortVersion(version: string | undefined | null) {
  if (!version) return '—'
  const parts = String(version).split('-')
  return parts.length > 2 ? `${parts[0]}-${parts[1]}` : String(version)
}

async function loadPoints() {
  try {
    points.value = await listCalibrationPoints()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '测点读取失败'
  }
}

async function loadRules() {
  try {
    const payload = await listEntries('time_align', {})
    const usable = payload.items.filter((row) => row.status !== '已废弃')
    alignRules.value = usable.length ? usable : payload.items
    if (alignRules.value.length && !alignRules.value.some((row) => row.id === alignId.value)) {
      alignId.value = Number(alignRules.value[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对齐规则读取失败'
  }
}

async function loadLatest() {
  if (!alignId.value) return
  try {
    latest.value = await latestAlignment(alignId.value)
  } catch {
    latest.value = null
  }
}

async function propose() {
  proposing.value = true
  errorMessage.value = ''
  message.value = ''
  verification.value = null
  try {
    const payload = await proposeAlignment(alignId.value, selected.value)
    if (!payload.ok) throw new Error(payload.message)
    draft.value = payload.draft
    result.value = null
    message.value = payload.message
    await loadLatest()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发起对齐失败'
  } finally {
    proposing.value = false
  }
}

async function submit() {
  if (!draft.value) return
  submitting.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await submitAlignment(alignId.value, selected.value, draft.value.draft_id)
    result.value = payload
    draft.value = null
    message.value = payload.message
    await Promise.all([loadLatest(), loadPoints()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交对齐失败'
  } finally {
    submitting.value = false
  }
}

async function verify(slot: string) {
  try {
    const payload = await verifyAlignment(alignId.value, slot)
    verification.value = {
      ok: payload.ok,
      alignedTimestamp: slot,
      checks: payload.checks,
      stale: payload.stale_points,
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复验失败'
  }
}

onMounted(async () => {
  await Promise.all([loadPoints(), loadRules()])
  await loadLatest()
})
</script>

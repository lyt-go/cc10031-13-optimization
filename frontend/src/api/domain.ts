/** 批量标定续做与时间对齐的领域接口封装。 */
import { request } from './client'

export type CalibrationPoint = {
  point: string
  approved: number
  pending: number
  adopted_calibrate_id: number | null
  adopted_scale: string | number | null
  active_version: string | null
  usable_version_count: number
  source_status: string
}

export type BatchPointResult = {
  point: string
  status: string
  message: string
  scale: number | null
  artifact_scale?: number | null
  adopted_scale?: number | null
  offset: number | null
  records: number
  value_sample: number | null
  version_key: string | null
  calibrate_id: number | null
  标定编号: string | null
  artifact_version: string | null
  source_status: string
  source_file: string
  output_file: string
  previous?: Record<string, unknown> | null
}

export type BatchSummary = {
  counts: Record<string, number>
  complete_data: {
    point: string
    calibrate_id: number | null
    version_key: string
    scale: number
    records: number
    value_sample: number | null
    output_file: string
  }[]
  resumable_scope: { point: string; status: string; reason: string }[]
  review_records: {
    point: string
    reason: string
    artifact_version: string | null
    adopted_version: string | null
    previous: Record<string, unknown> | null
  }[]
  succeeded_points: string[]
  already_ready_points: string[]
  missing_source_points: string[]
  review_points: string[]
  waiting_rule_points: string[]
}

export type BatchResult = {
  ok: boolean
  message?: string
  batch_key: string
  resumed: boolean
  run_count: number
  updated_at: string
  requested_points: string[]
  results: BatchPointResult[]
  summary: BatchSummary
}

export type PointVersions = {
  point: string
  usable_version_count: number
  active_version: {
    version_key: string
    calibrate_id: number
    scale: number
    offset: number
    records: number
    value_sample: number | null
    state: string
    produced_at: string
    supersedes: string | null
  } | null
  versions: {
    version_key: string
    calibrate_id: number
    scale: number
    value_sample: number | null
    records: number
    state: string
    produced_at: string
    supersedes: string | null
  }[]
  approved_records: {
    id: number
    标定编号: string
    换算系数: string
    零点偏移: string
    标定时间: string
    status: string
  }[]
  adopted_calibrate_id: number | null
  source: { path: string; status: string }
}

export type AlignPointVersion = {
  point: string
  status: string
  calibrate_id: number | null
  scale: number | null
  version_key?: string
  source_digest?: string
  value_sample?: number | null
  version_changed_since_draft?: boolean
  change_reason?: string
}

export type AlignDraft = {
  draft_id: string
  align_id: number
  points: string[]
  version_snapshot: AlignPointVersion[]
  created_at: string
  status: string
}

export type AlignSubmission = {
  ok: boolean
  run_id: string
  align_rule: string
  align_granularity: string
  grid: {
    start: string | null
    end: string | null
    slot_count: number
    coverage: Record<string, { slots_with_value: number; missing_slots: number }>
  }
  points: AlignPointVersion[]
  excluded: { point: string; status: string; reason: string }[]
  revalidated: { point: string; before: AlignPointVersion; after: AlignPointVersion; reason: string }[]
  aligned_count: number
  unaligned: number
  output_file: string
  message: string
}

export type AlignSlot = {
  aligned_timestamp: string
  points: Record<string, {
    value: number | null
    raw_value_mV: number | null
    source_timestamp: string
    calibrate_id: number
    标定编号: string
    calibrate_version: string
    scale: number
    offset: number
    source_digest: string
    record_digest: string
  }>
}

export type AlignLatest = {
  ok: boolean
  run_id: string
  align_rule: string
  align_granularity: string
  submitted_at: string
  points: AlignPointVersion[]
  excluded: { point: string; status: string; reason: string }[]
  revalidated: { point: string; before: AlignPointVersion; after: AlignPointVersion; reason: string }[]
  coverage: Record<string, { slots_with_value: number; missing_slots: number }>
  slots: AlignSlot[]
  output_file: string
}

export type VerifyCheck = {
  point: string
  aligned_timestamp: string
  value: number | null
  recomputed_value: number | null
  calibrate_id: number
  calibrate_version: string
  scale: number
  digest_ok: boolean
  value_ok: boolean
  provenance_ok: boolean
  version_still_current: boolean
  ok: boolean
}

async function send<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail || payload?.message || `接口返回 ${response.status}`)
  }
  return payload as T
}

export async function listCalibrationPoints(): Promise<CalibrationPoint[]> {
  const payload = await send<{ items: CalibrationPoint[] }>('/api/unit_calibrate/points')
  return payload.items
}

export function pointVersions(point: string): Promise<PointVersions> {
  return send<PointVersions>(`/api/unit_calibrate/points/${encodeURIComponent(point)}/versions`)
}

export async function runBatchCalibration(
  pointKeys: string[], batchKey?: string | null,
): Promise<BatchResult> {
  const payload = await send<{ ok: boolean; message: string; detail: BatchResult }>(
    '/api/unit_calibrate/batch',
    {
      method: 'POST',
      body: JSON.stringify({
        point_keys: pointKeys,
        batch_key: batchKey || null,
        resume: Boolean(batchKey),
      }),
    },
  )
  if (!payload.ok) throw new Error(payload.message)
  return payload.detail
}

export function batchState(batchKey: string): Promise<BatchResult> {
  return send<BatchResult>(`/api/unit_calibrate/batch/${encodeURIComponent(batchKey)}`)
}

export function proposeAlignment(
  alignId: number, pointKeys: string[],
): Promise<{ ok: boolean; message: string; draft: AlignDraft }> {
  return send('/api/time_align/' + alignId + '/propose', {
    method: 'POST',
    body: JSON.stringify({ point_keys: pointKeys, operator: '值班员' }),
  })
}

export async function submitAlignment(
  alignId: number, pointKeys: string[], draftId: string,
): Promise<AlignSubmission> {
  const payload = await send<{ ok: boolean; message: string; detail: AlignSubmission }>(
    `/api/time_align/${alignId}/submit`,
    {
      method: 'POST',
      body: JSON.stringify({ point_keys: pointKeys, draft_id: draftId, operator: '值班员' }),
    },
  )
  if (!payload.ok) throw new Error(payload.message)
  return payload.detail
}

export function latestAlignment(alignId: number): Promise<AlignLatest> {
  return send<AlignLatest>(`/api/time_align/${alignId}/results/latest`)
}

export async function verifyAlignment(
  alignId: number, alignedTimestamp: string, point?: string,
): Promise<{ ok: boolean; message: string; checks: VerifyCheck[]; stale_points: string[] }> {
  const payload = await send<{
    ok: boolean
    message: string
    detail: { checks: VerifyCheck[]; stale_points: string[] }
  }>(`/api/time_align/${alignId}/verify`, {
    method: 'POST',
    body: JSON.stringify({ aligned_timestamp: alignedTimestamp, point: point || null }),
  })
  return { ...payload.detail, ok: payload.ok, message: payload.message }
}

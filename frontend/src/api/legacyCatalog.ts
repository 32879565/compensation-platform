import { api } from './client'

export type LegacyCandidateClassification =
  'NEEDS_HR_CONFIRMATION' | 'DERIVED_NOT_CATALOG_COMPONENT'

export interface LegacyCatalogSource {
  record_count: number
  period_from: string | null
  period_to: string | null
  snapshot_id: string
}

export interface LegacyComponentCandidate {
  source_field: string
  record_count: number
  nonzero_count: number
  period_from: string
  period_to: string
  suggested_component_type: string | null
  suggested_allowance_kind: null
  classification: LegacyCandidateClassification
  importable: boolean
  applied: boolean
  applied_target_id: number | null
  note: string
}

export interface LegacyGradeCandidate {
  position: string
  record_count: number
  contributor_count: number
  salary_sample_count: number
  period_from: string
  period_to: string
  observed_p25: string | null
  observed_median: string | null
  observed_p75: string | null
  suppressed_for_privacy: boolean
  applied: boolean
  applied_target_id: number | null
  is_official_grade: false
}

export interface LegacyCatalogPreview {
  source: LegacyCatalogSource
  component_candidates: LegacyComponentCandidate[]
  grade_source_status: 'OFFICIAL_MASTER_NOT_PRESENT'
  grade_candidates: LegacyGradeCandidate[]
  warnings: string[]
}

export interface LegacyComponentDefinition {
  code: string
  name: string
  component_type:
    | 'BASE'
    | 'COMPREHENSIVE'
    | 'PERFORMANCE'
    | 'POSITION'
    | 'ALLOWANCE'
    | 'HOUSING'
    | 'OVERTIME'
    | 'DEDUCTION'
  taxable?: boolean
  in_social_base?: boolean
  in_housing_base?: boolean
  prorate_by_attendance?: boolean
  allowance_kind?: 'FIXED' | 'FLOATING'
  sort_order?: number
}

export interface ApplyLegacyComponentInput {
  source_field: string
  expected_record_count: number
  expected_source_snapshot_id: string
  confirmed_by_hr: true
  reason: string
  component: LegacyComponentDefinition
}

export interface LegacyAppliedComponent extends LegacyComponentDefinition {
  id: number
  created_by: number
}

export interface ApplyLegacyGradeInput {
  source_position: string
  expected_record_count: number
  expected_source_snapshot_id: string
  policy_confirmation: 'HR_CONFIRMED'
  reason: string
  grade: { code: string; name: string; rank: number }
  band: {
    band_min: string
    band_mid: string
    band_max: string
    effective_from: string
  }
}

export interface LegacyGradeApplyResult {
  grade: { id: number; code: string; name: string; rank: number; version: number }
  band: {
    id: number
    job_grade_id: number
    band_min: string
    band_mid: string
    band_max: string
    effective_from: string
  }
  observed_history: {
    record_count: number
    contributor_count: number
    salary_sample_count: number
    observed_median: string
  }
}

export async function fetchLegacyCatalogPreview(): Promise<LegacyCatalogPreview> {
  return (await api.get<LegacyCatalogPreview>('/api/legacy-catalog/preview')).data
}

export async function applyLegacyComponent(
  payload: ApplyLegacyComponentInput,
): Promise<LegacyAppliedComponent> {
  return (await api.post<LegacyAppliedComponent>('/api/legacy-catalog/components/apply', payload))
    .data
}

export async function applyLegacyGrade(
  payload: ApplyLegacyGradeInput,
): Promise<LegacyGradeApplyResult> {
  return (await api.post<LegacyGradeApplyResult>('/api/legacy-catalog/grades/apply', payload)).data
}

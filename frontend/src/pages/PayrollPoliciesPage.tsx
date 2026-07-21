import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Popconfirm,
  Result,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import { useEffect, useState } from 'react'

import {
  createPayrollPolicy,
  fetchPayrollPolicies,
  finalizePayrollPolicy,
  updatePayrollPolicy,
  type ContributionKind,
  type DerivedIncomeRule,
  type PayrollPolicy,
  type PayrollPolicyInput,
  type SocialRule,
  type TaxBracket,
} from '../api/payrollPolicies'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

const CONTRIBUTION_KINDS: { value: ContributionKind; label: string }[] = [
  { value: 'PENSION', label: '养老保险' },
  { value: 'MEDICAL', label: '医疗保险' },
  { value: 'UNEMPLOYMENT', label: '失业保险' },
  { value: 'WORK_INJURY', label: '工伤保险' },
  { value: 'MATERNITY', label: '生育保险' },
  { value: 'HOUSING', label: '住房公积金' },
]

const DERIVED_INCOME_CODES: { value: DerivedIncomeRule['code']; label: string }[] = [
  { value: 'OVERTIME', label: '加班收入' },
  { value: 'HOLIDAY', label: '法定假日收入' },
]

interface PolicyFormValues {
  city: string
  effective_from: string
  monthly_basic_deduction: string
  social_rules: SocialRule[]
  tax_brackets: TaxBracket[]
  derived_income_rules: DerivedIncomeRule[]
}

function currentMonthStart(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`
}

function emptySocialRule(kind: ContributionKind): SocialRule {
  return {
    kind,
    employee_rate: '0',
    employer_rate: '0',
    base_min: '0',
    base_max: null,
  }
}

function emptyDerivedRule(code: DerivedIncomeRule['code']): DerivedIncomeRule {
  return { code, taxable: true, in_social_base: false, in_housing_base: false }
}

function initialFormValues(policy?: PayrollPolicy | null): PolicyFormValues {
  if (policy) {
    const socialByKind = new Map(policy.social_rules.map((rule) => [rule.kind, rule]))
    const derivedByCode = new Map(policy.derived_income_rules.map((rule) => [rule.code, rule]))
    return {
      city: policy.city,
      effective_from: policy.effective_from,
      monthly_basic_deduction: policy.monthly_basic_deduction,
      social_rules: CONTRIBUTION_KINDS.map(
        ({ value }) => socialByKind.get(value) ?? emptySocialRule(value),
      ),
      tax_brackets: policy.tax_brackets,
      derived_income_rules: DERIVED_INCOME_CODES.map(
        ({ value }) => derivedByCode.get(value) ?? emptyDerivedRule(value),
      ),
    }
  }
  return {
    city: '',
    effective_from: currentMonthStart(),
    monthly_basic_deduction: '5000',
    social_rules: CONTRIBUTION_KINDS.map(({ value }) => emptySocialRule(value)),
    tax_brackets: [{ upper_bound: null, rate: '0', quick_deduction: '0' }],
    derived_income_rules: DERIVED_INCOME_CODES.map(({ value }) => emptyDerivedRule(value)),
  }
}

function asText(value: unknown): string {
  return String(value ?? '').trim()
}

function asOptionalText(value: unknown): string | null {
  const text = asText(value)
  return text || null
}

function toPolicyInput(values: PolicyFormValues): PayrollPolicyInput {
  return {
    city: values.city.trim(),
    effective_from: values.effective_from,
    monthly_basic_deduction: asText(values.monthly_basic_deduction),
    social_rules: values.social_rules.map((rule) => ({
      kind: rule.kind,
      employee_rate: asText(rule.employee_rate),
      employer_rate: asText(rule.employer_rate),
      base_min: asText(rule.base_min),
      base_max: asOptionalText(rule.base_max),
    })),
    tax_brackets: values.tax_brackets.map((bracket) => ({
      upper_bound: asOptionalText(bracket.upper_bound),
      rate: asText(bracket.rate),
      quick_deduction: asText(bracket.quick_deduction),
    })),
    derived_income_rules: values.derived_income_rules.map((rule) => ({
      code: rule.code,
      taxable: Boolean(rule.taxable),
      in_social_base: Boolean(rule.in_social_base),
      in_housing_base: Boolean(rule.in_housing_base),
    })),
  }
}

function errorMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

interface PolicyFormModalProps {
  open: boolean
  policy: PayrollPolicy | null
  saving: boolean
  onCancel: () => void
  onSubmit: (payload: PayrollPolicyInput) => void
}

function PolicyFormModal({ open, policy, saving, onCancel, onSubmit }: PolicyFormModalProps) {
  const [form] = Form.useForm<PolicyFormValues>()

  useEffect(() => {
    if (open) form.setFieldsValue(initialFormValues(policy))
  }, [form, open, policy])

  return (
    <Modal
      title={policy ? `编辑草稿 #${policy.id}` : '新建城市薪税政策'}
      open={open}
      width={1120}
      destroyOnClose
      onCancel={onCancel}
      onOk={() => form.submit()}
      okText={policy ? '保存草稿' : '创建草稿'}
      okButtonProps={{ 'data-testid': 'policy-save-draft' }}
      confirmLoading={saving}
    >
      <Form
        data-testid="policy-form"
        form={form}
        layout="vertical"
        onFinish={(values) => onSubmit(toPolicyInput(values))}
      >
        <Space align="start" wrap style={{ width: '100%' }}>
          <Form.Item name="city" label="参保城市" rules={[{ required: true, whitespace: true }]}>
            <Input data-testid="policy-city" style={{ width: 200 }} />
          </Form.Item>
          <Form.Item
            name="effective_from"
            label="生效日期"
            rules={[
              { required: true },
              {
                validator: (_, value: string | undefined) =>
                  value?.endsWith('-01')
                    ? Promise.resolve()
                    : Promise.reject(new Error('政策生效日期必须是薪资月第一天')),
              },
            ]}
          >
            <Input type="date" style={{ width: 180 }} />
          </Form.Item>
          <Form.Item
            name="monthly_basic_deduction"
            label="每月基本减除费用"
            rules={[{ required: true }]}
          >
            <Input inputMode="decimal" style={{ width: 180 }} />
          </Form.Item>
        </Space>

        <h3>社保与公积金</h3>
        <Table
          size="small"
          pagination={false}
          rowKey="kind"
          dataSource={CONTRIBUTION_KINDS.map(({ value }) => ({ kind: value }))}
          columns={[
            {
              title: '险种',
              dataIndex: 'kind',
              render: (kind: ContributionKind) =>
                CONTRIBUTION_KINDS.find((item) => item.value === kind)?.label ?? kind,
            },
            {
              title: '个人比例',
              render: (_, __, index: number) => (
                <Form.Item
                  name={['social_rules', index, 'employee_rate']}
                  rules={[{ required: true }]}
                  style={{ marginBottom: 0 }}
                >
                  <Input inputMode="decimal" />
                </Form.Item>
              ),
            },
            {
              title: '单位比例',
              render: (_, __, index: number) => (
                <Form.Item
                  name={['social_rules', index, 'employer_rate']}
                  rules={[{ required: true }]}
                  style={{ marginBottom: 0 }}
                >
                  <Input inputMode="decimal" />
                </Form.Item>
              ),
            },
            {
              title: '缴费基数下限',
              render: (_, __, index: number) => (
                <Form.Item
                  name={['social_rules', index, 'base_min']}
                  rules={[{ required: true }]}
                  style={{ marginBottom: 0 }}
                >
                  <Input inputMode="decimal" />
                </Form.Item>
              ),
            },
            {
              title: '缴费基数上限（可空）',
              render: (_, __, index: number) => (
                <Form.Item name={['social_rules', index, 'base_max']} style={{ marginBottom: 0 }}>
                  <Input inputMode="decimal" />
                </Form.Item>
              ),
            },
          ]}
        />
        {CONTRIBUTION_KINDS.map(({ value }, index) => (
          <Form.Item key={value} name={['social_rules', index, 'kind']} hidden>
            <Input />
          </Form.Item>
        ))}

        <h3>累计预扣税率表</h3>
        <Form.List
          name="tax_brackets"
          rules={[
            {
              validator: async (_, rows: TaxBracket[] | undefined) => {
                if (!rows?.length) throw new Error('至少需要一个税率档位')
              },
            },
          ]}
        >
          {(fields, { add, remove }, { errors }) => (
            <>
              {fields.map((field, index) => (
                <Space key={field.key} align="start" wrap>
                  <Form.Item
                    {...field}
                    name={[field.name, 'upper_bound']}
                    label={index === 0 ? '累计应纳税所得额上限（末档留空）' : undefined}
                  >
                    <Input inputMode="decimal" placeholder="末档留空" style={{ width: 250 }} />
                  </Form.Item>
                  <Form.Item
                    {...field}
                    name={[field.name, 'rate']}
                    label={index === 0 ? '税率' : undefined}
                    rules={[{ required: true }]}
                  >
                    <Input inputMode="decimal" style={{ width: 120 }} />
                  </Form.Item>
                  <Form.Item
                    {...field}
                    name={[field.name, 'quick_deduction']}
                    label={index === 0 ? '速算扣除数' : undefined}
                    rules={[{ required: true }]}
                  >
                    <Input inputMode="decimal" style={{ width: 140 }} />
                  </Form.Item>
                  <Button danger onClick={() => remove(field.name)}>
                    删除
                  </Button>
                </Space>
              ))}
              <Form.ErrorList errors={errors} />
              <Button
                onClick={() => add({ upper_bound: null, rate: '0', quick_deduction: '0' })}
              >
                新增税率档位
              </Button>
            </>
          )}
        </Form.List>

        <h3>引擎生成收入归类</h3>
        <Table
          size="small"
          pagination={false}
          rowKey="code"
          dataSource={DERIVED_INCOME_CODES.map(({ value }) => ({ code: value }))}
          columns={[
            {
              title: '收入项',
              dataIndex: 'code',
              render: (code: DerivedIncomeRule['code']) =>
                DERIVED_INCOME_CODES.find((item) => item.value === code)?.label ?? code,
            },
            {
              title: '计税',
              render: (_, __, index: number) => (
                <Form.Item
                  name={['derived_income_rules', index, 'taxable']}
                  valuePropName="checked"
                  style={{ marginBottom: 0 }}
                >
                  <Checkbox />
                </Form.Item>
              ),
            },
            {
              title: '计入社保基数',
              render: (_, __, index: number) => (
                <Form.Item
                  name={['derived_income_rules', index, 'in_social_base']}
                  valuePropName="checked"
                  style={{ marginBottom: 0 }}
                >
                  <Checkbox />
                </Form.Item>
              ),
            },
            {
              title: '计入公积金基数',
              render: (_, __, index: number) => (
                <Form.Item
                  name={['derived_income_rules', index, 'in_housing_base']}
                  valuePropName="checked"
                  style={{ marginBottom: 0 }}
                >
                  <Checkbox />
                </Form.Item>
              ),
            },
          ]}
        />
        {DERIVED_INCOME_CODES.map(({ value }, index) => (
          <Form.Item key={value} name={['derived_income_rules', index, 'code']} hidden>
            <Select />
          </Form.Item>
        ))}
      </Form>
    </Modal>
  )
}

export default function PayrollPoliciesPage() {
  const { user, hasPermission } = useAuth()
  const queryClient = useQueryClient()
  const queryScope = user?.username ?? 'anonymous'
  const canRead = hasPermission(Perm.POLICY_READ)
  const canWrite = hasPermission(Perm.POLICY_WRITE)
  const [city, setCity] = useState('')
  const [editing, setEditing] = useState<PayrollPolicy | null>(null)
  const [formOpen, setFormOpen] = useState(false)

  const policiesQuery = useQuery({
    queryKey: ['payrollPolicies', queryScope, city, canWrite],
    queryFn: () => fetchPayrollPolicies({ city, includeDrafts: canWrite }),
    enabled: canRead,
  })

  const invalidatePolicies = () =>
    queryClient.invalidateQueries({ queryKey: ['payrollPolicies', queryScope] })

  const createMutation = useMutation({
    mutationFn: createPayrollPolicy,
    onSuccess: () => {
      message.success('政策草稿已创建')
      setFormOpen(false)
      void invalidatePolicies()
    },
    onError: (error) => message.error(errorMessage(error, '创建政策失败')),
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: PayrollPolicyInput }) =>
      updatePayrollPolicy(id, payload),
    onSuccess: () => {
      message.success('政策草稿已保存')
      setFormOpen(false)
      setEditing(null)
      void invalidatePolicies()
    },
    onError: (error) => message.error(errorMessage(error, '保存政策失败')),
  })
  const finalizeMutation = useMutation({
    mutationFn: finalizePayrollPolicy,
    onSuccess: () => {
      message.success('政策已定稿')
      void invalidatePolicies()
    },
    onError: (error) => message.error(errorMessage(error, '政策无法定稿')),
  })

  if (!canRead) {
    return <Result status="403" title="无权查看薪税政策" />
  }

  const saving = createMutation.isPending || updateMutation.isPending

  return (
    <div data-testid="payroll-policies-page">
      <Alert
        type="info"
        showIcon
        message="城市薪税政策按生效月份版本化"
        description="草稿可编辑；定稿后不可修改。若已开始受影响月份的核算，请先按更正流程重开所有受影响批次。"
        style={{ marginBottom: 16 }}
      />
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search
          allowClear
          placeholder="按参保城市筛选"
          style={{ width: 260 }}
          onSearch={(value) => setCity(value)}
          onChange={(event) => {
            if (!event.target.value) setCity('')
          }}
        />
        {canWrite && (
          <Button
            data-testid="policy-create-draft"
            type="primary"
            onClick={() => {
              setEditing(null)
              setFormOpen(true)
            }}
          >
            新建政策草稿
          </Button>
        )}
      </Space>
      <Table<PayrollPolicy>
        rowKey="id"
        loading={policiesQuery.isLoading}
        dataSource={policiesQuery.data ?? []}
        pagination={{ pageSize: 20 }}
        columns={[
          { title: '城市', dataIndex: 'city' },
          { title: '生效日期', dataIndex: 'effective_from' },
          {
            title: '状态',
            dataIndex: 'is_finalized',
            render: (finalized: boolean) => (
              <Tag color={finalized ? 'green' : 'gold'}>{finalized ? '已定稿' : '草稿'}</Tag>
            ),
          },
          { title: '每月基本减除费用', dataIndex: 'monthly_basic_deduction' },
          { title: '社保/公积金规则', render: (_, policy) => `${policy.social_rules.length} 项` },
          { title: '税率档位', render: (_, policy) => `${policy.tax_brackets.length} 档` },
          {
            title: '操作',
            render: (_, policy) =>
              canWrite && !policy.is_finalized ? (
                <Space>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditing(policy)
                      setFormOpen(true)
                    }}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="确认定稿该政策？定稿后不能编辑。"
                    onConfirm={() => finalizeMutation.mutate(policy.id)}
                  >
                    <Button size="small" type="primary" loading={finalizeMutation.isPending}>
                      定稿
                    </Button>
                  </Popconfirm>
                </Space>
              ) : (
                '—'
              ),
          },
        ]}
      />
      <PolicyFormModal
        open={formOpen}
        policy={editing}
        saving={saving}
        onCancel={() => {
          setFormOpen(false)
          setEditing(null)
        }}
        onSubmit={(payload) => {
          if (editing) updateMutation.mutate({ id: editing.id, payload })
          else createMutation.mutate(payload)
        }}
      />
    </div>
  )
}

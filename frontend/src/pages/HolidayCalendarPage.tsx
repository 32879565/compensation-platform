import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useMemo, useState } from 'react'

import {
  fetchHolidayCalendarPeriod,
  fetchHolidayDates,
  fetchHolidayWork,
  finalizeHolidayCalendar,
  setHolidayWork,
  unfinalizeHolidayCalendar,
  upsertHolidayDate,
  type EmploymentType,
  type HolidayDate,
  type HolidayWorkInput,
} from '../api/holidays'
import { fetchEmployees, type Employee } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import { safeHttpUrl, validateHttpUrl } from '../utils/safeExternalUrl'

const employmentLabels: Record<EmploymentType, string> = {
  FULL_TIME: '全职月薪',
  PART_TIME_HOURLY: '兼职小时工',
  LABOR: '劳务',
}

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

function errorDetail(error: unknown): string {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    typeof error.response === 'object' &&
    error.response !== null &&
    'data' in error.response
  ) {
    const data = error.response.data
    if (typeof data === 'object' && data !== null && 'detail' in data) {
      return String(data.detail)
    }
  }
  return '操作失败，请检查输入后重试'
}

async function fetchAllEmployees(): Promise<Employee[]> {
  const firstPage = await fetchEmployees({ page: 1, page_size: 500 })
  const pageSize = Math.max(firstPage.page_size, 1)
  const pageCount = Math.ceil(firstPage.total / pageSize)
  if (pageCount <= 1) return firstPage.items

  const remainingPages = await Promise.all(
    Array.from({ length: pageCount - 1 }, (_, index) =>
      fetchEmployees({ page: index + 2, page_size: pageSize }),
    ),
  )
  return [...firstPage.items, ...remainingPages.flatMap((page) => page.items)]
}

export default function HolidayCalendarPage() {
  const { user, hasPermission } = useAuth()
  const queryClient = useQueryClient()
  const [period, setPeriod] = useState(currentPeriod)
  const [employeeId, setEmployeeId] = useState<number | null>(null)
  const [holidayOpen, setHolidayOpen] = useState(false)
  const [workTarget, setWorkTarget] = useState<HolidayDate | null>(null)
  const [holidayForm] = Form.useForm<HolidayDate>()
  const [workForm] = Form.useForm<HolidayWorkInput>()

  const canWriteCalendar = hasPermission(Perm.HOLIDAY_CALENDAR_WRITE)
  const canReadAttendance = hasPermission(Perm.ATTENDANCE_READ)
  const canWriteAttendance = hasPermission(Perm.ATTENDANCE_WRITE)
  const canReadEmployees = hasPermission(Perm.EMPLOYEE_READ)

  const datesQuery = useQuery({
    queryKey: ['holiday-calendar', 'dates', user?.username, period],
    queryFn: () => fetchHolidayDates(period),
  })
  const calendarQuery = useQuery({
    queryKey: ['holiday-calendar', 'period', user?.username, period],
    queryFn: () => fetchHolidayCalendarPeriod(period),
  })
  const employeesQuery = useQuery({
    queryKey: ['holiday-calendar', 'employees', user?.username],
    queryFn: fetchAllEmployees,
    enabled: canReadAttendance && canReadEmployees,
  })
  const workQuery = useQuery({
    queryKey: ['holiday-calendar', 'work', user?.username, period, employeeId],
    queryFn: () => fetchHolidayWork(employeeId as number, period),
    enabled: canReadAttendance && employeeId !== null,
  })
  const calendarMutationBlocked =
    datesQuery.isLoading || calendarQuery.isLoading || datesQuery.isError || calendarQuery.isError

  const refreshCalendar = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['holiday-calendar', 'dates'] }),
      queryClient.invalidateQueries({ queryKey: ['holiday-calendar', 'period'] }),
    ])
  }

  const holidayMutation = useMutation({
    mutationFn: (input: HolidayDate) => {
      if (calendarMutationBlocked) throw new Error('法定日历来源尚未完整读取')
      return upsertHolidayDate(input)
    },
    onSuccess: async () => {
      setHolidayOpen(false)
      holidayForm.resetFields()
      await refreshCalendar()
      message.success('法定日已保存')
    },
    onError: (error) => message.error(errorDetail(error)),
  })
  const finalizeMutation = useMutation({
    mutationFn: (action: 'finalize' | 'unfinalize') => {
      if (calendarMutationBlocked) throw new Error('法定日历来源尚未完整读取')
      return action === 'finalize'
        ? finalizeHolidayCalendar(period)
        : unfinalizeHolidayCalendar(period)
    },
    onSuccess: async (_, action) => {
      await refreshCalendar()
      message.success(action === 'finalize' ? '本月法定日历已确认' : '已撤销本月确认')
    },
    onError: (error) => message.error(errorDetail(error)),
  })
  const workMutation = useMutation({
    mutationFn: (input: HolidayWorkInput) => {
      if (employeeId === null || workTarget === null) throw new Error('缺少员工或法定日')
      return setHolidayWork(employeeId, workTarget.holiday_date, input)
    },
    onSuccess: async () => {
      setWorkTarget(null)
      workForm.resetFields()
      await queryClient.invalidateQueries({ queryKey: ['holiday-calendar', 'work'] })
      message.success('法定日出勤来源已保存')
    },
    onError: (error) => message.error(errorDetail(error)),
  })

  const workByDate = useMemo(
    () => new Map((workQuery.data ?? []).map((record) => [record.holiday_date, record])),
    [workQuery.data],
  )
  const calendar = calendarQuery.data
  const isFinalized = calendar?.is_finalized

  const columns = [
    { title: '日期', dataIndex: 'holiday_date', width: 130 },
    { title: '法定日', dataIndex: 'name', width: 180 },
    {
      title: '适用用工类型',
      dataIndex: 'eligible_employment_types',
      render: (values: EmploymentType[]) => (
        <Space size={[4, 4]} wrap>
          {values.map((value) => (
            <Tag key={value}>{employmentLabels[value]}</Tag>
          ))}
        </Space>
      ),
    },
    ...(employeeId !== null
      ? [
          {
            title: '该员工出勤',
            key: 'worked',
            width: 140,
            render: (_: unknown, holiday: HolidayDate) => {
              const record = workByDate.get(holiday.holiday_date)
              return record ? (
                <Tag color={record.worked ? 'green' : 'default'}>
                  {record.worked ? '已出勤' : '未出勤'}
                </Tag>
              ) : (
                <Tag>未登记（按未出勤）</Tag>
              )
            },
          },
          {
            title: '来源与依据',
            key: 'evidence',
            render: (_: unknown, holiday: HolidayDate) => {
              const record = workByDate.get(holiday.holiday_date)
              const evidenceUrl = safeHttpUrl(record?.evidence_url)
              return (
                <Space direction="vertical" size={2}>
                  <Typography.Text type="secondary">{record?.reason ?? '—'}</Typography.Text>
                  {evidenceUrl ? (
                    <Typography.Link href={evidenceUrl} target="_blank" rel="noreferrer">
                      查看依据
                    </Typography.Link>
                  ) : record?.evidence_url ? (
                    <Typography.Text type="danger">无效依据地址</Typography.Text>
                  ) : null}
                </Space>
              )
            },
          },
          ...(canWriteAttendance
            ? [
                {
                  title: '操作',
                  key: 'action',
                  width: 110,
                  render: (_: unknown, holiday: HolidayDate) => (
                    <Button
                      size="small"
                      onClick={() => {
                        const existing = workByDate.get(holiday.holiday_date)
                        workForm.setFieldsValue({
                          worked: existing?.worked ?? false,
                          reason: existing?.reason ?? undefined,
                          evidence_url: existing?.evidence_url ?? undefined,
                          correction_reason: undefined,
                        })
                        setWorkTarget(holiday)
                      }}
                      disabled={workQuery.isError || workQuery.isFetching}
                    >
                      登记出勤
                    </Button>
                  ),
                },
              ]
            : []),
        ]
      : []),
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card style={{ borderTop: '3px solid #1677ff' }} styles={{ body: { paddingBottom: 18 } }}>
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <div>
            <Typography.Text type="secondary">计薪来源台账 · 逐日留痕</Typography.Text>
            <Typography.Title level={3} style={{ margin: '4px 0 0' }}>
              法定节假日台账
            </Typography.Title>
          </div>
          <Tag
            color={isFinalized === true ? 'green' : isFinalized === false ? 'gold' : 'default'}
            style={{ padding: '5px 12px' }}
          >
            {isFinalized === true ? '已确认' : isFinalized === false ? '未确认' : '状态未知'}
          </Tag>
        </Space>
        <Descriptions size="small" column={{ xs: 1, sm: 3 }} style={{ marginTop: 18 }}>
          <Descriptions.Item label="计薪月份">
            <Input
              aria-label="计薪月份"
              type="month"
              value={period}
              onChange={(event) => {
                setPeriod(event.target.value)
                setEmployeeId(null)
              }}
              style={{ width: 150 }}
            />
          </Descriptions.Item>
          <Descriptions.Item label="确认人">{calendar?.finalized_by ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="确认时间">
            {calendar?.finalized_at ? new Date(calendar.finalized_at).toLocaleString() : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Alert
        type="info"
        showIcon
        message="法定工资 = 3000 ÷ 当月应出勤天数 ×（出勤 3 倍 / 未出勤 1 倍）"
        description="系统按员工劳动关系起止日期和适用用工类型逐日判断；入职晚于某法定日时，该日不计入。"
      />

      {datesQuery.isError ? (
        <Alert type="error" showIcon message="无法读取法定日列表，已停用日历维护操作" />
      ) : null}
      {calendarQuery.isError ? (
        <Alert type="error" showIcon message="无法读取法定日历确认状态，已停用日历维护操作" />
      ) : null}

      <Card
        title="本月法定日"
        extra={
          canWriteCalendar ? (
            <Space>
              <Button
                disabled={calendarMutationBlocked || isFinalized === true}
                onClick={() => {
                  holidayForm.resetFields()
                  setHolidayOpen(true)
                }}
              >
                新增法定日
              </Button>
              <Popconfirm
                title={
                  isFinalized === true
                    ? '撤销确认后批次将暂时不能核算，继续？'
                    : '确认后日历将冻结，继续？'
                }
                onConfirm={() =>
                  finalizeMutation.mutate(isFinalized === true ? 'unfinalize' : 'finalize')
                }
              >
                <Button
                  type={isFinalized === true ? 'default' : 'primary'}
                  disabled={calendarMutationBlocked}
                >
                  {isFinalized === true ? '撤销本月确认' : '确认本月日历'}
                </Button>
              </Popconfirm>
            </Space>
          ) : null
        }
      >
        {canReadAttendance && canReadEmployees ? (
          <Select
            aria-label="选择员工"
            showSearch
            allowClear
            placeholder="选择员工后登记逐日出勤"
            optionFilterProp="label"
            value={employeeId}
            onChange={(value) => setEmployeeId(value ?? null)}
            options={(employeesQuery.data ?? []).map((employee) => ({
              value: employee.id,
              label: `${employee.emp_no} · ${employee.name}`,
            }))}
            style={{ width: 'min(100%, 360px)', marginBottom: 16 }}
          />
        ) : null}
        {employeeId !== null && workQuery.isError ? (
          <Alert
            type="error"
            showIcon
            message="无法读取该员工的法定日出勤记录，已停用登记操作"
            style={{ marginBottom: 16 }}
          />
        ) : null}
        <Table<HolidayDate>
          rowKey="holiday_date"
          loading={datesQuery.isLoading || calendarQuery.isLoading || workQuery.isLoading}
          dataSource={datesQuery.data ?? []}
          columns={columns}
          pagination={false}
          scroll={{ x: 760 }}
          locale={{ emptyText: '本月尚未登记法定节假日' }}
        />
      </Card>

      <Modal
        title="新增或更新法定日"
        open={holidayOpen}
        onCancel={() => setHolidayOpen(false)}
        onOk={() => holidayForm.submit()}
        confirmLoading={holidayMutation.isPending}
        okButtonProps={{ disabled: calendarMutationBlocked }}
        destroyOnHidden
      >
        <Form
          form={holidayForm}
          layout="vertical"
          onFinish={(values) => holidayMutation.mutate(values)}
        >
          <Form.Item
            name="holiday_date"
            label="日期"
            rules={[
              { required: true },
              {
                validator: (_, value: string) =>
                  !value || value.startsWith(`${period}-`)
                    ? Promise.resolve()
                    : Promise.reject(new Error('日期必须属于当前计薪月份')),
              },
            ]}
          >
            <Input type="date" />
          </Form.Item>
          <Form.Item name="name" label="法定日名称" rules={[{ required: true, max: 64 }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="eligible_employment_types"
            label="适用用工类型"
            rules={[{ required: true }]}
          >
            <Select
              mode="multiple"
              options={Object.entries(employmentLabels).map(([value, label]) => ({ value, label }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`登记法定日出勤 · ${workTarget?.name ?? ''}`}
        open={workTarget !== null}
        onCancel={() => setWorkTarget(null)}
        onOk={() => workForm.submit()}
        confirmLoading={workMutation.isPending}
        okButtonProps={{ disabled: workQuery.isError }}
        destroyOnHidden
      >
        <Form form={workForm} layout="vertical" onFinish={(values) => workMutation.mutate(values)}>
          <Form.Item name="worked" label="出勤状态" rules={[{ required: true }]}>
            <Select
              options={[
                { value: true, label: '已出勤（3 倍）' },
                { value: false, label: '未出勤（1 倍）' },
              ]}
            />
          </Form.Item>
          <Form.Item name="reason" label="排班或确认说明" rules={[{ max: 1000 }]}>
            <Input.TextArea rows={3} maxLength={1000} />
          </Form.Item>
          <Form.Item
            name="evidence_url"
            label="证明附件地址"
            rules={[{ max: 512 }, { validator: validateHttpUrl }]}
          >
            <Input placeholder="https://…" />
          </Form.Item>
          <Form.Item
            name="correction_reason"
            label="更正原因（覆盖已有记录时必填）"
            rules={[
              {
                validator: (_, value: string | undefined) =>
                  workTarget && workByDate.has(workTarget.holiday_date) && !value?.trim()
                    ? Promise.reject(new Error('覆盖已有记录必须填写更正原因'))
                    : Promise.resolve(),
              },
              { max: 1000 },
            ]}
          >
            <Input.TextArea rows={2} maxLength={1000} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}

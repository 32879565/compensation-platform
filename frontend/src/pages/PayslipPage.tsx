import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Descriptions, Empty, Select, Space, Spin, Table, Typography } from 'antd'
import { useEffect, useMemo, useState } from 'react'

import {
  fetchMyPayslip,
  fetchMyPayslipPeriods,
  type PayslipLine,
} from '../api/payslips'
import { useAuth } from '../auth/AuthContext'

function requestErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '暂时无法读取工资单，请稍后重试。'
}

export default function PayslipPage() {
  const { user } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const [period, setPeriod] = useState<string | null>(null)

  const periodsQuery = useQuery({
    queryKey: ['payslipPeriods', queryScope],
    queryFn: fetchMyPayslipPeriods,
  })
  const periods = useMemo(() => periodsQuery.data ?? [], [periodsQuery.data])

  useEffect(() => {
    setPeriod(null)
  }, [queryScope])

  useEffect(() => {
    if (!period || !periods.some((item) => item.period === period)) {
      setPeriod(periods[0]?.period ?? null)
    }
  }, [period, periods])

  const payslipQuery = useQuery({
    queryKey: ['payslip', queryScope, period],
    queryFn: () => fetchMyPayslip(period!),
    enabled: period !== null,
  })
  const payslip = payslipQuery.data

  if (periodsQuery.isLoading) return <Spin />

  if (periodsQuery.isError) {
    return <Alert type="error" showIcon message={requestErrorMessage(periodsQuery.error)} />
  }

  if (!periods.length) {
    return (
      <Empty
        description="当前账号没有可查看的已锁定工资单；如应有工资单，请联系薪酬管理员确认账号绑定。"
      />
    )
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Space wrap>
        <Typography.Title level={3} style={{ margin: 0 }}>
          我的工资单
        </Typography.Title>
        <Select
          aria-label="工资周期"
          value={period}
          style={{ minWidth: 140 }}
          options={periods.map((item) => ({ value: item.period, label: item.period }))}
          onChange={setPeriod}
        />
      </Space>

      {payslipQuery.isLoading && <Spin />}
      {payslipQuery.isError && (
        <Alert type="error" showIcon message={requestErrorMessage(payslipQuery.error)} />
      )}
      {payslip && (
        <>
          {payslip.warnings.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="工资单提示"
              description={payslip.warnings.join('；')}
            />
          )}
          <Card title={`${payslip.period} 工资汇总`}>
            <Descriptions column={{ xs: 1, sm: 2, md: 3 }}>
              <Descriptions.Item label="实际出勤天数">
                {payslip.actual_attendance_days}
              </Descriptions.Item>
              <Descriptions.Item label="应发工资">{payslip.gross}</Descriptions.Item>
              <Descriptions.Item label="本月扣押金">{payslip.deposit}</Descriptions.Item>
              <Descriptions.Item label="实发工资">{payslip.net}</Descriptions.Item>
              <Descriptions.Item label="结转金额">{payslip.carry_forward}</Descriptions.Item>
              <Descriptions.Item label="规则版本">{payslip.rule_version}</Descriptions.Item>
            </Descriptions>
          </Card>
          <Card title="工资明细">
            <Table<PayslipLine>
              rowKey={(line) => `${line.code}-${line.formula}`}
              dataSource={payslip.lines}
              pagination={false}
              columns={[
                { title: '项目', dataIndex: 'code' },
                { title: '类别', dataIndex: 'category' },
                { title: '计算说明', dataIndex: 'formula' },
                { title: '金额', dataIndex: 'amount' },
              ]}
            />
          </Card>
        </>
      )}
    </Space>
  )
}

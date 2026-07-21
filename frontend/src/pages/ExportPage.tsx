import { useMutation } from '@tanstack/react-query'
import { Alert, Button, Card, Input, Space, Typography, message } from 'antd'
import { useState } from 'react'

import {
  bankPaymentExportFilename,
  exportBankPayment,
  exportErrorMessage,
  exportIndividualIncomeTax,
  exportPayroll,
  exportSocialInsurance,
  individualIncomeTaxExportFilename,
  payrollExportFilename,
  socialInsuranceExportFilename,
} from '../api/exports'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

type RegulatedExportKind = 'social-insurance' | 'individual-income-tax' | 'bank-payment'

interface RegulatedExportRequest {
  kind: RegulatedExportKind
  period: string
}

interface RegulatedExportDefinition {
  label: string
  request: (period: string) => Promise<Blob>
  filename: (period: string) => string
}

const regulatedExports: Record<RegulatedExportKind, RegulatedExportDefinition> = {
  'social-insurance': {
    label: '社保对账',
    request: exportSocialInsurance,
    filename: socialInsuranceExportFilename,
  },
  'individual-income-tax': {
    label: '个税对账',
    request: exportIndividualIncomeTax,
    filename: individualIncomeTaxExportFilename,
  },
  'bank-payment': {
    label: '银行付款对账',
    request: exportBankPayment,
    filename: bankPaymentExportFilename,
  },
}

function currentMonth(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 7)
}

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export default function ExportPage() {
  const { hasPermission } = useAuth()
  const [period, setPeriod] = useState(currentMonth)
  const [regulatedError, setRegulatedError] = useState<string | null>(null)
  const canExportData = hasPermission(Perm.EXPORT_DATA)
  const canExportRegulated = canExportData && hasPermission(Perm.EMPLOYEE_PII)

  const exportMutation = useMutation({
    mutationFn: exportPayroll,
    onSuccess: (workbook, exportedPeriod) => {
      download(workbook, payrollExportFilename(exportedPeriod))
      message.success('工资核算表已开始下载')
    },
    onError: async (error) => {
      message.error(await exportErrorMessage(error))
    },
  })
  const regulatedMutation = useMutation({
    mutationFn: ({ kind, period: exportPeriod }: RegulatedExportRequest) =>
      regulatedExports[kind].request(exportPeriod),
    onSuccess: (workbook, { kind, period: exportPeriod }) => {
      const definition = regulatedExports[kind]
      download(workbook, definition.filename(exportPeriod))
      message.success(definition.label + '文件已开始下载')
    },
    onError: async (error, { kind }) => {
      const definition = regulatedExports[kind]
      setRegulatedError(definition.label + '导出失败：' + (await exportErrorMessage(error)))
    },
  })
  const isDownloading = exportMutation.isPending || regulatedMutation.isPending

  if (!canExportData) {
    return (
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          数据导出
        </Typography.Title>
        <Alert
          type="warning"
          showIcon
          message="数据导出需要数据导出权限。"
          description="为保护薪酬和员工敏感信息，当前账号不可导出任何文件。"
        />
      </Space>
    )
  }

  function downloadRegulated(kind: RegulatedExportKind): void {
    setRegulatedError(null)
    regulatedMutation.mutate({ kind, period })
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        数据导出
      </Typography.Title>
      <Card title="已锁定工资核算表" style={{ maxWidth: 680 }}>
        <Space wrap>
          <label>
            计薪周期
            <Input
              aria-label="导出计薪周期"
              type="month"
              value={period}
              disabled={isDownloading}
              onChange={(event) => setPeriod(event.target.value || currentMonth())}
              style={{ width: 150, marginLeft: 8 }}
            />
          </label>
          <Button
            type="primary"
            loading={exportMutation.isPending}
            disabled={regulatedMutation.isPending}
            onClick={() => exportMutation.mutate(period)}
          >
            导出 XLSX
          </Button>
        </Space>
        <Alert
          type="info"
          showIcon
          message="仅导出已锁定、当前复核轮次的最新工资结果。"
          description="导出文件按当前账号的组织范围过滤，并对可能被 Excel 当作公式的文本自动转义。"
          style={{ marginTop: 16 }}
        />
      </Card>
      <Card title="社保、个税与银行付款对账文件" style={{ maxWidth: 680 }}>
        {canExportRegulated ? (
          <>
            <Space wrap>
              <Button
                aria-label="下载社保对账 XLSX"
                loading={
                  regulatedMutation.isPending &&
                  regulatedMutation.variables?.kind === 'social-insurance'
                }
                disabled={isDownloading}
                onClick={() => downloadRegulated('social-insurance')}
              >
                下载社保对账 XLSX
              </Button>
              <Button
                aria-label="下载个税对账 XLSX"
                loading={
                  regulatedMutation.isPending &&
                  regulatedMutation.variables?.kind === 'individual-income-tax'
                }
                disabled={isDownloading}
                onClick={() => downloadRegulated('individual-income-tax')}
              >
                下载个税对账 XLSX
              </Button>
              <Button
                aria-label="下载银行付款对账 XLSX"
                loading={
                  regulatedMutation.isPending &&
                  regulatedMutation.variables?.kind === 'bank-payment'
                }
                disabled={isDownloading}
                onClick={() => downloadRegulated('bank-payment')}
              >
                下载银行付款对账 XLSX
              </Button>
            </Space>
            <Alert
              type="warning"
              showIcon
              message="仅生成通用对账文件，不是官方申报或银行导入格式。"
              description="社保和个税数据须按所在地规则核验；银行付款文件须按开户行模板和本地字段配置确认后使用。"
              style={{ marginTop: 16 }}
            />
            {regulatedError ? (
              <Alert
                type="error"
                showIcon
                message={regulatedError}
                closable
                onClose={() => setRegulatedError(null)}
                style={{ marginTop: 16 }}
              />
            ) : null}
          </>
        ) : (
          <Alert
            type="warning"
            showIcon
            message="社保、个税和银行付款对账文件需要数据导出及员工敏感信息权限。"
            description="为保护员工敏感信息，当前账号不可生成此类文件。"
          />
        )}
      </Card>
    </Space>
  )
}

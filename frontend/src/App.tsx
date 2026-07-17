import { useQuery } from '@tanstack/react-query'
import { Card, Typography } from 'antd'
import axios from 'axios'

import { HealthTag } from './components/HealthTag'

interface HealthResponse {
  status: string
}

export default function App() {
  const { data, isError } = useQuery({
    queryKey: ['health'],
    queryFn: async () => (await axios.get<HealthResponse>('/api/health')).data,
    retry: 1,
  })

  const ok = isError ? false : data ? data.status === 'ok' : undefined

  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }}>
      <Card style={{ width: 420, textAlign: 'center' }}>
        <Typography.Title level={3}>薪酬一体化平台</Typography.Title>
        <Typography.Paragraph type="secondary">
          S1 脚手架 — 核算 · 管理 · 查询
        </Typography.Paragraph>
        <HealthTag ok={ok} />
      </Card>
    </div>
  )
}

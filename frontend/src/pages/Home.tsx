import { Card, Tag, Typography } from 'antd'

import { useAuth } from '../auth/AuthContext'

export default function Home() {
  const { user } = useAuth()
  return (
    <Card>
      <Typography.Title level={4}>欢迎，{user?.username}</Typography.Title>
      <Typography.Paragraph type="secondary">
        S3 认证与 RBAC 已就位。以下是你当前拥有的权限：
      </Typography.Paragraph>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {user?.permissions.map((p) => (
          <Tag key={p} color="blue">
            {p}
          </Tag>
        ))}
      </div>
    </Card>
  )
}

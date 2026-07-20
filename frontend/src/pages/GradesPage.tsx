import { useQuery } from '@tanstack/react-query'
import { Card, Table } from 'antd'

import { fetchGrades, type JobGrade } from '../api/masterdata'

export default function GradesPage() {
  const { data, isLoading } = useQuery({ queryKey: ['grades'], queryFn: fetchGrades })

  const columns = [
    { title: '编码', dataIndex: 'code' },
    { title: '名称', dataIndex: 'name' },
    { title: '级别', dataIndex: 'rank' },
  ]

  return (
    <Card title="职级体系">
      <Table<JobGrade>
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={false}
      />
    </Card>
  )
}

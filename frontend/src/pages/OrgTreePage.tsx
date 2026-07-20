import { useQuery } from '@tanstack/react-query'
import { Card, Spin, Tree } from 'antd'
import type { DataNode } from 'antd/es/tree'

import { fetchOrgTree, type OrgTreeNode } from '../api/masterdata'

const TYPE_LABEL: Record<OrgTreeNode['type'], string> = {
  GROUP: '集团',
  REGION: '区域',
  STORE: '门店',
}

function toTreeData(nodes: OrgTreeNode[]): DataNode[] {
  return nodes.map((n) => ({
    key: n.id,
    title: `${n.name}（${TYPE_LABEL[n.type]}${n.city ? ' · ' + n.city : ''}）`,
    children: n.children.length ? toTreeData(n.children) : undefined,
  }))
}

export default function OrgTreePage() {
  const { data, isLoading } = useQuery({ queryKey: ['orgTree'], queryFn: fetchOrgTree })

  if (isLoading) return <Spin />

  return (
    <Card title="组织架构">
      <Tree treeData={toTreeData(data ?? [])} defaultExpandAll selectable={false} />
    </Card>
  )
}

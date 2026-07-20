import { Empty } from 'antd'

export default function Placeholder({ title }: { title: string }) {
  return <Empty description={`${title} · 模块建设中`} style={{ marginTop: 80 }} />
}

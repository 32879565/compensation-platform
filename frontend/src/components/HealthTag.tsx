import { Tag } from 'antd'

export function HealthTag({ ok }: { ok: boolean | undefined }) {
  if (ok === undefined) {
    return <Tag>后端检测中…</Tag>
  }
  return ok ? <Tag color="green">后端正常</Tag> : <Tag color="red">后端异常</Tag>
}

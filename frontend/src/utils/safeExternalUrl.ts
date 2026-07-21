export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null
  const candidate = value.trim()
  if (!candidate) return null

  try {
    const parsed = new URL(candidate)
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? candidate : null
  } catch {
    return null
  }
}

export function validateHttpUrl(_: unknown, value: string | null | undefined): Promise<void> {
  if (!value?.trim() || safeHttpUrl(value)) return Promise.resolve()
  return Promise.reject(new Error('请输入不含账号信息的 https:// 地址'))
}

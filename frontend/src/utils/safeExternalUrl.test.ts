import { describe, expect, it } from 'vitest'

import { safeHttpUrl, validateHttpUrl } from './safeExternalUrl'

describe('safe external attachment URLs', () => {
  it('allows only credential-free absolute HTTPS links in rendered anchors', () => {
    expect(safeHttpUrl('https://files.example.test/proof.pdf')).toBe(
      'https://files.example.test/proof.pdf',
    )
    expect(safeHttpUrl('http://files.example.test/proof.pdf')).toBeNull()
    expect(safeHttpUrl('https://trusted.example@evil.example/proof.pdf')).toBeNull()
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull()
    expect(safeHttpUrl('data:text/html,unsafe')).toBeNull()
    expect(safeHttpUrl('/relative-proof.pdf')).toBeNull()
  })

  it('rejects non-http form values while allowing empty optional values', async () => {
    await expect(validateHttpUrl(undefined, '')).resolves.toBeUndefined()
    await expect(
      validateHttpUrl(undefined, 'https://files.example.test/proof.pdf'),
    ).resolves.toBeUndefined()
    await expect(validateHttpUrl(undefined, 'javascript:alert(1)')).rejects.toThrow(
      '请输入不含账号信息的 https:// 地址',
    )
  })
})

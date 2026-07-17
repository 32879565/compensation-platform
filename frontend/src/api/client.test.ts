import { describe, expect, it } from 'vitest'

import { getAccessToken, setAccessToken } from './client'

describe('access token store', () => {
  it('设置与读取 token', () => {
    setAccessToken('abc')
    expect(getAccessToken()).toBe('abc')
  })

  it('清空 token', () => {
    setAccessToken('abc')
    setAccessToken(null)
    expect(getAccessToken()).toBeNull()
  })
})

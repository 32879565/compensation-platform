import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

// 隔离网络：AuthProvider 挂载会调用 refresh；mock 掉 api 客户端。
vi.mock('../api/client', () => ({
  api: { post: vi.fn().mockRejectedValue(new Error('no session')) },
  setAccessToken: vi.fn(),
  getAccessToken: vi.fn(),
  subscribeAuthSessionExpired: vi.fn(() => () => undefined),
}))

import { AuthProvider } from '../auth/AuthContext'
import Login from './Login'

describe('Login 页', () => {
  it('渲染标题与登录按钮', async () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AuthProvider>
          <Login />
        </AuthProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('薪酬一体化平台')).toBeTruthy()
    // AntD 会在两个中文字之间自动插入空格，用正则容忍
    expect(screen.getByRole('button', { name: /登\s*录/ })).toBeTruthy()
    expect(await screen.findByLabelText('用户名')).toBeTruthy()
  })
})

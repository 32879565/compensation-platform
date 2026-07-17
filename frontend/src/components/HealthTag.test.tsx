import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { HealthTag } from './HealthTag'

describe('HealthTag', () => {
  it('未知状态显示检测中', () => {
    render(<HealthTag ok={undefined} />)
    expect(screen.getByText('后端检测中…')).toBeTruthy()
  })

  it('正常状态显示后端正常', () => {
    render(<HealthTag ok={true} />)
    expect(screen.getByText('后端正常')).toBeTruthy()
  })

  it('异常状态显示后端异常', () => {
    render(<HealthTag ok={false} />)
    expect(screen.getByText('后端异常')).toBeTruthy()
  })
})

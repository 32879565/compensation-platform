import { describe, expect, it } from 'vitest'

import playwrightConfig from '../playwright.config'

describe('Playwright credential artifact safety', () => {
  it('discards every per-test output directory, including failure DOM context', () => {
    expect(playwrightConfig.preserveOutput).toBe('never')
  })
})

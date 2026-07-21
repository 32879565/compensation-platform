import { describe, expect, it } from 'vitest'

import {
  assertE2EPageOrigin,
  assertE2ETargetMarker,
  assertE2EWriteTargetAllowed,
  getE2ECredentials,
  getE2EReviewerCredentials,
  getE2ETargetOrigin,
  getE2ETargetMarker,
  isE2ERequestURLAllowed,
} from './e2eTargetSafety'

describe('assertE2EWriteTargetAllowed', () => {
  it.each(['http://127.0.0.1:8080', 'https://localhost', 'http://[::1]:18080'])(
    'rejects a loopback target without the explicit write opt-in: %s',
    (baseURL) => {
      expect(() => assertE2EWriteTargetAllowed(baseURL)).toThrow('E2E_ALLOW_WRITES=true')
    },
  )

  it('permits an HTTP(S) target only when the caller explicitly opts into writes', () => {
    expect(() => assertE2EWriteTargetAllowed('https://compensation.example.test', true)).not.toThrow()
    expect(() => assertE2EWriteTargetAllowed('http://127.0.0.1:8080', true)).not.toThrow()
  })

  it('rejects malformed and non-HTTP(S) targets even with write opt-in', () => {
    expect(() => assertE2EWriteTargetAllowed('not-a-url', true)).toThrow('not a valid URL')
    expect(() => assertE2EWriteTargetAllowed('file:///tmp/e2e', true)).toThrow('must use HTTP(S)')
  })
})

describe('assertE2ETargetMarker', () => {
  const expectedMarker = 'compensation-e2e-disposable-target'

  it('permits a target only when its health marker exactly matches', async () => {
    let requestedURL = ''
    await expect(
      assertE2ETargetMarker('http://127.0.0.1:8080', expectedMarker, async (input) => {
        requestedURL = String(input)
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: 'ok', e2e_target_marker: expectedMarker }),
        }
      }),
    ).resolves.toBeUndefined()

    expect(requestedURL).toBe('http://127.0.0.1:8080/api/health')
  })

  it.each([
    [{ status: 'ok' }, 'missing'],
    [{ status: 'ok', e2e_target_marker: 'another-stack' }, 'did not match'],
    [{ status: 'ok', e2e_target_marker: 42 }, 'missing'],
  ])('rejects a health response with a %s marker', async (payload, errorMessage) => {
    await expect(
      assertE2ETargetMarker('https://compensation.example.test', expectedMarker, async () => ({
        ok: true,
        status: 200,
        json: async () => payload,
      })),
    ).rejects.toThrow(errorMessage)
  })

  it('rejects an unhealthy target before any write scenario can run', async () => {
    await expect(
      assertE2ETargetMarker('https://compensation.example.test', expectedMarker, async () => ({
        ok: false,
        status: 503,
        json: async () => ({ status: 'unavailable' }),
      })),
    ).rejects.toThrow('health check returned HTTP 503')
  })

  it('fails fast when the expected marker was not supplied', () => {
    expect(() => getE2ETargetMarker({})).toThrow('E2E_TARGET_MARKER must be set')
  })
})

describe('getE2ECredentials', () => {
  it('fails fast when the E2E username is absent', () => {
    expect(() => getE2ECredentials({ E2E_PASSWORD: 'test-password' })).toThrow(
      'E2E_USERNAME must be set',
    )
  })

  it('fails fast when the E2E password is absent', () => {
    expect(() => getE2ECredentials({ E2E_USERNAME: 'e2e-admin' })).toThrow(
      'E2E_PASSWORD must be set',
    )
  })

  it('returns explicitly supplied credentials without a fallback value', () => {
    expect(
      getE2ECredentials({ E2E_USERNAME: 'e2e-admin', E2E_PASSWORD: 'provided-password' }),
    ).toEqual({ username: 'e2e-admin', password: 'provided-password' })
  })
})

describe('getE2EReviewerCredentials', () => {
  it('fails fast when the E2E reviewer username is absent', () => {
    expect(() =>
      getE2EReviewerCredentials({ E2E_REVIEWER_PASSWORD: 'reviewer-password' }),
    ).toThrow('E2E_REVIEWER_USERNAME must be set')
  })

  it('fails fast when the E2E reviewer password is absent', () => {
    expect(() =>
      getE2EReviewerCredentials({ E2E_REVIEWER_USERNAME: 'e2e-reviewer' }),
    ).toThrow('E2E_REVIEWER_PASSWORD must be set')
  })

  it('returns only the explicitly supplied reviewer credentials', () => {
    expect(
      getE2EReviewerCredentials({
        E2E_REVIEWER_USERNAME: 'e2e-reviewer',
        E2E_REVIEWER_PASSWORD: 'provided-reviewer-password',
      }),
    ).toEqual({ username: 'e2e-reviewer', password: 'provided-reviewer-password' })
  })
})

describe('verified E2E target origin', () => {
  const verifiedOrigin = 'http://127.0.0.1:18080'

  it('normalizes the verified origin from the configured base URL', () => {
    expect(getE2ETargetOrigin(`${verifiedOrigin}/nested/path`)).toBe(verifiedOrigin)
  })

  it('permits only HTTP(S) requests to the verified origin', () => {
    expect(isE2ERequestURLAllowed(`${verifiedOrigin}/api/health`, verifiedOrigin)).toBe(true)
    expect(isE2ERequestURLAllowed('https://127.0.0.1:18080/api/health', verifiedOrigin)).toBe(
      false,
    )
    expect(isE2ERequestURLAllowed('https://production.example.test/login', verifiedOrigin)).toBe(
      false,
    )
    expect(isE2ERequestURLAllowed('not-a-request-url', verifiedOrigin)).toBe(false)
  })

  it('permits the initial blank page and same-origin blob downloads only', () => {
    expect(isE2ERequestURLAllowed('about:blank', verifiedOrigin)).toBe(true)
    expect(isE2ERequestURLAllowed(`blob:${verifiedOrigin}/e2e-download`, verifiedOrigin)).toBe(true)
    expect(isE2ERequestURLAllowed('data:text/plain,e2e', verifiedOrigin)).toBe(false)
    expect(isE2ERequestURLAllowed('blob:https://production.example.test/e2e', verifiedOrigin)).toBe(
      false,
    )
    expect(isE2ERequestURLAllowed('file:///tmp/e2e', verifiedOrigin)).toBe(false)
    expect(isE2ERequestURLAllowed('custom-e2e://target', verifiedOrigin)).toBe(false)
    expect(isE2ERequestURLAllowed('ws://127.0.0.1:18080/e2e', verifiedOrigin)).toBe(false)
    expect(isE2ERequestURLAllowed('ws://production.example.test/e2e', verifiedOrigin)).toBe(false)
    expect(isE2ERequestURLAllowed('wss://production.example.test/e2e', verifiedOrigin)).toBe(false)
  })

  it('fails before credentials or writes when the page leaves the verified origin', () => {
    expect(() => assertE2EPageOrigin(`${verifiedOrigin}/login`, verifiedOrigin)).not.toThrow()
    expect(() =>
      assertE2EPageOrigin('https://production.example.test/login', verifiedOrigin),
    ).toThrow('verified E2E origin')
    expect(() => assertE2EPageOrigin('about:blank', verifiedOrigin)).toThrow('verified E2E origin')
  })
})

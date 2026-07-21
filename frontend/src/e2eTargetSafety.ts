export interface E2ECredentials {
  username: string
  password: string
}

export type E2EEnvironment = Record<string, string | undefined>

export type HealthFetcher = (
  input: string | URL,
  init?: RequestInit,
) => Promise<Pick<Response, 'ok' | 'status' | 'json'>>

const E2E_HEALTH_PATH = '/api/health'

function parseE2ETarget(baseURL: string): URL {
  let target: URL
  try {
    target = new URL(baseURL)
  } catch {
    throw new Error(`E2E target is not a valid URL: ${baseURL}`)
  }

  if (target.protocol !== 'http:' && target.protocol !== 'https:') {
    throw new Error(`E2E target must use HTTP(S): ${baseURL}`)
  }

  return target
}

/** Return the exact HTTP(S) origin validated by E2E global setup. */
export function getE2ETargetOrigin(baseURL: string): string {
  return parseE2ETarget(baseURL).origin
}

/**
 * Route guards use this for every intercepted request.  The untouched
 * browser-start page and a same-origin blob download are the only non-HTTP(S)
 * URLs allowed.  In particular, data/file URLs, cross-origin blobs, and
 * WebSockets are not a safe escape hatch around the verified-origin contract.
 */
export function isE2ERequestURLAllowed(requestURL: string, verifiedOrigin: string): boolean {
  let target: URL
  try {
    target = new URL(requestURL)
  } catch {
    return false
  }

  if (target.href === 'about:blank') return true
  if (target.protocol === 'blob:') {
    return target.origin === getE2ETargetOrigin(verifiedOrigin)
  }
  if (target.protocol !== 'http:' && target.protocol !== 'https:') return false
  return target.origin === getE2ETargetOrigin(verifiedOrigin)
}

/**
 * Assert the browser is still on the globally verified stack immediately
 * before sensitive interaction. This deliberately rejects about:blank and any
 * cross-origin redirect rather than treating them as an implicit safe target.
 */
export function assertE2EPageOrigin(pageURL: string, verifiedOrigin: string): void {
  const expectedOrigin = getE2ETargetOrigin(verifiedOrigin)
  let actualOrigin: string
  try {
    actualOrigin = new URL(pageURL).origin
  } catch {
    throw new Error(`E2E page URL is not on the verified E2E origin: ${pageURL}`)
  }

  if (actualOrigin !== expectedOrigin) {
    throw new Error(
      `E2E page left the verified E2E origin. Expected ${expectedOrigin}, received ${actualOrigin}.`,
    )
  }
}

/**
 * Writing E2E scenarios always require an explicit opt-in. Network location
 * is not a safety boundary: a loopback URL can still route to valuable data.
 */
export function assertE2EWriteTargetAllowed(baseURL: string, allowWrites = false): URL {
  const target = parseE2ETarget(baseURL)

  if (!allowWrites) {
    throw new Error(
      `Refusing E2E writes to ${target.origin}. Set E2E_ALLOW_WRITES=true only for ` +
        'an intentionally disposable E2E stack.',
    )
  }

  return target
}

export function getE2ETargetMarker(environment: E2EEnvironment = process.env): string {
  const marker = environment.E2E_TARGET_MARKER?.trim()
  if (!marker) {
    throw new Error(
      'E2E_TARGET_MARKER must be set to the non-sensitive marker of the disposable E2E stack.',
    )
  }
  return marker
}

/**
 * Verify the target itself, after the caller has explicitly opted into writes.
 * The marker is an identifier rather than a secret and must exactly match the
 * value configured for the disposable stack.
 */
export async function assertE2ETargetMarker(
  baseURL: string,
  expectedMarker: string,
  fetchHealth: HealthFetcher = fetch,
): Promise<void> {
  const target = parseE2ETarget(baseURL)
  const healthURL = new URL(E2E_HEALTH_PATH, target)
  const response = await fetchHealth(healthURL, {
    headers: { accept: 'application/json' },
    // A redirected health probe could validate a different origin. Fail before
    // E2E starts rather than allowing a redirected marker check.
    redirect: 'error',
  })

  if (!response.ok) {
    throw new Error(`E2E target health check returned HTTP ${response.status}.`)
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new Error('E2E target health check did not return valid JSON.')
  }

  const observedMarker =
    typeof payload === 'object' && payload !== null
      ? (payload as Record<string, unknown>).e2e_target_marker
      : undefined
  if (typeof observedMarker !== 'string' || !observedMarker) {
    throw new Error('E2E target health marker is missing.')
  }
  if (observedMarker !== expectedMarker) {
    throw new Error('E2E target health marker did not match the expected marker.')
  }
}

export function getE2ECredentials(environment: E2EEnvironment = process.env): E2ECredentials {
  const username = environment.E2E_USERNAME?.trim()
  if (!username) {
    throw new Error('E2E_USERNAME must be set for authenticated E2E tests.')
  }

  const password = environment.E2E_PASSWORD
  if (!password?.trim()) {
    throw new Error('E2E_PASSWORD must be set for authenticated E2E tests.')
  }

  return { username, password }
}

export function getE2EReviewerCredentials(
  environment: E2EEnvironment = process.env,
): E2ECredentials {
  const username = environment.E2E_REVIEWER_USERNAME?.trim()
  if (!username) {
    throw new Error('E2E_REVIEWER_USERNAME must be set for authenticated E2E tests.')
  }

  const password = environment.E2E_REVIEWER_PASSWORD
  if (!password?.trim()) {
    throw new Error('E2E_REVIEWER_PASSWORD must be set for authenticated E2E tests.')
  }

  return { username, password }
}

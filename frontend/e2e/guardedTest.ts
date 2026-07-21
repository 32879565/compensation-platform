import { expect, test as base, type BrowserContext, type Page } from '@playwright/test'

import type { E2ECredentials } from '../src/e2eTargetSafety'
import {
  assertE2EPageOrigin,
  getE2ETargetOrigin,
  isE2ERequestURLAllowed,
} from '../src/e2eTargetSafety'

const defaultBaseURL = 'http://127.0.0.1:8080'

/**
 * Must remain the same sole configuration source that globalSetup validates.
 * Do not use Playwright's per-spec baseURL fixture here: a future test can
 * override that option without re-running the disposable-target marker check.
 */
function getGloballyValidatedE2EBaseURL(): string {
  return process.env.PLAYWRIGHT_BASE_URL ?? defaultBaseURL
}

export interface E2ETargetGuard {
  verifiedOrigin: string
  blockedRequestURLs: string[]
  assertPageOrigin: (page: Page) => void
}

type E2EFixtures = {
  e2eTargetGuard: E2ETargetGuard
}

/**
 * Install the network portion of the target guard on a browser context.
 * Exported so the redirect regression can exercise the same guard against a
 * small real HTTP server, rather than simulating a redirect by rewriting a
 * Playwright route URL.
 */
export async function installE2ETargetGuard(
  context: BrowserContext,
  baseURL: string,
): Promise<E2ETargetGuard> {
  const verifiedOrigin = getE2ETargetOrigin(baseURL)
  const blockedRequestURLs: string[] = []

  await context.route('**/*', async (route) => {
    const requestURL = route.request().url()
    if (!isE2ERequestURLAllowed(requestURL, verifiedOrigin)) {
      blockedRequestURLs.push(requestURL)
      await route.abort('blockedbyclient')
      return
    }
    // The page-level Chromium Fetch guard below catches redirect hops that
    // Playwright's routing layer does not expose. Continue normal same-origin
    // requests immediately so XHR/fetch behavior remains unmodified.
    await route.continue()
  })

  // The application has no browser-WebSocket dependency.  Block all sockets
  // rather than treating ws:/wss: as an implicit safe non-HTTP(S) scheme. A
  // routeWebSocket handler does not connect unless it explicitly calls
  // connectToServer(), so this prevents any socket handshake from reaching an
  // external endpoint.
  await context.routeWebSocket('**/*', async (webSocketRoute) => {
    const socketURL = webSocketRoute.url()
    blockedRequestURLs.push(socketURL)
    await webSocketRoute.close({ code: 1008, reason: 'E2E target guard blocked WebSocket' })
  })

  return {
    verifiedOrigin,
    blockedRequestURLs,
    assertPageOrigin: (page) => assertE2EPageOrigin(page.url(), verifiedOrigin),
  }
}

/**
 * Playwright's route handler does not receive every browser-generated request
 * that follows an HTTP redirect response. Chromium's Fetch domain does, so
 * attach a request-stage HTTP(S) guard to each fixture page before exposing it
 * to a spec. This closes otherwise invisible 30x hops before DNS or a socket
 * is opened, including redirects of document, XHR, and resource requests.
 */
export async function installE2EPageRedirectGuard(
  page: Page,
  guard: E2ETargetGuard,
): Promise<() => Promise<void>> {
  const session = await page.context().newCDPSession(page)
  let detached = false

  const handlePausedRequest = async ({
    requestId,
    request,
  }: {
    requestId: string
    request: { url: string }
  }): Promise<void> => {
    try {
      if (!isE2ERequestURLAllowed(request.url, guard.verifiedOrigin)) {
        guard.blockedRequestURLs.push(request.url)
        await session.send('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' })
        return
      }
      await session.send('Fetch.continueRequest', { requestId })
    } catch {
      // A closing page closes its CDP target. Any other failure is fail-closed
      // by closing the page, which prevents a sensitive interaction from
      // continuing without an active redirect guard.
      if (!detached) await page.close().catch(() => undefined)
    }
  }

  session.on('Fetch.requestPaused', handlePausedRequest)
  await session.send('Fetch.enable', {
    patterns: [{ urlPattern: '*', requestStage: 'Request' }],
  })

  return async () => {
    detached = true
    session.off('Fetch.requestPaused', handlePausedRequest)
    await session.detach().catch(() => undefined)
  }
}

/**
 * All writing browser specs must import this test fixture instead of Playwright
 * directly. It installs a context-wide request guard before the page fixture
 * is exposed, so a future spec cannot accidentally navigate to a different
 * HTTP(S) origin before its first explicit origin assertion.
 */
export const test = base.extend<E2EFixtures>({
  e2eTargetGuard: async ({ context }, fixtureDone) => {
    const guard = await installE2ETargetGuard(context, getGloballyValidatedE2EBaseURL())
    await fixtureDone(guard)
  },
  page: async ({ page, e2eTargetGuard }, fixtureDone) => {
    // This dependency ensures both guards are installed before the page fixture
    // becomes available to a spec or can perform its first navigation.
    const removeRedirectGuard = await installE2EPageRedirectGuard(page, e2eTargetGuard)
    try {
      await fixtureDone(page)
    } finally {
      await removeRedirectGuard()
    }
  },
})

export { expect }

function targetURL(guard: E2ETargetGuard, path: string): string {
  return new URL(path, `${guard.verifiedOrigin}/`).toString()
}

/** Navigate, then make the verified-origin assertion mandatory at the call site. */
export async function gotoE2EPage(
  page: Page,
  guard: E2ETargetGuard,
  path: string,
): Promise<void> {
  await page.goto(targetURL(guard, path))
  guard.assertPageOrigin(page)
}

/**
 * Shared authentication flow: origin checks run after navigation and before
 * every credential interaction or authentication POST trigger.
 */
export async function signInToE2E(
  page: Page,
  guard: E2ETargetGuard,
  credentials: E2ECredentials,
): Promise<void> {
  await gotoE2EPage(page, guard, '/login')
  await expect(page).toHaveURL(targetURL(guard, '/login'))
  await expect(page.getByTestId('login-form')).toBeVisible()

  guard.assertPageOrigin(page)
  await page.locator('input[autocomplete="username"]').fill(credentials.username)
  guard.assertPageOrigin(page)
  await page.locator('input[autocomplete="current-password"]').fill(credentials.password)
  guard.assertPageOrigin(page)
  await page.getByTestId('login-submit').click()

  // ProtectedRoute can preserve the previously requested same-origin path in
  // location state. A successful sign-in therefore need not land on `/` when
  // a lifecycle test switches principals on an existing page.
  await expect(page).not.toHaveURL(targetURL(guard, '/login'))
  guard.assertPageOrigin(page)
  await expect(page.getByTestId('app-shell')).toBeVisible()
}

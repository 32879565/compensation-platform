import { createServer, type Server } from 'node:http'

import {
  assertE2ETargetMarker,
  getE2ECredentials,
  getE2ETargetOrigin,
  getE2ETargetMarker,
} from '../src/e2eTargetSafety'

import {
  expect,
  gotoE2EPage,
  installE2EPageRedirectGuard,
  installE2ETargetGuard,
  signInToE2E,
  test,
} from './guardedTest'

const credentials = getE2ECredentials()

interface RedirectTestTarget {
  sourceOrigin: string
  redirectedLoginURL: string
  sourceRequests: string[]
  emittedRedirectLocations: string[]
  forbiddenRequests: string[]
  close: () => Promise<void>
}

interface WebSocketTestTarget {
  socketURL: string
  upgradeRequests: string[]
  close: () => Promise<void>
}

async function listen(server: Server): Promise<string> {
  await new Promise<void>((resolve, reject) => {
    const rejectOnError = (error: Error) => reject(error)
    server.once('error', rejectOnError)
    server.listen(0, '127.0.0.1', () => {
      server.off('error', rejectOnError)
      resolve()
    })
  })

  const address = server.address()
  if (!address || typeof address === 'string') {
    throw new Error('Could not determine the local redirect test server address.')
  }
  return `http://127.0.0.1:${address.port}`
}

async function closeServer(server: Server): Promise<void> {
  if (!server.listening) return
  server.closeAllConnections()
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()))
  })
}

/**
 * The source server emits an actual HTTP 302. The target server is deliberately
 * a different loopback origin: observing no request there proves the browser
 * guard aborted the redirect before it crossed an origin or reached a network
 * endpoint, without using an Internet host in the test.
 */
async function startRedirectTestTarget(marker: string): Promise<RedirectTestTarget> {
  const sourceRequests: string[] = []
  const emittedRedirectLocations: string[] = []
  const forbiddenRequests: string[] = []
  const forbiddenServer = createServer((request, response) => {
    forbiddenRequests.push(`${request.method ?? 'GET'} ${request.url ?? '/'}`)
    response.writeHead(204)
    response.end()
  })
  const redirectedOrigin = await listen(forbiddenServer)
  const redirectedLoginURL = `${redirectedOrigin}/login`

  const sourceServer = createServer((request, response) => {
    const requestPath = request.url ?? '/'
    sourceRequests.push(`${request.method ?? 'GET'} ${requestPath}`)

    if (requestPath === '/api/health') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ status: 'ok', e2e_target_marker: marker }))
      return
    }

    if (requestPath === '/login') {
      emittedRedirectLocations.push(redirectedLoginURL)
      response.writeHead(302, { location: redirectedLoginURL })
      response.end()
      return
    }

    response.writeHead(404)
    response.end()
  })
  const sourceOrigin = await listen(sourceServer)

  return {
    sourceOrigin,
    redirectedLoginURL,
    sourceRequests,
    emittedRedirectLocations,
    forbiddenRequests,
    close: async () => {
      await Promise.all([closeServer(sourceServer), closeServer(forbiddenServer)])
    },
  }
}

/**
 * A real HTTP upgrade listener lets the regression prove that the browser
 * never opens a forbidden WebSocket connection. The handler deliberately
 * does not complete a WebSocket handshake: any recorded upgrade is already a
 * target-guard failure.
 */
async function startWebSocketTestTarget(): Promise<WebSocketTestTarget> {
  const upgradeRequests: string[] = []
  const server = createServer((_, response) => {
    response.writeHead(404)
    response.end()
  })
  server.on('upgrade', (request, socket) => {
    upgradeRequests.push(`${request.method ?? 'GET'} ${request.url ?? '/'}`)
    socket.destroy()
  })
  const origin = await listen(server)

  return {
    socketURL: `${origin.replace(/^http/, 'ws')}/e2e`,
    upgradeRequests,
    close: () => closeServer(server),
  }
}

test('blocks a real cross-origin login 302 before credentials can be filled or submitted', async ({
  browser,
}) => {
  const expectedMarker = getE2ETargetMarker()
  const target = await startRedirectTestTarget(expectedMarker)
  const context = await browser.newContext({
    baseURL: target.sourceOrigin,
    serviceWorkers: 'block',
  })

  try {
    // The configured E2E suite has already run globalSetup against the stack.
    // Verify the same marker contract on this temporary, verified source before
    // exercising its real /login response.
    await assertE2ETargetMarker(target.sourceOrigin, expectedMarker)
    const e2eTargetGuard = await installE2ETargetGuard(context, target.sourceOrigin)
    const page = await context.newPage()
    const removeRedirectGuard = await installE2EPageRedirectGuard(page, e2eTargetGuard)

    try {
      const authRequests: string[] = []
      page.on('request', (request) => {
        if (request.url().includes('/api/auth/login')) authRequests.push(request.url())
      })

      await expect(signInToE2E(page, e2eTargetGuard, credentials)).rejects.toThrow()

      expect(target.sourceRequests).toContain('GET /api/health')
      expect(target.sourceRequests).toContain('GET /login')
      expect(target.emittedRedirectLocations).toEqual([target.redirectedLoginURL])
      expect(e2eTargetGuard.blockedRequestURLs).toContain(target.redirectedLoginURL)
      expect(target.forbiddenRequests).toEqual([])
      expect(authRequests).toEqual([])
      await expect(page.locator('input[autocomplete="username"]')).toHaveCount(0)
      expect(() => e2eTargetGuard.assertPageOrigin(page)).toThrow('verified E2E origin')
    } finally {
      await removeRedirectGuard()
    }
  } finally {
    await context.close()
    await target.close()
  }
})

test('blocks a forbidden WebSocket before an HTTP upgrade reaches the endpoint', async ({ browser }) => {
  const target = await startWebSocketTestTarget()
  const context = await browser.newContext({ serviceWorkers: 'block' })

  try {
    const e2eTargetGuard = await installE2ETargetGuard(context, 'http://127.0.0.1:18080')
    const page = await context.newPage()
    const removeRedirectGuard = await installE2EPageRedirectGuard(page, e2eTargetGuard)

    try {
      const terminalEvent = await page.evaluate(async (socketURL) => {
        return new Promise<string>((resolve) => {
          const socket = new WebSocket(socketURL)
          const timer = window.setTimeout(() => resolve('timeout'), 2_000)
          socket.addEventListener('close', () => {
            window.clearTimeout(timer)
            resolve('close')
          })
          socket.addEventListener('error', () => {
            window.clearTimeout(timer)
            resolve('error')
          })
        })
      }, target.socketURL)

      expect(terminalEvent).not.toBe('timeout')
      expect(e2eTargetGuard.blockedRequestURLs).toContain(target.socketURL)
      expect(target.upgradeRequests).toEqual([])
    } finally {
      await removeRedirectGuard()
    }
  } finally {
    await context.close()
    await target.close()
  }
})

test.describe('per-spec base URL overrides', () => {
  // This intentionally differs from PLAYWRIGHT_BASE_URL. The guard must use
  // only the globally marker-validated environment value, never this fixture.
  test.use({ baseURL: 'http://127.0.0.1:9' })

  test('cannot redirect guarded navigation away from the globally verified target', async ({
    page,
    e2eTargetGuard,
  }) => {
    const expectedOrigin = getE2ETargetOrigin(
      process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:8080',
    )

    expect(e2eTargetGuard.verifiedOrigin).toBe(expectedOrigin)
    await gotoE2EPage(page, e2eTargetGuard, '/login')
    await expect(page).toHaveURL(`${expectedOrigin}/login`)
    e2eTargetGuard.assertPageOrigin(page)
  })
})

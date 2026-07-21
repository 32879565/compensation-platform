import {
  assertE2ETargetMarker,
  assertE2EWriteTargetAllowed,
  getE2ECredentials,
  getE2EReviewerCredentials,
  getE2ETargetMarker,
} from '../src/e2eTargetSafety'

const defaultBaseURL = 'http://127.0.0.1:8080'

export default async function globalSetup(): Promise<void> {
  const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? defaultBaseURL

  assertE2EWriteTargetAllowed(baseURL, process.env.E2E_ALLOW_WRITES === 'true')
  const expectedMarker = getE2ETargetMarker()
  // Validate all required environment before requesting the browser target.
  // This avoids accidentally opening a browser session with fallback credentials.
  getE2ECredentials()
  getE2EReviewerCredentials()
  await assertE2ETargetMarker(baseURL, expectedMarker)
}

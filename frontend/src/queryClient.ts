import { QueryClient } from '@tanstack/react-query'

// Session data (especially payroll results) must never survive an account
// transition.  Exporting the one application client lets authentication clear
// every query before another principal can render a protected route.
export const queryClient = new QueryClient()

export function clearSessionQueries(): void {
  queryClient.clear()
}

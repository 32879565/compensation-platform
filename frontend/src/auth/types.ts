export interface AuthUser {
  username: string
  permissions: string[]
  globalPermissions: string[]
}

export interface LoginResponse {
  access_token: string
  token_type: string
  username: string
  permissions: string[]
  // Optional during a rolling deployment.  Missing capability data must
  // fail closed in the UI until the backend reports permission-level scope.
  global_permissions?: string[]
}

export interface AuthUser {
  username: string
  permissions: string[]
}

export interface LoginResponse {
  access_token: string
  token_type: string
  username: string
  permissions: string[]
}

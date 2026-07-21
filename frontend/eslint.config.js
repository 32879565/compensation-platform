import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

export default tseslint.config({ ignores: ['dist/**', 'playwright-report/**', 'test-results/**'] }, js.configs.recommended, ...tseslint.configs.recommended, {
  files: ['src/**/*.{ts,tsx}', 'e2e/**/*.ts', 'playwright.config.ts'],
  plugins: { 'react-hooks': reactHooks },
  rules: { ...reactHooks.configs.recommended.rules },
})

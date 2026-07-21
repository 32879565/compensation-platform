import { vi } from 'vitest'

// jsdom 未实现 matchMedia，AntD 响应式依赖它；提供一个惰性 mock。
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
})

// AntD probes pseudo-elements while calculating responsive overflow. jsdom
// accepts the second argument but reports it as a noisy "not implemented"
// error. Its base element styles are sufficient for component tests.
const jsdomGetComputedStyle = window.getComputedStyle.bind(window)
Object.defineProperty(window, 'getComputedStyle', {
  configurable: true,
  value: (element: Element) => jsdomGetComputedStyle(element),
})

// Minimal browser globals for the chat store; fetch is set per test (lib.mjs).
const store = new Map()
globalThis.window = globalThis
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  key: (i) => [...store.keys()][i] ?? null,
  get length() { return store.size },
  clear: () => store.clear(),
}
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} }
globalThis.location = { protocol: 'http:', host: 'localhost', origin: 'http://localhost', pathname: '/', search: '', hash: '' }
globalThis.document = { addEventListener() {}, removeEventListener() {}, visibilityState: 'visible', hidden: false, cookie: '' }
try { Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', platform: 'node' }, configurable: true }) } catch {}
globalThis.addEventListener = () => {}
globalThis.removeEventListener = () => {}
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
globalThis.fetch = async () => ({ ok: false, json: async () => ({}) })
globalThis.WebSocket = class { static OPEN = 1; readyState = 1; send() {} close() {} }

/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Prefix of every API request; defaults to /api when unset. */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

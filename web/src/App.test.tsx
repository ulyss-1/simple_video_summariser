import { QueryClientProvider } from '@tanstack/react-query'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { App } from './App'
import { createQueryClient } from './queryClient'

describe('App', () => {
  it('renders the app name in a main landmark inside the providers', () => {
    const html = renderToString(
      <QueryClientProvider client={createQueryClient()}>
        <App />
      </QueryClientProvider>,
    )
    expect(html).toContain('ytdigest')
    expect(html).toContain('<main')
  })
})

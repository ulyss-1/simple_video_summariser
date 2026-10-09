import type { RouteObject } from 'react-router'
import { Compare } from './Compare'
import { Library } from './Library'
import { NotFound } from './NotFound'
import { Ops } from './Ops'
import { RouteError } from './RouteError'
import { Search } from './Search'
import { Shell } from './Shell'
import { Transcript } from './Transcript'
import { VideoDetail } from './VideoDetail'

// The one route table: main.tsx and the tests both import it. No loaders or
// actions: TanStack Query owns server state (architecture §9).
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Library /> },
      { path: 'videos/:videoId', element: <VideoDetail /> },
      { path: 'videos/:videoId/transcript', element: <Transcript /> },
      { path: 'videos/:videoId/compare', element: <Compare /> },
      { path: 'search', element: <Search /> },
      { path: 'ops', element: <Ops /> },
      { path: '*', element: <NotFound /> },
    ],
  },
]

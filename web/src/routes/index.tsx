import type { RouteObject } from 'react-router'
import { PlayerProvider } from '../components/PlayerContext'
import { AddChannel } from './AddChannel'
import { ChannelCatalog } from './ChannelCatalog'
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
      // Per-route PlayerProvider, not one in Shell: only these three routes
      // render a Player, and Library/Search/Ops have none to register (#181).
      {
        path: 'videos/:videoId',
        element: (
          <PlayerProvider>
            <VideoDetail />
          </PlayerProvider>
        ),
      },
      {
        path: 'videos/:videoId/transcript',
        element: (
          <PlayerProvider>
            <Transcript />
          </PlayerProvider>
        ),
      },
      {
        path: 'videos/:videoId/compare',
        element: (
          <PlayerProvider>
            <Compare />
          </PlayerProvider>
        ),
      },
      { path: 'channels/add', element: <AddChannel /> },
      { path: 'channels/:channelId/catalog', element: <ChannelCatalog /> },
      { path: 'search', element: <Search /> },
      { path: 'ops', element: <Ops /> },
      { path: '*', element: <NotFound /> },
    ],
  },
]

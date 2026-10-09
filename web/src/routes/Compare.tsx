import { useLocation, useParams } from 'react-router'
import { parseVideoId } from '../lib/videoId'

// Placeholder: replaced by #53.
export function Compare() {
  const { search } = useLocation()
  const videoId = parseVideoId(useParams().videoId)
  return (
    <section>
      <h1>Compare</h1>
      {videoId === null ? (
        <p>Video not found</p>
      ) : (
        <p>
          Video: <code>{videoId}</code>
        </p>
      )}
      <p>
        Query: <code>{search}</code>
      </p>
    </section>
  )
}

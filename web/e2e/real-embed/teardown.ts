import { execFileSync } from 'node:child_process'

// Removes the nginx container the real-embed webServer started (#178).
export default function teardown(): void {
  execFileSync('docker', ['rm', '-f', 'ytdigest-web-e2e'], { stdio: 'ignore' })
}

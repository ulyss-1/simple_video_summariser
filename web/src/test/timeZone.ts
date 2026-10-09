// The fixed, non-UTC zone the Vitest config pins for both projects (#127).
// Shared so the config and the tests that assert it cannot drift apart.
export const TEST_TIME_ZONE = 'America/Los_Angeles'

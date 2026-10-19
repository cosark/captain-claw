// The browser's card scrubber must agree with the Python one on every shared
// case (tests/fixtures/card_scrub_cases.json). Node ≥ 22.18 runs the .ts file
// directly: `node flight-deck/scripts/card-scrub-check.mjs`.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { findCardData, scrubCardData } from '../src/utils/cardScrub.ts'

const here = dirname(fileURLToPath(import.meta.url))
const { cases } = JSON.parse(readFileSync(join(here, '../../tests/fixtures/card_scrub_cases.json'), 'utf8'))
let failed = 0
for (const c of cases) {
  const got = findCardData(c.text).map((f) => (f.kind === 'pan' ? { kind: f.kind, last4: f.last4 } : { kind: f.kind }))
  const ok = JSON.stringify(got) === JSON.stringify(c.expect)
    && (c.scrubbed === undefined || scrubCardData(c.text).text === c.scrubbed)
  if (!ok) {
    failed++
    console.error(`FAIL ${JSON.stringify(c.text)}\n  want ${JSON.stringify(c.expect)}\n  got  ${JSON.stringify(got)}`)
  }
}
console.log(`${cases.length - failed}/${cases.length} card-scrub cases agree with the Python scrubber`)
process.exit(failed ? 1 : 0)

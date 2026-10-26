# Strategy Factory coverage status

Measured in permanent self-hosted CI against the materialized Strategy Factory source and the exact local dependency surface.

- Strategy Factory tests: 589 passed on Python 3.12 and 3.13
- Risk Request Composer tests: 6 passed on Python 3.12 and 3.13
- Acceptance scenarios: 25/25 passed
- Branch-aware Strategy Factory coverage: 91%
- Required release threshold: 90%
- Coverage gate status: PASS
- Permanent CI run: `30366085770`

The coverage gate is only one release condition. Independent Strategy Audit integration, paper-runtime admission E2E, longer staging soak and final independent audit remain blocking. This file does not authorize merge, release, deployment or live execution.

# Third-party notices

This file records the dependency inventory used by Aftercare Agent 0.1.0a0. It is an
audit aid, not a replacement for each dependency's license text. Versions come from
`uv.lock` and `web/pnpm-lock.yaml`; license identifiers come from installed package
metadata, with the platform-only `uvloop` entry checked against its pinned PyPI release.

## Python runtime dependencies

| Package | Version | Declared license |
|---|---:|---|
| evidence-gated-memory | 0.6.0, Git commit `5d1302e…` | MIT; upstream `LICENSE` included |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.141.1 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| psycopg | 3.3.5 | LGPL-3.0-only |
| psycopg-binary | 3.3.5 | LGPL-3.0-only |
| psycopg-pool | 3.3.1 | LGPL-3.0-only |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| PyJWT | 2.14.0 | MIT |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| PyYAML | 6.0.3 | MIT |
| starlette | 1.6.0 | BSD-3-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| types-PyYAML | 6.0.12.20260906 | MIT |
| tzdata | 2026.3 | Apache-2.0 |
| uvicorn | 0.52.4 | BSD-3-Clause |
| uvloop | 0.22.1, non-Windows marker | MIT OR Apache-2.0 |
| watchfiles | 1.2.0 | MIT |
| websockets | 17.1 | BSD-3-Clause |

## Browser runtime dependencies

| Package | Version | Declared license |
|---|---:|---|
| react | 19.3.0 | MIT |
| react-dom | 19.3.0 | MIT |
| scheduler | 0.28.0 | MIT |

The locked frontend build and test toolchain additionally contains packages declaring MIT,
Apache-2.0, MPL-2.0, ISC and BSD-3-Clause licenses. Run
`pnpm --dir web licenses list --json` after a frozen-lockfile install to inspect the exact
transitive inventory for the current platform.

## Project-owned sample material

`aftercare_agent/connectors/data/commerce-sample.json`, the deterministic evaluation cases
and `web/public/favicon.svg` are project-owned synthetic assets. The repository does not
vendor a third-party dataset, model weight, customer export, font or stock-media bundle.

## Release note for Evidence-Gated-Memory

The pinned Evidence-Gated-Memory commit declares the SPDX expression `MIT` in
`pyproject.toml` and contains a standalone MIT `LICENSE`. Aftercare does not vendor EGM
source into this repository; the locked dependency and container build resolve that exact
Git commit. A redistribution that bundles EGM must preserve its license text and re-run the
inventory against the artifact actually shipped.

Re-run this inventory whenever either lock file changes. A dependency upgrade is not
accepted for release merely because installation and tests pass; its license identifier and
required notices must also be reviewed.

# Validation record

Validated locally on Windows, Python 3.12, 2026-11-07.

- 16 offline unit/integration tests passed (pagination, repeated pages, SKIMS decoding, JSON-LD, relative URLs, selector prices, missing values, currency separation, HTML script escaping, import, PDF, ZIP, and source failures).
- Alo live smoke: 4 products, 4 downloaded images, HTML/PDF/ZIP exported.
- SKIMS live smoke: 4 products, 4 downloaded images, HTML/PDF/ZIP exported.
- Generic JSON-LD live smoke through scripts/run_action.py: 1 SKIMS product and image, exports succeeded.
- Intentional product limits correctly marked these live smoke outputs as partial.
- Offline Chrome, 390 px viewport: four images loaded, no horizontal overflow, no JavaScript errors; details and favorites worked.
- PDF sample pages rendered and visually checked with Poppler.
- GitHub workflow static validation passed with actionlint v1.7.12 (official release checksum verified). Shellcheck and pyflakes were not installed for this check.
- The GitHub-hosted Windows/Linux CI matrix has NOT run: remote publication was deferred by the user. Linux behavior is covered by configured CI, not claimed as locally verified.

Source files contain no captured product assets or account credentials. Smoke output is under the ignored output/ directory.

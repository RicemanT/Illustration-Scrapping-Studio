# Contributing

Read the [development guide](docs/DEVELOPMENT.md) and [implementation guide](IMPLEMENTATION_GUIDE.md) before making substantial changes.

Use a disposable library for testing. Preserve existing artist/query semantics, per-collection file isolation, source provenance and recovery records. Keep local mode working when changing remote deployment. Add focused regressions for storage, parsing, job recovery or proxy behavior; use offline fixtures rather than live provider requests in automated checks.

Run backend tests, frontend tests and the production build. Include the problem, resulting behavior and validation in a pull request. Do not include scraped media, provider credentials, private diagnostic exports or generated dependencies/builds.

Project code is licensed under MIT; contributions are submitted under that license. Third-party dependencies retain their own licenses.

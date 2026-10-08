# Adding a language SDK

Each language has an independent package under `sdks/<language>/`, with its source, package metadata, README, tests and runnable examples. Python and TypeScript are the first implementations; add a sibling directory for another language rather than making a runtime bridge to an existing SDK.

## Shared contract

`protocol/models.json` is the canonical model/capability snapshot. It includes stable constant names, wire IDs, access tiers, reasoning defaults and native attachment profiles. Edit it once, then run `python scripts/sync_protocol.py` and commit the generated package-local files. Run with `--check` in CI. Extend the generator when adding a language.

`protocol/fixtures/chat.json` describes a request and SSE reply with Unicode and opaque assistant parts. `protocol/fixtures/documents.json` contains small synthetic exported-document fixtures and expected text. Language tests should consume these shared fixtures and test their own transport, cancellation, session lifecycle and package installation. Shared fixtures are development inputs: installed SDKs must use package-local metadata and must not depend on this checkout.

Preserve unknown upstream message/part fields, recognize `[DONE]`, bound response and attachment size, retain partial output on incomplete replies, and avoid committing failed turns to conversations. Publish capability differences in the language README rather than silently dropping data.

Anonymous session bootstrap runs against Duck.ai, defaults to isolated headless Chromium, and must release owned browser/process/profile resources. Never kill a user-owned CDP browser. Catalogue APIs and local document preparation must work without launching a browser. Surface upstream entitlement, challenge and rate-limit failures without fabricating responses or silently retrying.

## Package boundaries

SDK packages contain only their runtime library, declarations/typing metadata, package metadata and documentation. The optional root `example/` desktop project is not part of any SDK distribution. Hono and other framework examples belong with the appropriate language SDK and are not mandatory core dependencies.

Document runtime versions, native dependencies, installation, chat/stream/conversation/file APIs, cancellation and error behavior. Add the language to the root README and CI. Verify the built package in a clean consumer project outside the checkout so accidental workspace imports are caught. Version and release each package independently; updating GitHub does not automatically publish to a package registry.

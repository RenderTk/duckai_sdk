# Shared Duck.ai protocol inputs

- `models.json`: canonical model IDs, display/access metadata, reasoning choices and native attachment profiles. This is a checked-in snapshot, not a promise of account entitlement or permanent upstream availability.
- `fixtures/chat.json`: language-neutral request, Unicode SSE response and opaque assistant parts.
- `fixtures/documents.json`: synthetic, base64-encoded exported documents with expected extracted text fragments.

Run `python scripts/sync_protocol.py` after changing the catalogue or shared file-format guide. Run `python scripts/sync_protocol.py --check` to detect stale generated files. Packages ship their own generated metadata; the protocol directory is only needed for repository development and tests.

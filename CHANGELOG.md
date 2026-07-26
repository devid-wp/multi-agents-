# Changelog

## 0.3.0 - 2026-07-26

- Added persistent room-scoped chats with one built-in General room.
- Added Ollama readiness and missing-model guidance in the UI.
- Restored per-agent provider selection instead of forcing Ollama.
- Added diff preview and explicit approve/reject flow for file writes.
- Added a real HTTP/SSE workspace watcher test and normalized Windows paths.
- Restricted the release server to localhost and disabled Bash, Git and delete tools.
- Replaced the Windows bootstrap with a deterministic Python 3.11+ installer.
- Added room creation/switching and visible room-scoped history to the UI.

### Known limitations

- Local alpha only; no authentication or multi-user isolation.
- Rooms have create/list UI but no rename/delete UI yet.
- Session and proposal state is stored as local JSON, not a concurrent database.

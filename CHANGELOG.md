# Changelog

## 0.3.0 - 2026-07-26

- Added persistent room-scoped chats with one built-in General room.
- Added Ollama readiness and missing-model guidance in the UI.
- Restored per-agent provider selection instead of forcing Ollama.
- Added diff preview and explicit approve/reject flow for file writes.
- Added a real HTTP/SSE workspace watcher test and normalized Windows paths.

### Known limitations

- Local alpha only; no authentication or multi-user isolation.
- Rooms have create/list APIs but no rename/delete UI yet.
- Bash, Git and delete tools are not covered by the file-change approval flow.
- Session and proposal state is stored as local JSON, not a concurrent database.

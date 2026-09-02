// Trinity — modules/config.js (Phase 2+ 0.7.0)
// Единый источник ENDPOINTS — импортируется в app.js
export const ENDPOINTS = {
  chat:          "/api/chat",
  chatHistory:   "/api/chat/history",
  agentsActive:  "/api/agents/active",
  agentsSwitch:  "/api/agents/switch",
  wsTree:        "/api/workspace/tree",
  wsFile:        "/api/workspace/file",
  wsStream:      "/api/workspace/stream",
  settingsGet:   "/api/settings",
  settingsSet:   "/api/settings",
  health:        "/api/health",
  version:       "/api/version",
  rooms:         "/api/rooms",
  changes:       "/api/changes",
  backup:        "/api/backup",
  backupIntegrity: "/api/backup/integrity",
};

export const SSE_BACKOFF_MS = [500, 1000, 2000, 4000, 5000];
export const PING_TIMEOUT_MS = 30_000;

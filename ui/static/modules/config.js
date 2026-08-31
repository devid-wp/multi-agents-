// Trinity — modules/config.js (Phase 2)
// Вынесено из ui/static/app.js:22 — единый источник ENDPOINTS
export const ENDPOINTS = {
  chat:          "/api/chat",
  chatHistory:   "/api/chat/history",
  agentsActive:  "/api/agents/active",
  agentsSwitch:  "/api/agents/switch",
  wsTree:        "/api/workspace/tree",
  wsStream:      "/api/workspace/stream",
  settingsGet:   "/api/settings",
  settingsSet:   "/api/settings",
  health:        "/api/health",
  rooms:         "/api/rooms",
  changes:       "/api/changes",
};

export const SSE_BACKOFF_MS = [500, 1000, 2000, 4000, 5000];
export const PING_TIMEOUT_MS = 30_000;

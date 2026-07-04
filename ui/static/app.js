/* Trinity — Mission Control Dashboard
 * Vanilla JS + Tailwind (CDN). No build step.
 *
 * Sections (in order):
 *   1. Constants & config
 *   2. State
 *   3. Utilities
 *   4. SSE clients (connectSSE + postSSE)
 *   5. Settings modal
 *   6. Command Bridge renderers
 *   7. Workspace tree
 *   8. Send message (POST /api/chat)
 *  10. boot()
 */

(() => {
  "use strict";

  // ════════════════════════════════════════════════════════════
  // 1. Constants & config
  // ════════════════════════════════════════════════════════════
  const ENDPOINTS = {
    chat:          "/api/chat",
    agentsActive:  "/api/agents/active",
    agentsSwitch:  "/api/agents/switch",
    wsTree:        "/api/workspace/tree",       // GET ?path=. &hidden=0|1
    wsStream:      "/api/workspace/stream",     // GET, persistent SSE
    settingsGet:   "/api/settings",
    settingsSet:   "/api/settings",
    health:        "/api/health",
  };

  const SSE_BACKOFF_MS = [500, 1000, 2000, 4000, 5000];  // capped at 5s
  const PING_TIMEOUT_MS = 30_000;  // before we mark a stream offline

  // ════════════════════════════════════════════════════════════
  // 2. State
  // ════════════════════════════════════════════════════════════
  const state = {
    running: false,        // true while a chat run is in progress
    abortController: null, // for /api/chat fetch
    bridge: [],            // transcript of ProgressEvents for the current run
    workspace: { entries: [], root: "" },
    sse: {
      ws: null,
    },
    selectedAgent: "planner",
    activeAgent: null,
  };

  // ════════════════════════════════════════════════════════════
  // 3. Utilities
  // ════════════════════════════════════════════════════════════
  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function safeJson(obj) {
    try { return JSON.stringify(obj, null, 2); } catch { return String(obj); }
  }
  function truncate(s, n) {
    s = String(s ?? "");
    return s.length > n ? s.slice(0, n) + "…" : s;
  }
  function formatTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);  // server sends seconds
    return d.toLocaleTimeString(undefined, { hour12: false });
  }
  function formatMs(ms) {
    if (ms == null) return "";
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  }
  function agentEmoji(a) {
    return ({ planner: "🧠", critic: "🔍", executor: "⚙️", manager: "🎯" }[a] || "•");
  }
  function debounce(fn, ms) {
    let t = null;
    return (...args) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => { t = null; fn(...args); }, ms);
    };
  }

  // ════════════════════════════════════════════════════════════
  // 4. SSE clients
  // ════════════════════════════════════════════════════════════

  /**
   * Persistent SSE connection with auto-reconnect (exponential backoff).
   * onEvent receives a parsed JSON object.
   * onStatus receives "open" | "reconnecting" | "offline".
   * Returns a handle { close() }.
   */
  function connectSSE(url, onEvent, { onStatus, name = "sse" } = {}) {
    let attempt = 0;
    let es = null;
    let closed = false;
    let firstFailureAt = null;
    let pingTimer = null;

    function setStatus(s) {
      if (onStatus) onStatus(s);
    }

    function armPing() {
      if (pingTimer) clearTimeout(pingTimer);
      pingTimer = setTimeout(() => {
        if (firstFailureAt == null) firstFailureAt = Date.now();
        if (Date.now() - firstFailureAt > PING_TIMEOUT_MS) {
          setStatus("offline");
        } else {
          setStatus("reconnecting");
        }
      }, PING_TIMEOUT_MS);
    }

    function scheduleReconnect() {
      if (closed) return;
      const delay = SSE_BACKOFF_MS[Math.min(attempt, SSE_BACKOFF_MS.length - 1)];
      attempt++;
      armPing();
      setStatus("reconnecting");
      setTimeout(connect, delay);
    }

    function connect() {
      if (closed) return;
      try {
        es = new EventSource(url);
      } catch (err) {
        console.warn(`[${name}] EventSource ctor failed`, err);
        scheduleReconnect();
        return;
      }
      es.onopen = () => {
        attempt = 0;
        firstFailureAt = null;
        if (pingTimer) { clearTimeout(pingTimer); pingTimer = null; }
        setStatus("open");
      };
      es.onmessage = (msg) => {
        if (!msg.data) return;
        try {
          const ev = JSON.parse(msg.data);
          onEvent(ev);
        } catch (err) {
          console.warn(`[${name}] bad json`, msg.data, err);
        }
      };
      es.onerror = () => {
        try { es.close(); } catch {}
        es = null;
        scheduleReconnect();
      };
    }
    connect();

    return {
      close() {
        closed = true;
        if (pingTimer) clearTimeout(pingTimer);
        if (es) { try { es.close(); } catch {} }
        es = null;
      },
    };
  }

  /**
   * POST + SSE helper. /api/chat is POST-only. Uses fetch + ReadableStream
   * to read "data: …\n\n" frames. Returns { close() } to abort the stream.
   */
  async function postSSE(url, body, onEvent) {
    const controller = new AbortController();
    state.abortController = controller;
    let reader = null;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        let bodyText = "";
        try {
          bodyText = await res.text();
        } catch (_err) {
          bodyText = "<unreadable response>";
        }
        onEvent({ kind: "error", content: `HTTP ${res.status}: ${truncate(bodyText, 400)}` });
        return;
      }
      if (!res.body) {
        const bodyText = await res.text().catch(() => "");
        onEvent({ kind: "error", content: `No response stream: ${truncate(bodyText, 400)}` });
        return;
      }
      reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, idx).trim();
          buffer = buffer.slice(idx + 2);
          if (!frame.startsWith("data:")) continue;
          const json = frame.slice(5).trim();
          if (!json) continue;
          try {
            onEvent(JSON.parse(json));
          } catch (err) {
            console.warn("[chat] bad json frame", json, err);
          }
        }
      }
      if (buffer.trim().startsWith("data:")) {
        const json = buffer.trim().slice(5).trim();
        if (json) {
          try {
            onEvent(JSON.parse(json));
          } catch (err) {
            console.warn("[chat] bad json frame on flush", json, err);
          }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        onEvent({ kind: "error", content: String(err) });
      }
    } finally {
      state.abortController = null;
    }
    return {
      close() {
        try { controller.abort(); } catch {}
        if (reader) { try { reader.cancel(); } catch {} }
      },
    };
  }

  // ════════════════════════════════════════════════════════════
  // 5. Settings modal
  // ════════════════════════════════════════════════════════════
  const modal        = $("#settings-modal");
  const settingsForm = $("#settings-form");
  const settingsStat = $("#settings-status");

  function openSettings() {
    modal.classList.remove("hidden");
    loadSettings();
  }
  function closeSettings() {
    modal.classList.add("hidden");
  }
  async function loadSettings() {
    try {
      const r = await fetch(ENDPOINTS.settingsGet);
      if (!r.ok) return;
      const s = await r.json();
      const f = settingsForm;
      // Ключи НЕ приходят; оставляем как есть
      f.planner_base_url.value  = s.planner_base_url  || "";
      f.critic_base_url.value   = s.critic_base_url   || "";
      f.planner_model_url.value = s.planner_model_url || "";
      f.critic_model_url.value  = s.critic_model_url  || "";
      f.ollama_url.value        = s.ollama_url        || "";
      f.planner_model.value     = s.planner_model     || "";
      f.critic_model.value      = s.critic_model      || "";
      f.executor_model.value    = s.executor_model    || "";
    } catch (e) { console.warn("loadSettings failed", e); }
  }
  async function saveSettings(e) {
    e.preventDefault();
    const payload = {};
    for (const [k, v] of new FormData(settingsForm).entries()) {
      payload[k] = (v === "" ? null : v);
    }
    settingsStat.textContent = "Saving…";
    settingsStat.style.color = "var(--fg-dim)";
    try {
      const r = await fetch(ENDPOINTS.settingsSet, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      settingsStat.textContent = "✓ Saved";
      settingsStat.style.color = "var(--ok)";
      // Clear key fields
      settingsForm.planner_api_key.value = "";
      settingsForm.critic_api_key.value  = "";
      setTimeout(closeSettings, 500);
    } catch (err) {
      settingsStat.textContent = "✗ " + err.message;
      settingsStat.style.color = "var(--error)";
    }
  }
console.log("hello world ")
  // ════════════════════════════════════════════════════════════
  // 6. Live log stream and command bridge
  // ════════════════════════════════════════════════════════════
  const chatContainerEl = $("#chat-container");
  const chatInput       = $("#chat-input");
  const sendBtn         = $("#send-btn");

  function setRunningUI(running) {
    state.running = running;
    sendBtn.disabled = running;
    chatInput.disabled = running;
  }

  function renderBridgeEvent(ev) {
    state.bridge.push(ev);
    if (!chatContainerEl) return;

    const card = document.createElement("div");
    card.className = "card chat-card";
    card.innerHTML = bridgeCardHTML(ev);
    chatContainerEl.appendChild(card);
    chatContainerEl.scrollTop = chatContainerEl.scrollHeight;

    // Cross-column highlight: if a tool_call touches a file, light up the
    // corresponding workspace node.
    if ((ev.kind === "tool_call" || ev.kind === "tool_result")
        && ev.tool && (ev.tool.name === "read_file" || ev.tool.name === "write_file")
        && ev.tool.arguments && ev.tool.arguments.path) {
      highlightWorkspaceNode(String(ev.tool.arguments.path), ev.kind === "tool_result" ? 3000 : 0);
    }
  }

  function clearBridge() {
    if (chatContainerEl) {
      chatContainerEl.innerHTML = "<div class=\"log-header\">Live Log Stream</div>";
    }
    state.bridge = [];
  }

  async function refreshActiveAgent() {
    try {
      const res = await fetch(ENDPOINTS.agentsActive, { method: "GET" });
      if (!res.ok) return;
      const data = await res.json();
      state.selectedAgent = data.agent || state.selectedAgent;
      setActiveAgentButton(state.selectedAgent);
      updateAgentIndicatorState(state.selectedAgent, "ready");
    } catch (err) {
      console.warn("Failed to refresh active agent", err);
    }
  }

  async function switchAgent(agent) {
    if (!agent || state.running) return;
    const button = $(`.agent-btn[data-agent="${agent}"]`);
    if (button) button.disabled = true;

    try {
      const res = await fetch(ENDPOINTS.agentsSwitch, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent }),
      });
      if (!res.ok) {
        const errBody = await res.text().catch(() => "");
        throw new Error(`Failed to switch agent: ${res.status} ${truncate(errBody, 300)}`);
      }
      const data = await res.json();
      state.selectedAgent = data.agent || agent;
      setActiveAgentButton(state.selectedAgent);
      updateAgentIndicatorState(state.selectedAgent, "ready");
    } catch (err) {
      console.warn("switchAgent failed", err);
      renderBridgeEvent({ kind: "error", agent: "manager", content: String(err), timestamp: Date.now() / 1000 });
    } finally {
      if (button) button.disabled = false;
    }
  }

  function cardClassForEvent(ev) {
    const k = ev.kind || "info";
    const a = ev.agent || "manager";
    if (k === "error")       return "error";
    if (k === "final")       return "final";
    if (k === "info")        return "info " + a;
    if (k === "agent_message") return a;
    if (k === "agent_start" || k === "agent_done") return a;
    if (k === "tool_call")   return "tool_call";
    if (k === "tool_result") {
      return "tool_result" + (ev.result && ev.result.success ? "" : " failed");
    }
    return a;
  }

  function bridgeCardHTML(ev) {
    const a = ev.agent || "manager";
    const emoji = agentEmoji(a);
    const k = ev.kind || "info";
    const meta = `<div class="meta">
        <span class="badge">${escapeHtml(k)}</span>
        <span>${emoji} ${escapeHtml(a.toUpperCase())}</span>
        <span class="muted">${escapeHtml(formatTime(ev.timestamp))}</span>
      </div>`;

    if (k === "tool_call" && ev.tool) {
      return `${meta}
        <div class="body">
          <span class="tool-name">→ ${escapeHtml(ev.tool.name)}</span>
          ${ev.tool.id ? `<span class="muted"> · ${escapeHtml(ev.tool.id)}</span>` : ""}
          <pre>${escapeHtml(safeJson(ev.tool.arguments))}</pre>
        </div>`;
    }
    if (k === "tool_result" && ev.result) {
      const r = ev.result;
      const ok = r.success;
      return `${meta}
        <div class="body">
          <span class="tool-name">${ok ? "✓" : "✗"} ${escapeHtml(r.name || "")}</span>
          <span class="muted"> · ${escapeHtml(formatMs(r.duration_ms))}</span>
          <pre>${escapeHtml(truncate(r.output || r.error || "", 4000))}</pre>
        </div>`;
    }
    if (k === "agent_start") {
      return `${meta}<div class="body">${escapeHtml(ev.content || "starting…")}</div>`;
    }
    if (k === "agent_done") {
      return `${meta}<div class="body">${escapeHtml(ev.content || "done")}</div>`;
    }
    if (k === "final") {
      return `${meta}<div class="body">${escapeHtml(ev.content || "")}</div>`;
    }
    if (k === "error") {
      return `${meta}<div class="body">${escapeHtml(ev.content || "")}</div>`;
    }
    // info, agent_message, …
    return `${meta}<div class="body">${escapeHtml(ev.content || "")}</div>`;
  }

  // ════════════════════════════════════════════════════════════
  // 7. Workspace tree (left)
  // ════════════════════════════════════════════════════
  // ════════════════════════════════════════════════════════════
  const wsTreeEl   = $("#workspace-tree");
  const wsStatusEl = $("#workspace-status");

  function setWsStatus(s) {
    wsStatusEl.classList.remove("live", "reconnecting", "offline");
    if (s === "open" || s === "live") {
      wsStatusEl.classList.add("live");
      wsStatusEl.textContent = "● watching";
    } else if (s === "reconnecting") {
      wsStatusEl.classList.add("reconnecting");
      wsStatusEl.textContent = "● reconnecting…";
    } else {
      wsStatusEl.classList.add("offline");
      wsStatusEl.textContent = "● offline";
    }
  }

  async function loadWorkspaceTree() {
    try {
      const r = await fetch(`${ENDPOINTS.wsTree}?path=.`);
      if (!r.ok) return;
      const snap = await r.json();
      state.workspace = { entries: snap.entries || [], root: snap.root || "" };
      renderWorkspaceTree();
    } catch (e) { console.warn("loadWorkspaceTree failed", e); }
  }

  function renderWorkspaceTree() {
    // Build nested structure from flat entries
    const root = { name: ".", type: "dir", path: ".", children: [] };
    const dirs = { ".": root };
    const entries = state.workspace.entries || [];
    // Sort: dirs first, then by name
    const sorted = entries.slice().sort((a, b) => {
      if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
      return a.path.localeCompare(b.path);
    });
    for (const e of sorted) {
      const parent = dirs[dirname(e.path)] || root;
      if (e.type === "dir") {
        const node = { name: basename(e.path), type: "dir", path: e.path, children: [] };
        dirs[e.path] = node;
        parent.children.push(node);
      } else {
        parent.children.push({ name: basename(e.path), type: "file", path: e.path, size: e.size, mtime: e.mtime });
      }
    }
    wsTreeEl.innerHTML = "";
    wsTreeEl.appendChild(renderTreeNode(root, 0, true));
  }

  function renderTreeNode(node, depth, isRoot = false) {
    const li = document.createElement("li");
    const div = document.createElement("div");
    div.className = `node ${node.type}` + (depth === 0 && isRoot ? " open" : "");
    div.dataset.path = node.path;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = isRoot ? (state.workspace.root ? basename(state.workspace.root) || "." : "workspace") : node.name;
    div.appendChild(name);

    if (node.type === "file") {
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = formatSize(node.size);
      div.appendChild(meta);
    }
    li.appendChild(div);

    if (node.type === "dir" && node.children && node.children.length) {
      const ul = document.createElement("ul");
      for (const child of node.children) ul.appendChild(renderTreeNode(child, depth + 1));
      li.appendChild(ul);
      // Click to toggle
      div.addEventListener("click", () => div.classList.toggle("open"));
    } else if (node.type === "dir") {
      div.classList.add("open");  // empty dirs: show marker
    }
    return li;
  }

  function basename(p) {
    if (!p) return "";
    const i = p.lastIndexOf("/");
    return i < 0 ? p : p.slice(i + 1);
  }
  function dirname(p) {
    if (!p) return ".";
    const i = p.lastIndexOf("/");
    return i < 0 ? "." : p.slice(0, i) || ".";
  }
  function formatSize(n) {
    if (n == null) return "";
    if (n < 1024) return `${n}B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`;
    return `${(n / (1024 * 1024)).toFixed(1)}M`;
  }

  function applyWorkspaceChange(ev) {
    if (!ev || !ev.path) return;
    const p = ev.path.replace(/\\/g, "/");
    const entries = state.workspace.entries;
    const idx = entries.findIndex((e) => e.path === p);
    if (ev.type === "deleted") {
      if (idx >= 0) entries.splice(idx, 1);
    } else {
      // created / modified
      const stat = { path: p, type: p.includes("/") ? "file" : guessType(p), size: 0, mtime: Date.now() / 1000 };
      if (idx >= 0) entries[idx] = stat; else entries.push(stat);
    }
    renderWorkspaceTree();
    // flash the node briefly
    const node = wsTreeEl.querySelector(`.node[data-path="${cssEscape(p)}"]`);
    if (node) {
      node.classList.add("flash");
      setTimeout(() => node.classList.remove("flash"), 2000);
    }
  }

  function guessType(p) {
    // crude — server entries use type=file for files, dir for directories.
    // On a single-segment path we don't know — treat as file (most common).
    return "file";
  }

  function highlightWorkspaceNode(path, ms = 3000) {
    const node = wsTreeEl.querySelector(`.node[data-path="${cssEscape(path.replace(/\\/g, "/"))}"]`);
    if (!node) return;
    node.classList.add("active");
    setTimeout(() => node.classList.remove("active"), Math.max(500, ms));
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
  }

  // ════════════════════════════════════════════════════════════
  // 9. Send message (POST /api/chat → Command Bridge)
  // ════════════════════════════════════════════════════════════
  async function sendMessage(text, action = null) {
    if (state.running) return;
    if (!text.trim()) return;

    setRunningUI(true);

    // user message bubble
    renderBridgeEvent({ kind: "user", agent: "user", content: text, timestamp: Date.now() / 1000 });

    const payload = { message: text };

    const stop = await postSSE(ENDPOINTS.chat, payload, (ev) => {
      if (!ev || !ev.kind) return;
      ev.timestamp = ev.timestamp || Date.now() / 1000;
      renderBridgeEvent(ev);

      if (ev.kind === "agent_start" && ev.agent) {
        updateAgentIndicatorState(ev.agent, "active");
        state.activeAgent = ev.agent;
      }

      if (ev.kind === "agent_done" && ev.agent) {
        updateAgentIndicatorState(ev.agent, null);
        state.activeAgent = null;
      }
    });
    // Save handle for Stop button
    state.chatStop = stop;
    setRunningUI(false);
    state.chatStop = null;
  }

  function stopChat() {
    if (state.abortController) {
      try { state.abortController.abort(); } catch {}
    }
    if (state.chatStop) {
      try { state.chatStop.close(); } catch {}
    }
    renderBridgeEvent({
      kind: "info", agent: "manager",
      content: "⏹ Stopped by user",
      timestamp: Date.now() / 1000,
    });
    setRunningUI(false);
  }

  function toggleSidebar() {
    const sidebar = $("#col-workspace");
    const app = $("#app");
    sidebar.classList.toggle("closed");
    app.classList.toggle("sidebar-closed", sidebar.classList.contains("closed"));
  }

  function updateAgentIndicatorState(agent, stateName) {
    const button = $(`.agent-btn[data-agent="${agent}"]`);
    if (!button) return;
    button.classList.remove("status-ready", "status-alert", "status-working");
    if (stateName === "ready") {
      button.classList.add("status-ready");
    } else if (stateName === "alert") {
      button.classList.add("status-alert");
    } else if (stateName === "working") {
      button.classList.add("status-working");
    } else {
      button.classList.add("status-ready");
    }
  }

  function setActiveAgentButton(agent) {
    $$(".agent-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.agent === agent));
  }

  async function switchAgent(agent) {
    if (!agent) return;
    state.selectedAgent = agent;
    setActiveAgentButton(agent);
    updateAgentIndicatorState(agent, "working");

    setTimeout(() => {
      if (state.selectedAgent === agent) {
        updateAgentIndicatorState(agent, "ready");
      }
    }, 500);
  }

  // ════════════════════════════════════════════════════
  // 10. boot()
  // ════════════════════════════════════════════════════════════
  async function boot() {
    // Settings
    settingsForm.addEventListener("submit", saveSettings);
    modal.addEventListener("click", (e) => { if (e.target.matches("[data-close]")) closeSettings(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.classList.contains("hidden")) closeSettings();
    });

    // Composer
    const form = $("#chat-form");
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const t = chatInput.value.trim();
      if (!t) return;
      chatInput.value = "";
      sendMessage(t, state.selectedAgent);
    });
    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });

    // Agent buttons
    $$(".agent-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const agent = button.dataset.agent;
        if (!agent) return;
        void switchAgent(agent);
      });
    });
    await refreshActiveAgent();

    // Sidebar toggle
    $("#btn-sidebar-toggle").addEventListener("click", toggleSidebar);

    // Workspace tree
    $("#btn-refresh-tree").addEventListener("click", loadWorkspaceTree);
    loadWorkspaceTree();
    state.sse.ws = connectSSE(ENDPOINTS.wsStream, applyWorkspaceChange, {
      onStatus: setWsStatus,
      name: "workspace",
    });

    // Live diagnostics disabled in simplified UI

    // Topbar status disabled in simplified UI
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

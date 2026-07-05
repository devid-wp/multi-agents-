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
      diagnostics: null,
    },
    selectedAgent: "planner",
    activeAgent: null,
    toolState: {
      current: null,
      status: "idle",
      detail: "Waiting for the next tool action…",
    },
    hardware: {
      connected: false,
      detail: "Awaiting ESP32 connection…",
    },
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
  const STORAGE_KEY = "trinity_settings";

  const modal        = $("#settings-modal");
  const settingsForm = $("#settings-form");
  const settingsStat = $("#settings-status");

  // ── localStorage helpers ────────────────────────────────────────
  function storageLoad() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch { return {}; }
  }
  function storageSave(data) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch {}
  }

  // ── Provider tab switching ─────────────────────────────────────
  const PROVIDER_PANELS = { ollama: "#panel-ollama", nvidia: "#panel-nvidia", custom: "#panel-nvidia" };

  function setActiveProvider(p) {
    $$("[data-provider]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.provider === p);
    });
    // Show/hide panels
    Object.values(PROVIDER_PANELS).forEach((sel) => {
      const el = $(sel);
      if (el) el.classList.add("hidden");
    });
    const target = $(PROVIDER_PANELS[p] || "#panel-ollama");
    if (target) target.classList.remove("hidden");
    const inp = $("#llm-provider-input");
    if (inp) inp.value = p;
  }

  // ── Modal open / close ─────────────────────────────────────────
  function openSettings() {
    modal.classList.remove("hidden");
    modal.classList.add("modal-enter");
    requestAnimationFrame(() => modal.classList.remove("modal-enter"));
    populateForm();
  }
  function closeSettings() {
    modal.classList.add("hidden");
    if (settingsStat) { settingsStat.textContent = ""; settingsStat.className = "settings-status"; }
  }

  // ── Populate form from localStorage ─────────────────────────────
  function populateForm() {
    const s = storageLoad();
    const provider = s.llm_provider || "ollama";
    setActiveProvider(provider);

    const f = settingsForm;
    if (!f) return;
    // Ollama panel
    if (f.ollama_url)     f.ollama_url.value     = s.ollama_url     || "";
    if (f.executor_model) f.executor_model.value = s.executor_model || "";
    // NVIDIA / Custom panel
    if (f.planner_api_key) f.planner_api_key.value = ""; // never pre-fill password
    if (f.planner_base_url) f.planner_base_url.value = s.planner_base_url || "";
    if (f.planner_model)    f.planner_model.value    = s.planner_model    || "";
    if (f.critic_api_key)   f.critic_api_key.value   = "";
    if (f.critic_base_url)  f.critic_base_url.value  = s.critic_base_url  || "";
    if (f.critic_model)     f.critic_model.value     = s.critic_model     || "";
  }

  // ── Save to localStorage + sync to backend cookie session ─────────
  async function saveSettings(e) {
    e.preventDefault();
    const fd = new FormData(settingsForm);
    const payload = {};
    for (const [k, v] of fd.entries()) payload[k] = v === "" ? null : v;

    // Merge with existing stored creds: empty key fields => keep old value
    const existing = storageLoad();
    const merged = {
      llm_provider: payload.llm_provider || existing.llm_provider || "ollama",
      ollama_url:     payload.ollama_url     || existing.ollama_url     || "",
      executor_model: payload.executor_model || existing.executor_model || "",
      planner_base_url: payload.planner_base_url || existing.planner_base_url || "",
      planner_model:    payload.planner_model    || existing.planner_model    || "",
      critic_base_url:  payload.critic_base_url  || existing.critic_base_url  || "",
      critic_model:     payload.critic_model     || existing.critic_model     || "",
      // Keys: only overwrite when user actually typed something
      ...(payload.planner_api_key ? { planner_api_key: payload.planner_api_key } : { planner_api_key: existing.planner_api_key || "" }),
      ...(payload.critic_api_key  ? { critic_api_key:  payload.critic_api_key  } : { critic_api_key:  existing.critic_api_key  || "" }),
    };
    storageSave(merged);

    // Also sync to server cookie session (non-blocking)
    _syncToServer(merged);

    showStatus("✓ Saved", "ok");
    setTimeout(closeSettings, 700);
  }

  async function _syncToServer(data) {
    try {
      await fetch(ENDPOINTS.settingsSet, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
    } catch (err) {
      console.warn("[settings] server sync failed (non-critical):", err);
    }
  }

  function showStatus(text, kind = "ok") {
    if (!settingsStat) return;
    settingsStat.textContent = text;
    settingsStat.className = `settings-status ${kind}`;
  }

  // ── Toggle API-key visibility ──────────────────────────────────
  function initEyeBtn() {
    const btn = $("#toggle-key-visibility");
    const inp = $("#api-key-input");
    if (!btn || !inp) return;
    btn.addEventListener("click", () => {
      const isPass = inp.type === "password";
      inp.type = isPass ? "text" : "password";
      btn.setAttribute("aria-pressed", String(isPass));
    });
  }
  // ════════════════════════════════════════════════════════════
  // 6. Live log stream and command bridge
  // ════════════════════════════════════════════════════════════
  const chatContainerEl = $("#chat-container");
  const chatInput       = $("#chat-input");
  const sendBtn         = $("#send-btn");
  const diagnosticsListEl = $("#diagnostics-list");
  const diagnosticsStatusEl = $("#diagnostics-status");
  const toolStatusEl = $("#tool-status");
  const toolStatusContentEl = $("#tool-status-content");
  const hardwareLinkStatusEl = $("#hardware-link-status");
  const hardwareLinkPillEl = $("#hardware-link-pill");
  const hardwareLinkDetailEl = $("#hardware-link-detail");

  function setRunningUI(running) {
    state.running = running;
    sendBtn.disabled = running;
    chatInput.disabled = running;
  }

  function updateToolLifecycleState(status, detail, toolName = null) {
    state.toolState.status = status;
    state.toolState.detail = detail;
    state.toolState.current = toolName;
    const label = toolName ? `Tool lifecycle: ${status} · ${toolName}` : `Tool lifecycle: ${status}`;
    if (toolStatusEl) {
      toolStatusEl.textContent = label;
      toolStatusEl.className = `tool-status-pill ${status}`;
    }
    if (toolStatusContentEl) {
      toolStatusContentEl.textContent = detail;
    }
  }

  function updateHardwareLinkState(connected, detail) {
    state.hardware.connected = connected;
    state.hardware.detail = detail;
    const activeClass = connected ? "online" : "offline";
    [hardwareLinkStatusEl, hardwareLinkPillEl].forEach((el) => {
      if (!el) return;
      el.className = `status-pill ${activeClass}`;
      el.textContent = connected ? "online" : "offline";
    });
    if (hardwareLinkDetailEl) {
      hardwareLinkDetailEl.textContent = detail;
    }
  }

  function addDiagnosticEntry(ev) {
    if (!diagnosticsListEl) return;
    const row = document.createElement("div");
    row.className = "diag-entry";
    const kind = ev.kind || "info";
    const title = kind === "tool_execution" ? "tool execution" : kind;
    const message = (ev.content || ev.tool || ev.result || ev.args) ? safeJson(ev) : "—";
    row.innerHTML = `<div class="diag-title">${escapeHtml(title)}</div><div class="diag-body">${escapeHtml(truncate(message, 300))}</div>`;
    diagnosticsListEl.prepend(row);
    while (diagnosticsListEl.children.length > 24) {
      diagnosticsListEl.removeChild(diagnosticsListEl.lastChild);
    }
  }

  function renderBridgeEvent(ev) {
    state.bridge.push(ev);
    if (!chatContainerEl) return;

    const card = document.createElement("div");
    card.className = `card chat-card ${cardClassForEvent(ev)}`;
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
      chatContainerEl.innerHTML = "<div class=\"log-header\">Agent stream</div>";
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
  function buildEphemeralCreds() {
    /** Read stored credentials and build ephemeral_credentials payload. */
    const s = storageLoad();
    if (!s || !s.llm_provider) return null;
    return {
      llm_provider:   s.llm_provider   || null,
      ollama_url:     s.ollama_url     || null,
      executor_model: s.executor_model || null,
      planner_api_key: s.planner_api_key || null,
      planner_base_url: s.planner_base_url || null,
      planner_model:   s.planner_model   || null,
      critic_api_key:  s.critic_api_key  || null,
      critic_base_url: s.critic_base_url || null,
      critic_model:    s.critic_model    || null,
    };
  }

  async function sendMessage(text, action = null) {
    if (state.running) return;
    if (!text.trim()) return;

    setRunningUI(true);

    // user message bubble
    renderBridgeEvent({ kind: "user", agent: "user", content: text, timestamp: Date.now() / 1000 });

    const payload = { message: text };
    const creds = buildEphemeralCreds();
    if (creds) payload.ephemeral_credentials = creds;

    const stop = await postSSE(ENDPOINTS.chat, payload, (ev) => {
      if (!ev || !ev.kind) return;
      ev.timestamp = ev.timestamp || Date.now() / 1000;
      renderBridgeEvent(ev);

      if (ev.kind === "tool_call" && ev.tool) {
        updateToolLifecycleState("executing", `Calling ${ev.tool.name}`, ev.tool.name);
        addDiagnosticEntry(ev);
      }
      if (ev.kind === "tool_result" && ev.result) {
        const outcome = ev.result.success ? "complete" : "failed";
        updateToolLifecycleState(outcome, ev.result.output || ev.result.error || "Tool finished", ev.result.name || null);
        addDiagnosticEntry(ev);
      }
      if (ev.kind === "tool_execution") {
        addDiagnosticEntry(ev);
      }
      if (ev.kind === "agent_start" && ev.agent) {
        updateAgentIndicatorState(ev.agent, "working");
        state.activeAgent = ev.agent;
      }

      if (ev.kind === "agent_done" && ev.agent) {
        updateAgentIndicatorState(ev.agent, "ready");
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

  // ════════════════════════════════════════════════════
  // 10. boot()
  // ════════════════════════════════════════════════════════════
  async function boot() {
    // Settings
    settingsForm.addEventListener("submit", saveSettings);
    modal.addEventListener("click", (e) => { if (e.target.closest("[data-close]") || e.target === modal) closeSettings(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.classList.contains("hidden")) closeSettings();
    });
    // Gear button
    const btnSettings = $("#btn-settings");
    if (btnSettings) btnSettings.addEventListener("click", openSettings);
    // Provider tabs
    $$("[data-provider]").forEach((btn) => {
      btn.addEventListener("click", () => setActiveProvider(btn.dataset.provider));
    });
    // Eye button (show/hide API key)
    initEyeBtn();

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

    // Diagnostics stream
    if (state.sse.diagnostics) {
      state.sse.diagnostics.close();
    }
    state.sse.diagnostics = connectSSE("/api/diagnostics/stream", (ev) => {
      if (!ev || !ev.kind) return;
      if (ev.kind === "error") {
        diagnosticsStatusEl.className = "status-pill offline";
        diagnosticsStatusEl.textContent = "offline";
      } else {
        diagnosticsStatusEl.className = "status-pill live";
        diagnosticsStatusEl.textContent = "live";
      }
      addDiagnosticEntry(ev);
    }, {
      onStatus: (status) => {
        diagnosticsStatusEl.className = `status-pill ${status === "open" ? "live" : status === "reconnecting" ? "reconnecting" : "offline"}`;
        diagnosticsStatusEl.textContent = status === "open" ? "live" : status === "reconnecting" ? "reconnecting" : "offline";
      },
      name: "diagnostics",
    });

    updateHardwareLinkState(false, "Awaiting ESP32 connection…");
    updateToolLifecycleState("idle", "Waiting for the next tool action…", null);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

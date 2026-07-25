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
    chatHistory:   "/api/chat/history",
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
  // Генерируем или восстанавливаем session_id из sessionStorage.
  // sessionStorage сбрасывается при закрытии вкладки, но переживает F5.
  function getOrCreateSessionId() {
    let id = sessionStorage.getItem("trinity_session_id");
    if (!id) {
      id = "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
      sessionStorage.setItem("trinity_session_id", id);
    }
    return id;
  }
  const SESSION_ID = getOrCreateSessionId();

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
    selectedStrategy: "planner",  // matches first active agent-btn
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

  // ── Dynamic API Keys ───────────────────────────────────────────
  document.addEventListener('input', (e) => {
    if (e.target.classList.contains('api-key-input')) {
      const container = e.target.closest('.api-keys-container');
      if (!container) return;
      
      const inputs = Array.from(container.querySelectorAll('.api-key-input'));
      const lastInput = inputs[inputs.length - 1];
      
      if (lastInput.value.trim() !== '') {
        const newInput = lastInput.cloneNode();
        newInput.value = '';
        newInput.classList.add('new-input-anim');
        container.appendChild(newInput);
      }
      
      for (let i = inputs.length - 2; i >= 0; i--) {
          if (inputs[i].value.trim() === '') {
              inputs[i].remove();
          }
      }
    }
  });

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
  // Event listeners for radio toggles
  document.querySelectorAll('input[type="radio"][name$="_type"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
      const agent = e.target.name.split('_')[0]; // "planner", "executor", "critic"
      const type = e.target.value; // "base" or "custom"
      
      const block = document.getElementById(`config-${agent}`);
      if (!block) return;
      
      const basePanel = block.querySelector('.base-panel');
      const customPanel = block.querySelector('.custom-panel');
      
      if (type === 'base') {
        basePanel.classList.remove('hidden');
        customPanel.classList.add('hidden');
      } else {
        basePanel.classList.add('hidden');
        customPanel.classList.remove('hidden');
      }
    });
  });

  // Event listeners for custom provider dropdowns to hide API key for Ollama
  // and auto-fill Base URL default value
  const DEFAULT_OLLAMA_URL = 'http://localhost:11434';
  const DEFAULT_NVIDIA_URL = 'https://integrate.api.nvidia.com/v1';

  function applyProviderVisibility(sel) {
    const agentName = sel.name.split('_')[0];
    const isOllama = sel.value === 'ollama';
    const isGoogle = sel.value === 'google';

    // Show/hide API Key field
    const apiKeyInput = document.querySelector(`input[name="${agentName}_custom_api_key"]`);
    if (apiKeyInput) {
      const field = apiKeyInput.closest('.settings-field');
      if (field) field.style.display = isOllama ? 'none' : '';
    }

    // Auto-fill Base URL or hide it for Google
    const baseUrlInput = document.querySelector(`input[name="${agentName}_base_url"]`);
    if (baseUrlInput) {
      const field = baseUrlInput.closest('.settings-field');
      if (field) field.style.display = isGoogle ? 'none' : '';
      
      if (!isGoogle) {
        const current = baseUrlInput.value.trim();
        const isDefaultLike = !current || current === DEFAULT_OLLAMA_URL || current === DEFAULT_NVIDIA_URL;
        if (isDefaultLike) {
          baseUrlInput.value = isOllama ? DEFAULT_OLLAMA_URL : DEFAULT_NVIDIA_URL;
        }
      }
    }
    
    // Auto-fill Model Name for Google
    const modelInput = document.querySelector(`input[name="${agentName}_model_name"]`);
    if (modelInput && isGoogle) {
      if (!modelInput.value.trim()) modelInput.value = 'gemini-1.5-pro';
    }
  }

  document.querySelectorAll('select[name$="_custom_provider"]').forEach(select => {
    applyProviderVisibility(select);
    select.addEventListener('change', (e) => applyProviderVisibility(e.target));
  });

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
    const f = settingsForm;
    if (!f) return;
    
    const pop = (agentName) => {
      const cfg = s[agentName] || {};
      // "nvidia" and "ollama" fall under Custom. Standard ones fall under Base.
      const isCustom = ["nvidia", "ollama"].includes(cfg.provider);
      const radioName = `${agentName}_type`;
      
      if (f.elements[radioName]) {
        // Trigger UI toggle
        const radios = Array.from(f.elements[radioName]);
        const targetRadio = radios.find(r => r.value === (isCustom ? "custom" : "base"));
        if (targetRadio) {
          targetRadio.checked = true;
          targetRadio.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
      
      if (isCustom) {
        const providerSel = f.elements[`${agentName}_custom_provider`];
        if (providerSel) {
          providerSel.value = cfg.provider || "nvidia";
          // Re-trigger visibility so API key is hidden for Ollama right away
          applyProviderVisibility(providerSel);
        }
        if (f.elements[`${agentName}_base_url`]) f.elements[`${agentName}_base_url`].value = cfg.base_url || "";
        if (f.elements[`${agentName}_model_name`]) f.elements[`${agentName}_model_name`].value = cfg.model_name || "";
      } else {
        if (f.elements[`${agentName}_base_provider`]) f.elements[`${agentName}_base_provider`].value = cfg.provider || "gpt";
      }
      
      // Reset password fields to a single empty input on open
      const resetKeys = (name) => {
        const inputs = Array.from(document.querySelectorAll(`input[name="${name}"]`));
        if (inputs.length > 0) {
            const container = inputs[0].closest('.api-keys-container');
            if (container) {
                container.innerHTML = '';
                const baseInput = inputs[0].cloneNode();
                baseInput.value = '';
                baseInput.classList.remove('new-input-anim');
                container.appendChild(baseInput);
            }
        }
      };
      resetKeys(`${agentName}_base_api_key`);
      resetKeys(`${agentName}_custom_api_key`);
    };
    
    pop("planner");
    pop("executor");
    pop("critic");
  }

  // ── Save to localStorage + sync to backend cookie session ─────────
  async function saveSettings(e) {
    e.preventDefault();
    const fd = new FormData(settingsForm);
    const existing = storageLoad();
    
    const getAgentPayload = (agentName) => {
        const type = fd.get(`${agentName}_type`);
        const oldCfg = existing[agentName] || {};
        
        const baseKeys = fd.getAll(`${agentName}_base_api_key`).map(k => k.trim()).filter(Boolean);
        const customKeys = fd.getAll(`${agentName}_custom_api_key`).map(k => k.trim()).filter(Boolean);
        
        if (type === "base") {
            const provider = fd.get(`${agentName}_base_provider`);
            const api_key = baseKeys.length > 0 ? baseKeys.join(',') : (oldCfg.api_key || null);
            return { provider, api_key, base_url: null, model_name: null };
        } else {
            const provider = fd.get(`${agentName}_custom_provider`);
            // Ollama is local — it never needs an API key
            const api_key = provider === 'ollama'
                ? null
                : (customKeys.length > 0 ? customKeys.join(',') : (oldCfg.api_key || null));
            const base_url = provider === 'google' ? null : (fd.get(`${agentName}_base_url`) || null);
            const model_name = fd.get(`${agentName}_model_name`) || null;
            return { provider, api_key, base_url, model_name };
        }
    };
    
    const payload = {
        planner: getAgentPayload("planner"),
        executor: getAgentPayload("executor"),
        critic: getAgentPayload("critic")
    };
    
    storageSave(payload);
    _syncToServer(payload);

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
  const stopBtn         = $("#stop-btn");
  const skeletonEl      = $("#executor-skeleton");
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
    // Toggle send ↔ stop button
    if (running) {
      sendBtn.classList.add("hidden");
      stopBtn.classList.remove("hidden");
    } else {
      stopBtn.classList.add("hidden");
      sendBtn.classList.remove("hidden");
      hideSkeleton();
    }
  }

  function showSkeleton() {
    if (skeletonEl) skeletonEl.classList.remove("hidden");
  }
  function hideSkeleton() {
    if (skeletonEl) skeletonEl.classList.add("hidden");
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

  function cardClassForEvent(ev) {
    const k = ev.kind || "info";
    const a = ev.agent || "manager";
    let base = "";
    if (k === "error")       base = "error";
    else if (k === "final")  base = "final";
    else if (k === "info")   base = "info";
    else if (k === "agent_message" || k === "agent_start" || k === "agent_done") base = a;
    else if (k === "tool_call")   base = "tool_call";
    else if (k === "tool_result") base = "tool_result" + (ev.result && ev.result.success ? "" : " failed");
    else base = "info";
    
    // Always attach the agent class for filtering
    return `${base} agent-${a}`;
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
      let argsHtml = `<pre>${escapeHtml(safeJson(ev.tool.arguments))}</pre>`;
      
      if (ev.tool.name === "replace_in_file" && ev.tool.arguments) {
        const args = ev.tool.arguments;
        const target = typeof args.target_content === "string" ? args.target_content : "";
        const repl = typeof args.replacement_content === "string" ? args.replacement_content : "";
        argsHtml = `
          <div class="diff-view">
            <div class="diff-header">Replacing in: ${escapeHtml(args.path || "unknown")}</div>
            <pre class="diff-target">- ${escapeHtml(target).replace(/\n/g, "\n- ")}</pre>
            <pre class="diff-replace">+ ${escapeHtml(repl).replace(/\n/g, "\n+ ")}</pre>
          </div>
        `;
      } else if (ev.tool.name === "write_file" && ev.tool.arguments) {
        const args = ev.tool.arguments;
        const content = typeof args.content === "string" ? args.content : "";
        argsHtml = `
          <div class="diff-view">
            <div class="diff-header">Writing to: ${escapeHtml(args.path || "unknown")}</div>
            <pre class="diff-replace">+ ${escapeHtml(content).replace(/\n/g, "\n+ ")}</pre>
          </div>
        `;
      }

      return `${meta}
        <div class="body">
          <span class="tool-name">→ ${escapeHtml(ev.tool.name)}</span>
          ${ev.tool.id ? `<span class="muted"> · ${escapeHtml(ev.tool.id)}</span>` : ""}
          ${argsHtml}
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

    const payload = { message: text, strategy: state.selectedStrategy || "auto", session_id: SESSION_ID };
    const creds = buildEphemeralCreds();
    if (creds) payload.ephemeral_credentials = creds;

    const stop = await postSSE(ENDPOINTS.chat, payload, (ev) => {
      if (!ev || !ev.kind) return;
      ev.timestamp = ev.timestamp || Date.now() / 1000;
      renderBridgeEvent(ev);

      // Show executor skeleton while executor is working
      if (ev.kind === "agent_start" && ev.agent === "executor") showSkeleton();
      if ((ev.kind === "agent_done" && ev.agent === "executor") ||
           ev.kind === "final" || ev.kind === "error") hideSkeleton();

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
      if (ev.kind === "strategy") {
        // Сервер подтвердил выбранную стратегию
        setActiveStrategy(ev.content);
        return;
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

  // ── Strategy routing ────────────────────────────────────────────
  const STRATEGY_META = {
    planner: { icon: "\uD83E\uDDE0", label: "planner",  color: "#a78bfa" },
    auto:    { icon: "\u26A1",       label: "auto",     color: "#34d399" },
    direct:  { icon: "\u2699\uFE0F", label: "direct",   color: "#f97316" },
  };

  function setActiveStrategy(strategy) {
    state.selectedStrategy = strategy || "auto";
    const meta = STRATEGY_META[state.selectedStrategy] || STRATEGY_META.auto;
    const icon  = document.getElementById("strategy-badge-icon");
    const label = document.getElementById("strategy-badge-label");
    const badge = document.getElementById("strategy-badge");
    if (icon)  icon.textContent  = meta.icon;
    if (label) label.textContent = meta.label;
    if (badge) badge.style.setProperty("--strategy-color", meta.color);
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

    // Stop button
    if (stopBtn) stopBtn.addEventListener("click", stopChat);
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

    // Agent buttons — switch strategy + highlight
    $$(".agent-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const agent    = button.dataset.agent;
        const strategy = button.dataset.strategy || "auto";
        if (!agent) return;
        setActiveAgentButton(agent);
        setActiveStrategy(strategy);
        state.selectedAgent = agent;
        
        // Room filtering
        const chatContainer = $("#chat-container");
        if (chatContainer) {
          chatContainer.dataset.room = agent;
        }

        // Also call switchAgent to keep backend ACTIVE_AGENT in sync
        if (agent !== "all") {
          void switchAgent(agent);
        }
      });
    });
    // Init badge and room from default active button
    const defaultBtn = $(".agent-btn.active");
    if (defaultBtn) {
      setActiveStrategy(defaultBtn.dataset.strategy || "auto");
      const chatContainer = $("#chat-container");
      if (chatContainer) chatContainer.dataset.room = defaultBtn.dataset.agent;
    }
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

    // Загружаем сохранённую историю диалога для текущей сессии (F5-устойчивость)
    await loadSessionHistory();
  }

  async function loadSessionHistory() {
    try {
      const resp = await fetch(`${ENDPOINTS.chatHistory}?session_id=${encodeURIComponent(SESSION_ID)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.ok || !data.messages || data.messages.length === 0) return;

      // Отображаем панель восстановления
      const historyBanner = document.createElement("div");
      historyBanner.className = "card chat-card info";
      historyBanner.innerHTML = `<div class="meta"><span class="agent-tag system">system</span></div><div class="body">&#9679; Сессия восстановлена (${data.messages.length} сообщений из предыдущей сессии)</div>`;
      if (chatContainerEl) chatContainerEl.appendChild(historyBanner);
    } catch (e) {
      console.warn("loadSessionHistory failed:", e);
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

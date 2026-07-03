/* Trinity — ChatGPT-style multi-session client */

(function () {
    "use strict";

    // ─────────────────────────────────────────────
    // State (in-memory + localStorage)
    // ─────────────────────────────────────────────
    const STORAGE_KEY = "trinity.sessions.v1";

    const state = {
        sessions: [],          // [{id, title, messages, createdAt, running}]
        activeSessionId: null,
    };

    // sessionId → AbortController (для параллельных SSE)
    const streams = new Map();

    // ─────────────────────────────────────────────
    // DOM
    // ─────────────────────────────────────────────
    const $ = (sel) => document.querySelector(sel);
    const chatListEl   = $("#chat-list");
    const messagesEl   = $("#messages");
    const chatForm     = $("#chat-form");
    const chatInput    = $("#chat-input");
    const sendBtn      = $("#send-btn");
    const stopBtn      = $("#stop-btn");
    const chatTitleEl  = $("#chat-title");
    const chatStatusEl = $("#chat-status");
    const newChatBtn   = $("#new-chat-btn");
    const settingsBtn  = $("#settings-btn");

    const modal         = $("#settings-modal");
    const settingsForm  = $("#settings-form");
    const settingsStatus= $("#settings-status");
    const plannerStatus = $("#planner-key-status");
    const criticStatus  = $("#critic-key-status");

    // ─────────────────────────────────────────────
    // Utils
    // ─────────────────────────────────────────────
    const uid = () => "s_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

    function escapeHtml(s) {
        return String(s ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function nl2br(s) {
        return escapeHtml(s).replace(/\n/g, "<br>");
    }

    function agentEmoji(agent) {
        return ({
            planner: "🧠",
            critic: "🔍",
            executor: "⚙️",
            manager: "🎯",
        }[agent] || "💬");
    }

    function formatTime(ts) {
        const d = new Date(ts);
        return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    }

    // ─────────────────────────────────────────────
    // Persistence
    // ─────────────────────────────────────────────
    function persist() {
        try {
            const slim = state.sessions.map((s) => ({
                id: s.id,
                title: s.title,
                createdAt: s.createdAt,
                messages: s.messages,
            }));
            localStorage.setItem(STORAGE_KEY, JSON.stringify(slim));
        } catch (e) {
            console.warn("persist failed", e);
        }
    }

    function load() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) return false;
            const data = JSON.parse(raw);
            if (!Array.isArray(data)) return false;
            state.sessions = data.map((s) => ({
                ...s,
                running: false, // running-флаги не персистим
                messages: Array.isArray(s.messages) ? s.messages : [],
            }));
            if (state.sessions.length && !state.activeSessionId) {
                state.activeSessionId = state.sessions[0].id;
            }
            return state.sessions.length > 0;
        } catch (e) {
            console.warn("load failed", e);
            return false;
        }
    }

    // ─────────────────────────────────────────────
    // Sessions
    // ─────────────────────────────────────────────
    function findSession(id) {
        return state.sessions.find((s) => s.id === id) || null;
    }

    function getActive() {
        return findSession(state.activeSessionId);
    }

    function createSession() {
        const s = {
            id: uid(),
            title: "Новый чат",
            createdAt: Date.now(),
            running: false,
            messages: [],
        };
        state.sessions.unshift(s);
        state.activeSessionId = s.id;
        persist();
        renderSidebar();
        renderActive();
        return s;
    }

    function selectSession(id) {
        if (state.activeSessionId === id) return;
        state.activeSessionId = id;
        persist();
        renderSidebar();
        renderActive();
    }

    function deleteSession(id) {
        // остановить стрим, если идёт
        const ctrl = streams.get(id);
        if (ctrl) { ctrl.abort(); streams.delete(id); }
        const idx = state.sessions.findIndex((s) => s.id === id);
        if (idx < 0) return;
        state.sessions.splice(idx, 1);
        if (state.activeSessionId === id) {
            state.activeSessionId = state.sessions[0]?.id || null;
            if (!state.activeSessionId) createSession();
        }
        persist();
        renderSidebar();
        renderActive();
    }

    function autoTitle(session, firstUserText) {
        if (session.title !== "Новый чат") return;
        const t = firstUserText.trim().slice(0, 40);
        session.title = t || "Новый чат";
    }

    // ─────────────────────────────────────────────
    // Render
    // ─────────────────────────────────────────────
    function renderSidebar() {
        chatListEl.innerHTML = "";
        if (!state.sessions.length) {
            const empty = document.createElement("div");
            empty.className = "muted";
            empty.style.padding = "0.5rem 0.75rem";
            empty.style.fontSize = "0.8rem";
            empty.textContent = "Нет чатов. Нажми «＋ Новый чат».";
            chatListEl.appendChild(empty);
            return;
        }
        for (const s of state.sessions) {
            const item = document.createElement("div");
            item.className = "chat-item" + (s.id === state.activeSessionId ? " active" : "");
            item.dataset.id = s.id;

            const dot = document.createElement("span");
            dot.className = "dot" + (s.running ? "" : " idle");
            item.appendChild(dot);

            const title = document.createElement("span");
            title.className = "title";
            title.textContent = s.title;
            item.appendChild(title);

            const del = document.createElement("button");
            del.className = "delete";
            del.type = "button";
            del.title = "Удалить чат";
            del.textContent = "×";
            del.addEventListener("click", (e) => {
                e.stopPropagation();
                if (confirm(`Удалить «${s.title}»?`)) deleteSession(s.id);
            });
            item.appendChild(del);

            item.addEventListener("click", () => selectSession(s.id));
            chatListEl.appendChild(item);
        }
    }

    function bubbleHTMLForEvent(ev) {
        // Возвращает HTML-строку одного сообщения
        const kind = ev.kind || "info";
        const agent = ev.agent || "manager";

        // user-сообщения (мы их пушим как kind=info c content=prefix «🧑‍💻 Вы:»)
        // Чтобы было проще — отдельный kind "user" будем использовать только из локального addUserMessage
        if (kind === "user") {
            return `<div class="msg-row msg-row--user">
                <div class="bubble bubble--user">${nl2br(ev.content)}</div>
            </div>`;
        }

        // agent messages — карточки
        if (kind === "agent_message" || kind === "agent_start" || kind === "agent_done") {
            const tag = `<span class="agent-tag">${agentEmoji(agent)} ${(agent || "").toUpperCase()}</span>`;
            const cls = `bubble bubble--agent ${agent || "manager"}`;
            let inner = tag;
            if (ev.content) inner += `<span>${nl2br(ev.content)}</span>`;
            if (ev.tool) {
                inner += `<span class="tool">→ ${escapeHtml(ev.tool.name)}(${escapeHtml(JSON.stringify(ev.tool.arguments))})</span>`;
            }
            return `<div class="msg-row msg-row--assistant">
                <div class="${cls}">${inner}</div>
            </div>`;
        }

        if (kind === "tool_call") {
            const cls = `bubble bubble--agent ${agent || "manager"}`;
            const tag = `<span class="agent-tag">${agentEmoji(agent)} ${(agent || "").toUpperCase()}</span>`;
            return `<div class="msg-row msg-row--assistant">
                <div class="${cls}">${tag}<span class="muted">вызывает инструмент</span>
                    <span class="tool">→ ${escapeHtml(ev.tool.name)}(${escapeHtml(JSON.stringify(ev.tool.arguments))})</span>
                </div>
            </div>`;
        }

        if (kind === "tool_result") {
            const cls = `bubble bubble--agent ${agent || "manager"}`;
            const tag = `<span class="agent-tag">${agentEmoji(agent)} ${(agent || "").toUpperCase()}</span>`;
            const r = ev.result || {};
            const rcls = "tool-result" + (r.success ? "" : " failed");
            const rtxt = (r.success ? "✓ " : "✗ ") + (r.output || r.error || "");
            return `<div class="msg-row msg-row--assistant">
                <div class="${cls}">${tag}<span class="muted">результат</span>
                    <span class="${rcls}">${escapeHtml(rtxt)}</span>
                </div>
            </div>`;
        }

        if (kind === "final") {
            const cls = `bubble bubble--agent bubble--final ${agent || "executor"}`;
            const tag = `<span class="agent-tag">${agentEmoji(agent)} FINAL</span>`;
            return `<div class="msg-row msg-row--assistant">
                <div class="${cls}">${tag}<span>${nl2br(ev.content || "")}</span></div>
            </div>`;
        }

        if (kind === "error") {
            return `<div class="msg-row msg-row--assistant">
                <div class="bubble bubble--error">✗ ${nl2br(ev.content || "")}</div>
            </div>`;
        }

        // info и прочее
        return `<div class="msg-row msg-row--assistant">
            <div class="bubble bubble--info">${nl2br(ev.content || "")}</div>
        </div>`;
    }

    function renderActive() {
        const s = getActive();
        if (!s) {
            messagesEl.innerHTML = "";
            chatTitleEl.textContent = "Trinity";
            chatStatusEl.textContent = "";
            return;
        }
        chatTitleEl.textContent = s.title;
        chatStatusEl.textContent = s.running ? "● выполняется…" : "";

        // перерисовываем все сообщения
        const parts = [];
        for (const m of s.messages) parts.push(bubbleHTMLForEvent(m));
        if (s.running) {
            parts.push(`<div class="msg-row msg-row--assistant">
                <div class="bubble bubble--agent manager">
                    <span class="agent-tag">🎯 MANAGER</span>
                    <span class="typing"><span></span><span></span><span></span></span>
                </div>
            </div>`);
        }
        messagesEl.innerHTML = parts.join("");
        scrollDown();
    }

    function appendToActive(ev) {
        const s = getActive();
        if (!s) return;
        s.messages.push(ev);
        persist();
        // Если активная — обновим DOM in-place (без полного ререндера, чтобы не мигало)
        if (s.id === state.activeSessionId) {
            const tmp = document.createElement("div");
            tmp.innerHTML = bubbleHTMLForEvent(ev);
            while (tmp.firstChild) messagesEl.appendChild(tmp.firstChild);
            // убрать «печатает…» если был
            const typing = messagesEl.querySelector(".typing");
            if (typing && (ev.kind === "final" || ev.kind === "error")) {
                typing.closest(".msg-row")?.remove();
            }
            scrollDown();
        }
    }

    function scrollDown() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // ─────────────────────────────────────────────
    // Chat (SSE) — один стрим на сессию
    // ─────────────────────────────────────────────
    function setRunning(sessionId, running) {
        const s = findSession(sessionId);
        if (!s) return;
        s.running = running;
        renderSidebar();
        if (sessionId === state.activeSessionId) renderActive();
    }

    async function sendMessage(text) {
        const s = getActive();
        if (!s) return;
        if (s.running) return; // уже идёт — не спамим

        // user-сообщение
        const userMsg = { kind: "user", content: text, agent: "user", timestamp: Date.now() };
        s.messages.push(userMsg);
        autoTitle(s, text);
        persist();
        renderSidebar();
        renderActive();

        setRunning(s.id, true);

        const controller = new AbortController();
        streams.set(s.id, controller);

        try {
            const r = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: text }),
                signal: controller.signal,
            });
            if (!r.ok) {
                const txt = await r.text().catch(() => "");
                appendToActive({ kind: "error", content: `HTTP ${r.status}: ${txt}` });
                return;
            }
            await consumeSSE(r, s.id);
        } catch (err) {
            if (err.name !== "AbortError") {
                appendToActive({ kind: "error", content: String(err) });
            }
        } finally {
            streams.delete(s.id);
            setRunning(s.id, false);
            updateSendButton();
        }
    }

    async function consumeSSE(response, sessionId) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf("\n\n")) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith("data:")) continue;
                const json = raw.slice(5).trim();
                if (!json) continue;
                try {
                    const ev = JSON.parse(json);
                    ev.timestamp = ev.timestamp || Date.now();
                    // Если событие пришло в фоновую сессию — нам нужно обновить
                    // именно её messages, а не активную. Сделаем прямую вставку:
                    const target = findSession(sessionId);
                    if (target) {
                        target.messages.push(ev);
                        if (target.id === state.activeSessionId) {
                            // активная — дописываем в DOM
                            const tmp = document.createElement("div");
                            tmp.innerHTML = bubbleHTMLForEvent(ev);
                            while (tmp.firstChild) messagesEl.appendChild(tmp.firstChild);
                            const typing = messagesEl.querySelector(".typing");
                            if (typing && (ev.kind === "final" || ev.kind === "error")) {
                                typing.closest(".msg-row")?.remove();
                            }
                            scrollDown();
                        }
                        persist();
                    }
                } catch (err) {
                    console.warn("bad SSE json", json, err);
                }
            }
        }
    }

    function stopActiveStream() {
        // Останавливаем ВСЕ стримы, чтобы поведение было предсказуемым
        for (const [sid, ctrl] of streams.entries()) {
            ctrl.abort();
            const s = findSession(sid);
            if (s) s.messages.push({ kind: "info", content: "⏹ Остановлено пользователем", timestamp: Date.now() });
        }
        streams.clear();
        // перерисовать активную
        renderActive();
        persist();
    }

    function updateSendButton() {
        const s = getActive();
        sendBtn.disabled = !!(s && s.running);
        stopBtn.disabled = !(s && s.running);
    }

    // ─────────────────────────────────────────────
    // Settings modal
    // ─────────────────────────────────────────────
    function openSettings() {
        modal.classList.remove("hidden");
        if (location.hash !== "#/settings") {
            history.replaceState(null, "", "#/settings");
        }
        loadSettingsIntoForm();
    }
    function closeSettings() {
        modal.classList.add("hidden");
        if (location.hash === "#/settings") {
            history.replaceState(null, "", location.pathname);
        }
    }

    async function loadSettingsIntoForm() {
        try {
            const r = await fetch("/api/settings");
            if (!r.ok) return;
            const s = await r.json();
            settingsForm.planner_base_url.value = s.planner_base_url || "";
            settingsForm.critic_base_url.value  = s.critic_base_url  || "";
            settingsForm.planner_model_url.value = s.planner_model_url || "";
            settingsForm.critic_model_url.value  = s.critic_model_url  || "";
            settingsForm.ollama_url.value       = s.ollama_url       || "";
            settingsForm.planner_model.value    = s.planner_model    || "";
            settingsForm.critic_model.value     = s.critic_model     || "";
            settingsForm.executor_model.value   = s.executor_model   || "";
            // Ключи НЕ приходят, но мы их и не хотим затирать
            updateKeyStatus(plannerStatus, s.has_planner_key, s.planner_key_masked);
            updateKeyStatus(criticStatus,  s.has_critic_key,  s.critic_key_masked);
        } catch (e) {
            console.warn("loadSettings failed", e);
        }
    }

    function updateKeyStatus(el, has, masked) {
        if (has) {
            el.textContent = `задан (${masked})`;
            el.style.color = "var(--executor)";
        } else {
            el.textContent = "не задан";
            el.style.color = "var(--fg-muted)";
        }
    }

    async function saveSettings(e) {
        e.preventDefault();
        const fd = new FormData(settingsForm);
        const payload = {};
        for (const [k, v] of fd.entries()) {
            // пустые ключи и пустые URL → null (чтобы мердж в бэке оставил прежние)
            payload[k] = (v === "" ? null : v);
        }
        settingsStatus.textContent = "Сохраняю…";
        settingsStatus.style.color = "var(--fg-muted)";
        try {
            const r = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!r.ok) {
                const err = await r.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${r.status}`);
            }
            const data = await r.json();
            settingsStatus.textContent = "✓ Сохранено";
            settingsStatus.style.color = "var(--executor)";
            updateKeyStatus(plannerStatus, data.has_planner_key, data.planner_key_masked);
            updateKeyStatus(criticStatus,  data.has_critic_key,  data.critic_key_masked);
            // Очищаем поля ключей (чтобы случайно не пересохранить)
            settingsForm.planner_api_key.value = "";
            settingsForm.critic_api_key.value  = "";
            setTimeout(closeSettings, 500);
        } catch (err) {
            settingsStatus.textContent = "✗ " + err.message;
            settingsStatus.style.color = "var(--error)";
        }
    }

    // ─────────────────────────────────────────────
    // Event wiring
    // ─────────────────────────────────────────────
    newChatBtn.addEventListener("click", createSession);
    settingsBtn.addEventListener("click", openSettings);
    settingsForm.addEventListener("submit", saveSettings);

    modal.addEventListener("click", (e) => {
        if (e.target.matches("[data-close]")) closeSettings();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.classList.contains("hidden")) closeSettings();
    });
    window.addEventListener("hashchange", () => {
        if (location.hash === "#/settings") {
            openSettings();
        } else if (!modal.classList.contains("hidden")) {
            closeSettings();
        }
    });

    chatForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const text = chatInput.value.trim();
        if (!text) return;
        chatInput.value = "";
        autoGrowInput();
        sendMessage(text);
    });

    stopBtn.addEventListener("click", stopActiveStream);

    // Enter — отправка, Shift+Enter — перенос
    chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            chatForm.requestSubmit();
        }
    });

    function autoGrowInput() {
        chatInput.style.height = "auto";
        chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + "px";
    }
    chatInput.addEventListener("input", autoGrowInput);

    // ─────────────────────────────────────────────
    // Init
    // ─────────────────────────────────────────────
    function init() {
        const had = load();
        if (!had) {
            // создаём первый пустой чат с приветствием
            const s = createSession();
            s.messages.push({
                kind: "info",
                content: "👋 Привет! Это Trinity — мульти-агентная система.\nОткрой ⚙ Настройки, чтобы задать API-ключи для Planner и Critic, затем отправь задачу.",
                timestamp: Date.now(),
            });
            persist();
        }
        renderSidebar();
        renderActive();
        autoGrowInput();
        if (location.hash === "#/settings") openSettings();
    }

    init();
})();

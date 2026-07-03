/* Trinity — клиентская логика */

(function () {
    "use strict";

    const $ = (sel) => document.querySelector(sel);
    const messagesEl = $("#messages");
    const chatForm = $("#chat-form");
    const sendBtn = $("#send-btn");
    const stopBtn = $("#stop-btn");
    const settingsForm = $("#settings-form");
    const settingsStatus = $("#settings-status");
    const nvidiaStatus = $("#nvidia-status");

    let activeController = null; // AbortController текущего стрима

    // ─────────────────────────────────────────────
    // Утилиты
    // ─────────────────────────────────────────────
    function scrollDown() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function escapeHtml(s) {
        return String(s ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function agentEmoji(agent) {
        return ({
            planner: "🧠",
            critic: "🔍",
            executor: "⚙️",
            manager: "🎯",
        }[agent] || "💬");
    }

    function addMessage({ kind, agent, content, tool, result }) {
        const div = document.createElement("div");
        div.className = `msg ${agent || "manager"} ${kind || ""}`;

        if (agent && kind !== "info" && kind !== "error") {
            const tag = document.createElement("span");
            tag.className = "agent-tag";
            tag.textContent = `${agentEmoji(agent)} ${agent.toUpperCase()}`;
            div.appendChild(tag);
        }

        if (content) {
            const span = document.createElement("span");
            span.innerHTML = escapeHtml(content).replace(/\n/g, "<br>");
            div.appendChild(span);
        }

        if (tool) {
            const t = document.createElement("span");
            t.className = "tool";
            t.textContent = `→ ${tool.name}(${JSON.stringify(tool.arguments)})`;
            div.appendChild(t);
        }

        if (result) {
            const r = document.createElement("span");
            r.className = "tool-result" + (result.success ? "" : " failed");
            r.textContent =
                (result.success ? "✓ " : "✗ ") +
                (result.output || result.error || "");
            div.appendChild(r);
        }

        messagesEl.appendChild(div);
        scrollDown();
    }

    // ─────────────────────────────────────────────
    // Settings
    // ─────────────────────────────────────────────
    async function loadSettings() {
        try {
            const r = await fetch("/api/settings");
            if (!r.ok) return;
            const s = await r.json();
            if (s.ollama_url) settingsForm.ollama_url.value = s.ollama_url;
            if (s.planner_model) settingsForm.planner_model.value = s.planner_model;
            if (s.critic_model) settingsForm.critic_model.value = s.critic_model;
            if (s.executor_model) settingsForm.executor_model.value = s.executor_model;
            if (s.has_nvidia) {
                nvidiaStatus.textContent = `задан (${s.nvidia_key_masked})`;
                nvidiaStatus.style.color = "var(--executor)";
            } else {
                nvidiaStatus.textContent = "не задан";
                nvidiaStatus.style.color = "var(--fg-muted)";
            }
        } catch (e) {
            console.warn("loadSettings failed", e);
        }
    }

    settingsForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(settingsForm);
        const payload = Object.fromEntries(fd.entries());
        // Пустые строки → null
        for (const k in payload) {
            if (payload[k] === "") payload[k] = null;
        }
        settingsStatus.textContent = "Сохраняю...";
        try {
            const r = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const data = await r.json();
            settingsStatus.textContent = "✓ Сохранено";
            settingsStatus.style.color = "var(--executor)";
            nvidiaStatus.textContent = data.has_nvidia
                ? `задан (${data.nvidia_key_masked})`
                : "не задан";
            nvidiaStatus.style.color = data.has_nvidia
                ? "var(--executor)"
                : "var(--fg-muted)";
        } catch (err) {
            settingsStatus.textContent = "✗ " + err.message;
            settingsStatus.style.color = "var(--error)";
        }
    });

    // ─────────────────────────────────────────────
    // Chat (SSE)
    // ─────────────────────────────────────────────
    chatForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const text = chatForm.message.value.trim();
        if (!text) return;

        // Добавить сообщение пользователя в лог
        addMessage({ kind: "info", content: `🧑‍💻 Вы: ${text}` });
        chatForm.message.value = "";

        // UI
        sendBtn.disabled = true;
        stopBtn.disabled = false;

        activeController = new AbortController();
        try {
            const r = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: text }),
                signal: activeController.signal,
            });
            if (!r.ok) {
                addMessage({ kind: "error", content: `HTTP ${r.status}: ${await r.text()}` });
                return;
            }

            // Читаем SSE-стрим
            const reader = r.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                // Парсим «data: ...\n\n» блоки
                let idx;
                while ((idx = buffer.indexOf("\n\n")) !== -1) {
                    const raw = buffer.slice(0, idx).trim();
                    buffer = buffer.slice(idx + 2);
                    if (raw.startsWith("data:")) {
                        const json = raw.slice(5).trim();
                        if (!json) continue;
                        try {
                            const ev = JSON.parse(json);
                            addMessage(ev);
                        } catch (err) {
                            console.warn("bad SSE json", json);
                        }
                    }
                }
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                addMessage({ kind: "error", content: String(err) });
            }
        } finally {
            sendBtn.disabled = false;
            stopBtn.disabled = true;
            activeController = null;
            scrollDown();
        }
    });

    stopBtn.addEventListener("click", () => {
        if (activeController) {
            activeController.abort();
            addMessage({ kind: "info", content: "⏹ Остановлено пользователем" });
        }
    });

    // ─────────────────────────────────────────────
    // Init
    // ─────────────────────────────────────────────
    loadSettings();
})();

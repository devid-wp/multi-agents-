// Trinity — modules/utils.js (Phase 2)
export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
export function safeJson(obj) { try { return JSON.stringify(obj, null, 2); } catch { return String(obj); } }
export function truncate(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n) + "…" : s; }
export function formatTime(ts) { if (!ts) return ""; const d = new Date(ts * 1000); return d.toLocaleTimeString(undefined, { hour12: false }); }
export function formatMs(ms) { if (ms == null) return ""; if (ms < 1000) return `${ms}ms`; return `${(ms / 1000).toFixed(2)}s`; }
export function agentEmoji(a) { return ({ planner: "🧠", critic: "🔍", executor: "⚙️", manager: "🎯" }[a] || "•"); }
export function debounce(fn, ms) { let t = null; return (...args) => { if (t) clearTimeout(t); t = setTimeout(() => { t = null; fn(...args); }, ms); }; }
export function basename(p) { if (!p) return ""; const i = p.lastIndexOf("/"); return i < 0 ? p : p.slice(i + 1); }
export function dirname(p) { if (!p) return "."; const i = p.lastIndexOf("/"); return i < 0 ? "." : p.slice(0, i) || "."; }
export function formatSize(n) { if (n == null) return ""; if (n < 1024) return `${n}B`; if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`; return `${(n / (1024 * 1024)).toFixed(1)}M`; }
export function cssEscape(s) { if (window.CSS && CSS.escape) return CSS.escape(s); return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c); }

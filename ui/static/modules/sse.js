// Trinity — modules/sse.js (Phase 2)
// Вынесено из ui/static/app.js:231,309 — SSE клиенты

import { SSE_BACKOFF_MS, PING_TIMEOUT_MS } from "./config.js";

export function connectSSE(url, onEvent, { onStatus, name = "sse" } = {}) {
  let attempt = 0, es = null, closed = false, firstFailureAt = null, pingTimer = null;
  function setStatus(s) { if (onStatus) onStatus(s); }
  function armPing() {
    if (pingTimer) clearTimeout(pingTimer);
    pingTimer = setTimeout(() => {
      if (firstFailureAt == null) firstFailureAt = Date.now();
      if (Date.now() - firstFailureAt > PING_TIMEOUT_MS) setStatus("offline");
      else setStatus("reconnecting");
    }, PING_TIMEOUT_MS);
  }
  function scheduleReconnect() {
    if (closed) return;
    const delay = SSE_BACKOFF_MS[Math.min(attempt, SSE_BACKOFF_MS.length - 1)];
    attempt++; armPing(); setStatus("reconnecting"); setTimeout(connect, delay);
  }
  function connect() {
    if (closed) return;
    try { es = new EventSource(url); } catch (err) { console.warn(`[${name}] ctor failed`, err); scheduleReconnect(); return; }
    es.onopen = () => { attempt = 0; firstFailureAt = null; if (pingTimer) { clearTimeout(pingTimer); pingTimer = null; } setStatus("open"); };
    es.onmessage = (msg) => { if (!msg.data) return; try { onEvent(JSON.parse(msg.data)); } catch (err) { console.warn(`[${name}] bad json`, msg.data, err); } };
    es.onerror = () => { try { es.close(); } catch {} es = null; scheduleReconnect(); };
  }
  connect();
  return { close() { closed = true; if (pingTimer) clearTimeout(pingTimer); if (es) { try { es.close(); } catch {} } es = null; } };
}

export async function postSSE(url, body, onEvent, { signal } = {}) {
  // Поддерживает внешний AbortSignal (из app.js state.abortController)
  const controller = new AbortController();
  const sig = controller.signal;
  let onExternalAbort = null;
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else {
      onExternalAbort = () => controller.abort(signal.reason);
      signal.addEventListener("abort", onExternalAbort, { once: true });
    }
  }
  let reader = null;
  const timeoutId = setTimeout(() => { onEvent({ kind: "error", content: "Request timed out after 3 minutes. Try a smaller task." }); try { controller.abort(); } catch {} }, 180_000);
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: sig });
    if (!res.ok) { let t=""; try{ t=await res.text(); }catch{} onEvent({ kind:"error", content:`HTTP ${res.status}: ${t.slice(0,400)}`}); return; }
    if (!res.body) { const t=await res.text().catch(()=>""); onEvent({ kind:"error", content:`No stream: ${t.slice(0,400)}`}); return; }
    reader = res.body.getReader(); const decoder = new TextDecoder("utf-8"); let buffer="";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx; while ((idx = buffer.indexOf("\n\n")) !== -1) { const frame = buffer.slice(0, idx).trim(); buffer = buffer.slice(idx+2); if (!frame.startsWith("data:")) continue; const json=frame.slice(5).trim(); if(!json) continue; try{ onEvent(JSON.parse(json)); }catch(err){ console.warn("[chat] bad frame", json, err);} }
    }
    if (buffer.trim().startsWith("data:")) { const json=buffer.trim().slice(5).trim(); if(json) try{ onEvent(JSON.parse(json)); }catch{}}
  } catch (err) { if (err.name !== "AbortError") onEvent({ kind:"error", content:String(err)}); }
  finally { clearTimeout(timeoutId); if (signal && onExternalAbort) try { signal.removeEventListener("abort", onExternalAbort); } catch {} }
  return { close(){ try{ controller.abort(); }catch{} if(reader) try{ reader.cancel(); }catch{} } };
}

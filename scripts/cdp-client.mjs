// Local CDP connector. Never accesses Teams service APIs or browser tokens.
const PORT = Number(process.env.TEAMMATIC_CDP_PORT || 9457);
if (!Number.isInteger(PORT) || PORT < 1024 || PORT > 65535) throw Error("Invalid TEAMMATIC_CDP_PORT");
const BASE = `http://127.0.0.1:${PORT}`;
// Consumer and work/school Teams. Only the consumer client has been tested live.
const TEAMS_URL = /^https:\/\/(teams\.live\.com|teams\.microsoft\.com|teams\.cloud\.microsoft)\//;
const CHAT_ROWS = `document.querySelectorAll('[role="treeitem"][data-fui-tree-item-value]').length`;

async function open(target) {
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await Promise.race([
    new Promise((ok, err) => { ws.addEventListener("open", ok, {once: true}); ws.addEventListener("error", err, {once: true}); }),
    new Promise((_, err) => setTimeout(() => err(Error("CDP connect timeout")), 3000)),
  ]);
  let seq = 1;
  const pending = new Map();
  ws.addEventListener("message", ev => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    const p = pending.get(d.id); if (!p) return;
    clearTimeout(p.timer); pending.delete(d.id);
    d.error ? p.reject(Error(JSON.stringify(d.error))) : p.resolve(d.result);
  });
  function send(method, params) {
    return new Promise((resolve, reject) => {
      const id = seq++;
      const timer = setTimeout(() => { pending.delete(id); reject(Error("CDP timeout " + method)); }, 18000);
      pending.set(id, {resolve, reject, timer});
      ws.send(JSON.stringify({id, method, params}));
    });
  }
  async function evalJson(expression) {
    const out = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (out.exceptionDetails) {
      // Guard codes thrown by fixed page functions must reach the caller verbatim.
      const detail = out.exceptionDetails.exception?.description?.split("\n")[0]
        || JSON.stringify(out.exceptionDetails).slice(0, 200);
      throw Error("Page error: " + detail);
    }
    return typeof out.result?.value === "string" ? JSON.parse(out.result.value) : out.result?.value;
  }
  return {evalJson, send, close: () => ws.close(), title: target.title, url: target.url};
}

// The main window is found by structure (it shows the chat list), not by a localized title.
export async function connectTeams() {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 2500);
  const list = await (await fetch(BASE + "/json/list", {signal: ctrl.signal})).json();
  clearTimeout(timeout);
  const pages = list.filter(t => t.type === "page" && TEAMS_URL.test(t.url)
    && t.webSocketDebuggerUrl?.startsWith(`ws://127.0.0.1:${PORT}/`));
  const found = [];
  for (const target of pages) {
    const page = await open(target);
    if (await page.evalJson(CHAT_ROWS).catch(() => 0) > 0) found.push(page); else page.close();
  }
  if (found.length !== 1) {
    found.forEach(page => page.close());
    throw Error(`Expected exactly one Teams window showing the chat list, found ${found.length}. Open Chat in Teams.`);
  }
  return found[0];
}

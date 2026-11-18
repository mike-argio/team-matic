// Local-only, read-only IndexedDB extraction (JSON to stdout) for the chats currently loaded in the Teams chat list.
import {connectTeams} from "./cdp-client.mjs";

const browser = await connectTeams();
const run = async (fn, args) => browser.evalJson("(" + fn.toString() + ")(" + JSON.stringify(args) + ")");

function roster() {
  return JSON.stringify({
    title: document.title,
    host: location.host,
    entries: [...document.querySelectorAll('[role=treeitem][data-testid=list-item]')].map(e => ({
      value: e.getAttribute('data-fui-tree-item-value'),
      kind: e.getAttribute('data-item-type'),
      lines: (e.innerText || '').split('\n').map(s => s.trim()).filter(Boolean)
    }))
  });
}
async function dbNames() {
  const entries = await indexedDB.databases();
  return JSON.stringify(entries.map(x => x.name).filter(n => n?.includes(':replychain-manager:')));
}
async function readThread({name, thread}) {
  const req = indexedDB.open(name);
  const db = await new Promise((ok, err) => { req.onsuccess = () => ok(req.result); req.onerror = () => err(req.error); });
  try {
    const messages = new Map();
    let replyChains = 0, nonMessages = 0;
    const range = IDBKeyRange.bound([thread, ''], [thread, '￿']);
    await new Promise((ok, err) => {
      const req = db.transaction('replychains', 'readonly').objectStore('replychains').openCursor(range);
      req.onerror = () => err(req.error);
      req.onsuccess = () => {
        const cursor = req.result; if (!cursor) { ok(); return; }
        replyChains++;
        if (replyChains > 10000) { err(new Error('Too many replychains; partial cache not accepted')); return; }
        for (const m of Object.values(cursor.value?.messageMap || {})) {
          if (!m || m.type !== 'Message' || !m.id) { nonMessages++; continue; }
          const rawHtml = typeof m.content === 'string' ? m.content : '';
          const when = Number(m.originalArrivalTime || m.clientArrivalTime || 0);
          const entry = {
            id: String(m.id), creator: String(m.creator || ''), timeMs: Number.isFinite(when) ? when : 0,
            // The client's own flag; Python cross-checks it before trusting it.
            mine: typeof m.isSentByCurrentUser === 'boolean' ? m.isSentByCurrentUser : null,
            html: rawHtml.slice(0, 100000), messageType: m.messageType || '',
            // File tiles live outside the message body, as a JSON string written by the client.
            files: typeof m.properties?.files === 'string' ? m.properties.files.slice(0, 200000) : '',
            deleted: !!m.deletionInfo, contentTruncated: rawHtml.length > 100000
          };
          const prior = messages.get(entry.id);
          if (!prior || prior.timeMs <= entry.timeMs) messages.set(entry.id, entry);
        }
        cursor.continue();
      };
    });
    return JSON.stringify({replyChains, nonMessages, messages: [...messages.values()].sort((a, b) => a.timeMs - b.timeMs || a.id.localeCompare(b.id))});
  } finally { db.close(); }
}
try {
  const list = await run(roster, {});
  const names = await run(dbNames, {});
  if (names.length !== 1) throw Error(`Expected one local message cache, found ${names.length}. Sign in to a single account.`);
  const chats = list.entries.filter(x => x.value && (x.kind === 'chat' || x.kind === 'muted-chat' || x.kind === 'group-chat'));
  if (!chats.length) throw Error('No chats in the Teams chat list. Open the Chat view and scroll the list once.');
  const snapshots = [];
  for (let i = 0; i < chats.length; i++) {
    const x = chats[i], thread = x.value.split('|').at(-1);
    const data = await run(readThread, {name: names[0], thread});
    snapshots.push({threadId: thread, title: x.lines[0] || thread, kind: x.kind, rosterTime: x.lines[1] || null,
      rosterPreview: x.lines.at(-1) || null, replyChains: data.replyChains,
      nonMessages: data.nonMessages, messages: data.messages});
    // Counts only: contact names do not belong in terminal logs.
    console.error('READ ' + (i + 1) + '/' + chats.length + ': ' + data.messages.length);
  }
  const payload = {schema: 'teammatic-cache-v3', source: 'local_WebView2_IndexedDB_readonly',
    timestamp: new Date().toISOString(), host: list.host, visibleChats: chats.length, chats: snapshots};
  // Printed for the caller, never written to disk here.
  process.stdout.write(JSON.stringify(payload));
} finally { browser.close(); }

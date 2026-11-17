// Fixed read-only compatibility probe for `teammatic.py doctor`. Reports counts and shapes, never content.
import {connectTeams} from "./cdp-client.mjs";

async function probe() {
  const count = selector => document.querySelectorAll(selector).length;
  const names = (await indexedDB.databases()).map(x => x.name || "");
  const caches = names.filter(n => n.includes(":replychain-manager:"));
  const out = {
    host: location.host,
    language: document.documentElement.lang || null,
    chat_rows: count('[role=treeitem][data-testid=list-item]'),
    selected_rows: [...document.querySelectorAll('[role="treeitem"][data-fui-tree-item-value]')].filter(el => {
      if (el.getAttribute("aria-selected") === "true" || el.querySelector('[aria-selected="true"]')) return true;
      try { return JSON.parse(el.getAttribute("data-tabster") || "{}").observed?.names?.includes("LeftRailSelectedItem") === true; }
      catch { return false; }
    }).length,
    editors: count('div[data-tid="ckeditor"][contenteditable="true"]'),
    send_buttons: count('button[data-tid$="MessageCommands-send"]'),
    message_caches: caches.length,
    conversation_caches: names.filter(n => n.includes(":conversation-manager:")).length,
    // Identifiers are masked; only the layout of the name is reported.
    cache_name_shape: caches[0]?.replace(/[0-9a-f]{8}-[0-9a-f-]{27}/giu, "<guid>").replace(/[0-9a-f]{12,}/giu, "<hex>") || null,
    record_fields: null,
  };
  if (caches.length === 1) {
    const req = indexedDB.open(caches[0]);
    const db = await new Promise((ok, fail) => { req.onsuccess = () => ok(req.result); req.onerror = () => fail(req.error); });
    try {
      const sample = await new Promise((ok, fail) => {
        const cursor = db.transaction("replychains", "readonly").objectStore("replychains").openCursor();
        let seen = 0;
        cursor.onerror = () => fail(cursor.error);
        cursor.onsuccess = () => {
          const cur = cursor.result;
          if (!cur || ++seen > 200) { ok(null); return; }
          const message = Object.values(cur.value?.messageMap || {}).find(m => m?.type === "Message" && m.id);
          if (message) ok(message); else cur.continue();
        };
      });
      if (sample) {
        out.record_fields = Object.fromEntries(
          ["id", "creator", "content", "originalArrivalTime", "messageType", "isSentByCurrentUser"]
            .map(field => [field, typeof sample[field]]));
      }
    } finally { db.close(); }
  }
  return JSON.stringify(out);
}

const browser = await connectTeams();
try { console.log(JSON.stringify(await browser.evalJson("(" + probe.toString() + ")()"))); }
finally { browser.close(); }

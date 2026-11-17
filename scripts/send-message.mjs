// Fixed local UI adapter. No arbitrary JS/HTML from callers, no auth stores, no Teams service APIs.
import {connectTeams} from "./cdp-client.mjs";
import {pathToFileURL} from "node:url";

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

// Comparison form shared with teammatic_format.squash: whitespace carries no meaning.
export function squash(text) {
  return text.replace(/[\s​﻿]+/gu, "");
}

// Every state-changing function checks a stable selected thread, not a display name.
export function uiState({chat_id}) {
  const rows = [...document.querySelectorAll('[role="treeitem"][data-fui-tree-item-value]')];
  const selected = rows.filter(el => {
    if (el.getAttribute("aria-selected") === "true" || el.querySelector('[aria-selected="true"]')) return true;
    try {
      return JSON.parse(el.getAttribute("data-tabster") || "{}").observed?.names?.includes("LeftRailSelectedItem") === true;
    } catch { return false; }
  });
  const ids = selected.map(el => el.getAttribute("data-fui-tree-item-value").split("|").at(-1));
  const editors = [...document.querySelectorAll('div[data-tid="ckeditor"][contenteditable="true"]')];
  const ed = editors[0];
  let draft = ed?.innerText || "";
  // Teams can turn an emoji into an image; its character then lives in alt, not in the text.
  if (ed?.querySelector?.("img[alt]")) {
    const copy = ed.cloneNode(true);
    for (const img of copy.querySelectorAll("img[alt]")) img.replaceWith(img.getAttribute("alt"));
    draft = copy.textContent || "";
  }
  const buttons = [...document.querySelectorAll('button[data-tid$="MessageCommands-send"]')];
  const visible = buttons.filter(el => el.getClientRects().length > 0);
  return {
    target: ids.length === 1 && ids[0] === chat_id,
    selected: ids, editors: editors.length, draft,
    html: ed?.innerHTML || "", send: visible.length === 1 && !visible[0].disabled,
    blocked: !!document.querySelector('[data-tid="compose-disabled-placeholder-text"]'),
  };
}

export function locate({chat_id}) {
  const matches = [...document.querySelectorAll('[role="treeitem"][data-fui-tree-item-value]')]
    .filter(el => el.getAttribute("data-fui-tree-item-value").split("|").at(-1) === chat_id);
  if (matches.length !== 1) throw Error("TARGET_NOT_LOADED_OR_AMBIGUOUS");
  matches[0].scrollIntoView({block: "center"});
  const r = matches[0].getBoundingClientRect();
  if (r.width < 1 || r.height < 1 || r.y < 0 || r.bottom > innerHeight) throw Error("TARGET_OFFSCREEN");
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
}

export async function cached({chat_id, own_creator}) {
  const names = (await indexedDB.databases()).map(x => x.name)
    .filter(n => n?.includes(":replychain-manager:"));
  if (names.length !== 1) throw Error("ACCOUNT_CACHE_AMBIGUOUS");
  const request = indexedDB.open(names[0]);
  const db = await new Promise((ok, fail) => {
    request.onsuccess = () => ok(request.result);
    request.onerror = () => fail(request.error);
    request.onupgradeneeded = () => { request.transaction.abort(); fail(Error("CACHE_DISAPPEARED")); };
  });
  try {
    const thread = chat_id;
    const result = [];
    await new Promise((ok, fail) => {
      const tx = db.transaction("replychains", "readonly");
      const req = tx.objectStore("replychains").openCursor(IDBKeyRange.bound([thread, ""], [thread, "￿"]));
      req.onerror = () => fail(req.error);
      tx.onabort = () => fail(tx.error || Error("CACHE_ABORTED"));
      let count = 0;
      req.onsuccess = () => {
        const cur = req.result;
        if (!cur) { ok(); return; }
        if (++count > 10000) { fail(Error("CACHE_TOO_LARGE")); return; }
        for (const m of Object.values(cur.value?.messageMap || {})) {
          if (m?.type !== "Message" || !m.id || m.creator !== own_creator || m.deletionInfo) continue;
          result.push({id: String(m.id), creator: m.creator, html: m.content || "", deleted: false});
        }
        cur.continue();
      };
    });
    return result;
  } finally { db.close(); }
}

export function selectPart({chat_id, end, all}) {
  const state = uiState({chat_id});
  if (!state.target || state.editors !== 1 || state.blocked) throw Error("TARGET_CHANGED");
  const ed = document.querySelector('div[data-tid="ckeditor"][contenteditable="true"]');
  const node = all ? ed : end ? ed.lastElementChild : ed.children[0];
  if (!node) throw Error("EDITOR_STRUCTURE_CHANGED");
  ed.focus({preventScroll: true});
  const range = document.createRange();
  range.selectNodeContents(node);
  if (end) range.collapse(false);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  return true;
}

// The Teams client itself marks messages sent by the signed-in account. One such
// message by own_creator proves the client runs the account the policy names.
export async function accountProof({own_creator}) {
  const names = (await indexedDB.databases()).map(x => x.name)
    .filter(n => n?.includes(":replychain-manager:"));
  if (names.length !== 1) throw Error("ACCOUNT_CACHE_AMBIGUOUS");
  const request = indexedDB.open(names[0]);
  const db = await new Promise((ok, fail) => {
    request.onsuccess = () => ok(request.result);
    request.onerror = () => fail(request.error);
    request.onupgradeneeded = () => { request.transaction.abort(); fail(Error("CACHE_DISAPPEARED")); };
  });
  try {
    return await new Promise((ok, fail) => {
      const req = db.transaction("replychains", "readonly").objectStore("replychains").openCursor();
      let count = 0;
      req.onerror = () => fail(req.error);
      req.onsuccess = () => {
        const cur = req.result;
        if (!cur || ++count > 20000) { ok(false); return; }
        const mine = Object.values(cur.value?.messageMap || {})
          .some(m => m?.creator === own_creator && m.isSentByCurrentUser === true);
        if (mine) ok(true); else cur.continue();
      };
    });
  } finally { db.close(); }
}

// Rich content enters the editor the way a human paste does: the editor's own
// clipboard pipeline filters it against what Teams supports. The HTML is built
// by teammatic_format.py from a Markdown subset, never supplied by a caller.
export function pasteHtml({chat_id, html, plain}) {
  const state = uiState({chat_id});
  if (!state.target || state.editors !== 1 || state.blocked || state.draft.replace(/[\s​﻿]+/gu, "")) {
    throw Error("TARGET_CHANGED");
  }
  const ed = document.querySelector('div[data-tid="ckeditor"][contenteditable="true"]');
  ed.focus({preventScroll: true});
  const range = document.createRange();
  range.selectNodeContents(ed);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  const data = new DataTransfer();
  data.setData("text/html", html);
  data.setData("text/plain", plain);
  ed.dispatchEvent(new ClipboardEvent("paste", {clipboardData: data, bubbles: true, cancelable: true}));
  return true;
}

export function command({chat_id, tid}) {
  const state = uiState({chat_id});
  if (!state.target || state.editors !== 1 || state.blocked) throw Error("TARGET_CHANGED");
  const el = document.querySelector(`[data-tid="${CSS.escape(tid)}"]`)
    || document.querySelector(`[data-tid="${CSS.escape(tid.replace("newMessageCommands-", "sendMessageCommands-"))}"]`);
  if (!el || el.disabled) throw Error("COMMAND_UNAVAILABLE " + tid);
  el.click();
  return true;
}

export function finalClick({chat_id, expected}) {
  const state = uiState({chat_id});
  const norm = x => x.replace(/[\s​﻿]+/gu, "");
  if (!state.target || state.editors !== 1 || state.blocked || !state.send
      || norm(state.draft) !== norm(expected)) throw Error("PRE_SEND_GUARD");
  const button = [...document.querySelectorAll('button[data-tid$="MessageCommands-send"]')]
    .filter(el => el.getClientRects().length > 0);
  if (button.length !== 1) throw Error("SEND_AMBIGUOUS");
  button[0].click(); // One click only; caller never retries uncertain delivery.
  return true;
}

// progress.clicked tells the caller whether a failure happened before or after the Send click.
export async function deliver(payload, browser, progress = {}) {
  // uiState is the only dependency injected into fixed UI functions.
  const run = (fn, arg) => browser.evalJson(`(()=>{const uiState=${uiState.toString()};return (${fn.toString()})(${JSON.stringify(arg)});})()`);
  const state = () => run(uiState, payload);
  const must = async (empty = false) => {
    const s = await state();
    if (!s.target || s.editors !== 1 || s.blocked || (empty && squash(s.draft))) throw Error("UNSAFE_TARGET_OR_DRAFT");
    return s;
  };
  const key = async (vk, code, modifiers) => {
    for (const type of ["rawKeyDown", "keyUp"]) {
      await browser.send("Input.dispatchKeyEvent", {type, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, code, modifiers});
    }
  };
  const initial = await state();
  if (initial.editors !== 1 || initial.blocked || squash(initial.draft)) throw Error("NONEMPTY_DRAFT_OR_UNSUPPORTED_COMPOSER");
  if (!await run(accountProof, payload)) throw Error("ACCOUNT_NOT_VERIFIED");
  if (!initial.target) {
    if (!payload.allow_navigation) throw Error("SELECT_CHAT_MANUALLY_OR_ALLOW_NAVIGATION");
    const p = await run(locate, payload);
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await browser.send("Input.dispatchMouseEvent", {type, ...p, button: type === "mouseMoved" ? "none" : "left", clickCount: 1});
    }
    for (let i = 0; i < 20; i++) { await pause(150); if ((await state()).target) break; }
  }
  await must(true); // Never overwrite destination drafts.
  const before = new Set((await run(cached, payload)).map(m => m.id));
  if (payload.html) {
    await run(pasteHtml, payload);
    await pause(400);
  } else {
    await run(selectPart, {...payload, end: true});
    await must();
    await browser.send("Input.insertText", {text: payload.type_text});
    await pause(150);
    await run(selectPart, {...payload, all: true});
    await key(88, "KeyX", 10); // Ctrl+Shift+X expands the composer so the toolbar exists.
    await pause(200);
    await run(command, {...payload, tid: "newMessageCommands-RemoveFormat"});
    await pause(150);
  }
  const ready = await must();
  if (squash(ready.draft) !== squash(payload.plain)) throw Error("DRAFT_MISMATCH_LEAVE_FOR_INSPECTION");
  for (const [name, source] of payload.require) {
    if (!new RegExp(source, "iu").test(ready.html)) throw Error("FORMAT_NOT_APPLIED_LEAVE_FOR_INSPECTION " + name);
  }
  if (/(?:^|[;"'\s])(?:color|background-color)\s*:/iu.test(ready.html)) {
    throw Error("NON_NEUTRAL_FORMATTING_LEAVE_FOR_INSPECTION");
  }
  if (payload.draft_only) return {ok: true, drafted: true};
  progress.clicked = true;
  await run(finalClick, {...payload, expected: payload.plain});
  for (let i = 0; i < 30; i++) {
    await pause(300);
    const after = await run(cached, payload);
    // Teams first stores a local echo with a negative id; only the server id proves delivery.
    const added = after.filter(m => !before.has(m.id) && !m.id.startsWith("-"));
    // Python verifies exact text and author against this persisted record.
    if (added.length === 1 && !squash((await state()).draft)) return {ok: true, message: added[0]};
    if (added.length > 1) throw Error("MULTIPLE_NEW_MESSAGES_UNCERTAIN");
  }
  throw Error("SEND_UNCONFIRMED_NO_RETRY");
}

async function main() {
  const progress = {clicked: false};
  try {
    if (process.env.TEAMS_SEND_APPROVED !== "YES") throw Error("Use `teammatic.py run`; direct adapter execution blocked");
    let input = "";
    for await (const part of process.stdin) { input += part; if (input.length > 200000) throw Error("INPUT_TOO_LARGE"); }
    const payload = JSON.parse(input);
    const browser = await connectTeams();
    try { console.log(JSON.stringify(await deliver(payload, browser, progress))); }
    finally { browser.close(); }
  } catch (error) {
    console.error(JSON.stringify({ok: false, error: error.message, clicked: progress.clicked}));
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();

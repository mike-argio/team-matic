// Offline contract tests with a tiny fake DOM. Never connects to CDP.
import assert from "node:assert/strict";
import {squash, uiState, finalClick, selectPart, pasteHtml, deliver} from "../scripts/send-message.mjs";

assert.equal(squash("🐾\n  A B​"), "🐾AB");
let clicks = 0;
let selected = "chat-a";
let draft = "Hello";
let draftHtml = "<p>Hello</p>";
let selectionMode = "aria";
let label = "Wyślij";
const button = {
  disabled: false, getClientRects: () => [{}],
  getAttribute: () => label, click: () => clicks++,
};
const row = {
  getAttribute: key => key === "aria-selected" ? (selectionMode === "aria" ? "true" : null)
    : key === "data-tabster" ? JSON.stringify({observed: {names: selectionMode === "tabster" ? ["LeftRailSelectedItem"] : []}})
    : `chat|${selected}`,
  querySelector: () => null,
};
let pasted = null;
const editor = {
  get innerText() { return draft; }, get innerHTML() { return draftHtml; },
  querySelector: () => null, focus() {},
  dispatchEvent: event => { pasted = event; draft = event.clipboardData.get("text/plain"); draftHtml = event.clipboardData.get("text/html"); },
};
globalThis.document = {
  querySelectorAll: selector => selector.includes('role="treeitem"') ? [row]
    : selector.includes("ckeditor") ? [editor] : [button],
  querySelector: selector => selector.includes("ckeditor") ? editor : null,
  createRange: () => ({selectNodeContents() {}, collapse() {}}),
};
globalThis.window = {getSelection: () => ({removeAllRanges() {}, addRange() {}})};
globalThis.DataTransfer = class { #data = new Map(); setData(k, v) { this.#data.set(k, v); } get(k) { return this.#data.get(k); } };
globalThis.ClipboardEvent = class { constructor(type, init) { this.type = type; this.clipboardData = init.clipboardData; } };

assert.equal(uiState({chat_id: "chat-a"}).target, true);
selectionMode = "tabster";
assert.equal(uiState({chat_id: "chat-a"}).target, true);
selectionMode = "none";
assert.equal(uiState({chat_id: "chat-a"}).target, false);
selectionMode = "aria";
// The Send button is recognised by its stable id, not by a localized label.
label = "Envoyer";
assert.equal(uiState({chat_id: "chat-a"}).send, true);
// An emoji rendered as an image still counts as draft text.
const emojiEditor = {
  innerText: "Hi ", innerHTML: "", querySelector: () => ({}),
  cloneNode: () => ({querySelectorAll: () => [], textContent: "Hi 🙂"}),
};
const realAll = document.querySelectorAll;
document.querySelectorAll = selector => selector.includes("ckeditor") ? [emojiEditor] : realAll(selector);
assert.equal(uiState({chat_id: "chat-a"}).draft, "Hi 🙂");
document.querySelectorAll = realAll;

assert.throws(() => finalClick({chat_id: "chat-b", expected: "Hello"}), /PRE_SEND_GUARD/);
assert.throws(() => finalClick({chat_id: "chat-a", expected: "Altered"}), /PRE_SEND_GUARD/);
assert.equal(clicks, 0);
finalClick({chat_id: "chat-a", expected: "Hel lo"});
assert.equal(clicks, 1);
button.disabled = true;
assert.throws(() => finalClick({chat_id: "chat-a", expected: "Hello"}), /PRE_SEND_GUARD/);
button.disabled = false;
selected = "other";
assert.throws(() => selectPart({chat_id: "chat-a"}), /TARGET_CHANGED/);
selected = "chat-a";
// Paste never overwrites an existing draft.
assert.throws(() => pasteHtml({chat_id: "chat-a", html: "<p>x</p>", plain: "x"}), /TARGET_CHANGED/);
assert.equal(pasted, null);

// Fail before any input delivery when composer contains someone's draft.
const browser = {
  evalJson: async expression => eval(expression),
  send: () => { throw Error("Unexpected physical input"); },
};
const progress = {clicked: false};
await assert.rejects(deliver({chat_id: "chat-a"}, browser, progress), /NONEMPTY_DRAFT/);
assert.equal(clicks, 1);
assert.equal(progress.clicked, false);

// Rich path: composes through paste, verifies text and formats, and stops before Send in draft mode.
draft = ""; draftHtml = "";
globalThis.indexedDB = {
  databases: async () => [{name: "x:replychain-manager:y"}],
  open: () => {
    const request = {};
    queueMicrotask(() => {
      request.result = {
        close() {},
        transaction: () => {
          const tx = {objectStore: () => ({openCursor: () => {
            const req = {};
            queueMicrotask(() => {
              req.result = {value: {messageMap: {a: {type: "Message", id: "old", creator: "8:me", content: "<p>old</p>", isSentByCurrentUser: true}}},
                continue() { req.result = null; req.onsuccess(); }};
              req.onsuccess();
            });
            return req;
          }})};
          return tx;
        },
      };
      request.onsuccess();
    });
    return request;
  },
};
globalThis.IDBKeyRange = {bound: () => null};
const rich = {chat_id: "chat-a", own_creator: "8:me", allow_navigation: false, draft_only: true,
  html: "<p><strong>Hi</strong></p>", plain: "Hi", type_text: "Hi", require: [["bold", "<(strong|b)[\\s>]"]]};
assert.deepEqual(await deliver(rich, browser, progress), {ok: true, drafted: true});
assert.equal(pasted.type, "paste");
assert.equal(clicks, 1);
assert.equal(progress.clicked, false);
// A format the editor dropped blocks the send and leaves the draft.
draft = ""; draftHtml = "";
editor.dispatchEvent = event => { draft = event.clipboardData.get("text/plain"); draftHtml = "<p>Hi</p>"; };
await assert.rejects(deliver({...rich, draft_only: false}, browser, progress), /FORMAT_NOT_APPLIED_LEAVE_FOR_INSPECTION bold/);
assert.equal(clicks, 1);
assert.equal(progress.clicked, false);
// The account must be proven by the client's own flag, never assumed.
draft = ""; draftHtml = "";
await assert.rejects(deliver({...rich, own_creator: "8:someone-else"}, browser, progress), /ACCOUNT_NOT_VERIFIED/);
assert.equal(progress.clicked, false);
// Teams refusing the recipient ("wait until they reply") gets its own code, before any input.
const realOne = document.querySelector;
document.querySelector = selector => selector.includes("compose-disabled") ? {} : realOne(selector);
await assert.rejects(deliver(rich, browser, progress), /COMPOSE_BLOCKED_BY_TEAMS/);
document.querySelector = realOne;
// A draft in another, currently selected chat does not block navigating to the target.
selected = "other"; draft = "someone's draft";
await assert.rejects(deliver({...rich, chat_id: "chat-a"}, browser, progress), /SELECT_CHAT_MANUALLY_OR_ALLOW_NAVIGATION/);
selected = "chat-a"; draft = "";
assert.equal(clicks, 1);
assert.equal(progress.clicked, false);
console.log("Adapter guard checks PASS (offline; no UI compatibility claim)");

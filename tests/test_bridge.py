"""Offline checks for the reader: parsing, account detection, sync pipeline, cursors."""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import teams_bridge
from teams_bridge import incoming, resolve_own_creator, stats, strip_html


def chat(thread, *messages):
    return {"threadId": thread, "title": "Chat " + thread, "kind": "chat", "rosterTime": None,
            "rosterPreview": None, "replyChains": 1,
            "messages": [{"id": f"{thread}-{i}", "creator": c, "mine": mine, "timeMs": 1000 + i,
                          "html": f"<p>{c} says {i}</p>", "messageType": kind, "deleted": False}
                         for i, (c, mine, kind) in enumerate(messages)]}


ME, ANN, BOB, EVE = "8:me", "8:ann", "8:bob", "8:eve"


class ParserTests(unittest.TestCase):
    def test_html_text(self):
        self.assertEqual(strip_html("<p>Hi <strong>Anna</strong>!</p>"), "Hi Anna!")

    def test_multiline(self):
        self.assertEqual(strip_html("<p>A<br>B</p>"), "A\nB")

    def test_script_ignored(self):
        self.assertEqual(strip_html("<script>bad()</script><p>Safe</p>"), "Safe")

    def test_entity_decode(self):
        self.assertEqual(strip_html("<p>A &amp; B</p>"), "A & B")

    def test_emoji_image_and_table_cells(self):
        self.assertEqual(strip_html('<p>Ok <img alt="🙂" itemtype="http://schema.skype.com/Emoji" src="x">'
                                    '<img alt="photo" itemtype="http://schema.skype.com/AMSImage"></p>'), "Ok 🙂")
        self.assertEqual(strip_html("<table><tr><td>A</td><td>B</td></tr></table>"), "A\nB")


class IdentityTests(unittest.TestCase):
    def test_client_flag_wins_with_one_chat(self):
        chats = [chat("a", (ME, True, "Text"), (ANN, False, "Text"))]
        self.assertEqual(resolve_own_creator(chats), (ME, "client-flag"))

    def test_spread_across_chats_without_flag(self):
        chats = [chat("a", (ME, None, "Text"), (ANN, None, "Text")), chat("b", (ME, None, "Text"), (BOB, None, "Text")),
                 chat("c", (ME, None, "Text"), (EVE, None, "Text"))]
        self.assertEqual(resolve_own_creator(chats), (ME, "chat-spread"))

    def test_ambiguous_fails_closed_and_names_candidates(self):
        two = [chat("a", (ME, None, "Text"), (ANN, None, "Text")), chat("b", (ME, None, "Text"), (ANN, None, "Text"))]
        with self.assertRaisesRegex(RuntimeError, "whoami --set"):
            resolve_own_creator(two)
        self.assertEqual(resolve_own_creator(two, explicit=ANN), (ANN, "configured"))

    def test_disagreeing_signals_fail_closed(self):
        chats = [chat("a", (ME, None, "Text"), (ANN, True, "Text")), chat("b", (ME, None, "Text")),
                 chat("c", (ME, None, "Text")), chat("d", (ME, None, "Text"))]
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            resolve_own_creator(chats)
        with self.assertRaisesRegex(RuntimeError, "different account"):
            resolve_own_creator(chats, explicit=ME)
        with self.assertRaisesRegex(RuntimeError, "Several"):
            resolve_own_creator([chat("a", (ME, True, "Text"), (ANN, True, "Text"))])


class SyncPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (("DB", root / "cache.sqlite3"), ("DATA", root), ("SNAPSHOT", root / "snap.json.gz")):
            patcher = patch.object(teams_bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(teams_bridge, "verify_port_ownership", Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, chats):
        def collector(*_args, **_kwargs):
            with gzip.open(teams_bridge.SNAPSHOT, "wt", encoding="utf-8") as f:
                json.dump({"schema": teams_bridge.SCHEMA, "chats": chats}, f)
            return Mock(returncode=0, stdout="", stderr="")
        with patch.object(teams_bridge.subprocess, "run", collector):
            return teams_bridge.sync()

    def test_first_sync_detects_account_and_is_a_baseline(self):
        chats = [chat("a", (ME, True, "Text"), (ANN, False, "Text"), (ANN, False, "ThreadActivity/AddMember"))]
        out = self.sync(chats)
        self.assertEqual((out["own_creator"], out["own_creator_source"], out["initial_baseline"]),
                         (ME, "client-flag", True))
        self.assertEqual((out["incoming"], out["outgoing"]), (2, 1))
        self.assertEqual(incoming(limit=10, only_new=True), [])
        self.assertEqual([m["message_id"] for m in incoming(limit=10)], ["a-1"])  # System events are not messages.
        total = stats()
        self.assertEqual(total["messages"]["all_messages"],
                         total["messages"]["incoming"] + total["messages"]["outgoing"])
        self.assertFalse((teams_bridge.DATA / ".sync.lock").exists())

    def test_cursor_delivers_each_new_message_once(self):
        self.sync([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        with self.assertRaisesRegex(RuntimeError, "Unknown cursor"):
            teams_bridge.read_cursor("bot", commit=False)
        first = teams_bridge.read_cursor("bot", commit=True)
        self.assertEqual((first["messages"], first["created"], first["untrusted_content"]), ([], True, True))
        self.sync([chat("a", (ME, True, "Text"), (ANN, False, "Text"), (ANN, False, "Text"))])
        seen = teams_bridge.read_cursor("bot", commit=False)
        self.assertEqual([m["message_id"] for m in seen["messages"]], ["a-2"])
        self.assertEqual(len(teams_bridge.read_cursor("bot", commit=True)["messages"]), 1)
        self.assertEqual(teams_bridge.read_cursor("bot", commit=False)["messages"], [])
        self.assertEqual(teams_bridge.read_cursor("other", commit=True)["messages"], [])

    def test_account_switch_is_refused_until_reset(self):
        self.sync([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        switched = [chat("z", (BOB, True, "Text"), (EVE, False, "Text"))]
        with self.assertRaisesRegex(RuntimeError, "different account"):
            self.sync(switched)
        self.assertEqual(teams_bridge.whoami()["own_creator"], ME)
        self.assertIsNone(teams_bridge.whoami(reset=True)["own_creator"])
        self.assertEqual(self.sync(switched)["own_creator"], BOB)

    def test_read_chat_orders_oldest_first_and_rejects_unknown(self):
        self.sync([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        self.assertEqual([m["direction"] for m in teams_bridge.read_chat("a")], ["outgoing", "incoming"])
        with self.assertRaisesRegex(RuntimeError, "Unknown chat_id"):
            teams_bridge.read_chat("nope")


if __name__ == "__main__":
    unittest.main()

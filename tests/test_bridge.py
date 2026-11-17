"""Offline checks for the reader: parsing, account detection, live and offline reads, cursors."""
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

    def test_configured_account_conflicts_with_spread_without_client_flag(self):
        chats = [chat(thread, (ME, None, "Text")) for thread in "abc"]
        with self.assertRaisesRegex(RuntimeError, "different account"):
            resolve_own_creator(chats, explicit=ANN)

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


class ReadTests(unittest.TestCase):
    """The live client is replaced by a fake collector that prints JSON, like the real one."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("DB", self.root / "cache.sqlite3"), ("DATA", self.root),
                            ("STATE", self.root / "state.json"), ("OFFLINE", False)):
            patcher = patch.object(teams_bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("verify_port_ownership",):
            patcher = patch.object(teams_bridge, name, Mock())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(teams_bridge.close_view)
        self.client = []

        def collector(*_args, **_kwargs):
            return Mock(returncode=0, stderr="", stdout=json.dumps({"schema": teams_bridge.SCHEMA, "chats": self.client}))
        patcher = patch.object(teams_bridge.subprocess, "run", collector)
        patcher.start()
        self.addCleanup(patcher.stop)

    def live(self, chats):
        """Next command reads this client state, as a fresh one-shot process would."""
        teams_bridge.close_view()
        self.client = chats

    def test_live_read_keeps_no_copy_of_messages(self):
        self.live([chat("a", (ME, True, "Text"), (ANN, False, "Text"), (ANN, False, "ThreadActivity/AddMember"))])
        self.assertEqual([m["message_id"] for m in incoming(limit=10)], ["a-1"])  # System events are not messages.
        total = stats()
        self.assertEqual((total["source"], total["offline_copy"]), ("live", None))
        self.assertEqual(total["messages"]["all_messages"], total["messages"]["incoming"] + total["messages"]["outgoing"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["state.json"])
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state, {"own_creator": ME, "chats": {"a": "Chat a"}})
        self.assertNotIn("says", json.dumps(state))

    def test_sync_saves_copy_and_offline_reads_it(self):
        self.live([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        out = teams_bridge.sync()
        self.assertEqual((out["own_creator"], out["own_creator_source"], out["incoming"]), (ME, "client-flag", 1))
        self.assertFalse((self.root / ".sync.lock").exists())
        self.live([])
        teams_bridge.OFFLINE = True
        self.assertEqual(stats()["source"], "offline")
        self.assertEqual([m["message_id"] for m in incoming(limit=10)], ["a-1"])

    def test_offline_without_copy_explains_what_to_do(self):
        teams_bridge.OFFLINE = True
        with self.assertRaisesRegex(RuntimeError, "sync"):
            stats()

    def test_cursor_delivers_each_new_message_once(self):
        self.live([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        with self.assertRaisesRegex(RuntimeError, "Unknown cursor"):
            teams_bridge.read_cursor("bot", commit=False)
        first = teams_bridge.read_cursor("bot", commit=True)
        self.assertEqual((first["messages"], first["created"], first["untrusted_content"]), ([], True, True))
        self.live([chat("a", (ME, True, "Text"), (ANN, False, "Text"), (ANN, False, "Text"))])
        self.assertEqual([m["message_id"] for m in teams_bridge.read_cursor("bot", commit=False)["messages"]], ["a-2"])
        self.assertEqual(len(teams_bridge.read_cursor("bot", commit=True)["messages"]), 1)
        self.live(self.client)
        self.assertEqual(teams_bridge.read_cursor("bot", commit=False)["messages"], [])
        self.assertEqual(teams_bridge.read_cursor("other", commit=True)["messages"], [])

    def test_account_switch_is_refused_until_reset(self):
        self.live([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        stats()
        self.live([chat("z", (BOB, True, "Text"), (EVE, False, "Text"))])
        with self.assertRaisesRegex(RuntimeError, "different account"):
            stats()
        self.assertEqual(teams_bridge.whoami()["own_creator"], ME)
        self.assertIsNone(teams_bridge.whoami(reset=True)["own_creator"])
        stats()
        self.assertEqual(teams_bridge.whoami()["own_creator"], BOB)
        self.assertEqual(teams_bridge.known_chats(), {"z": "Chat z"})

    def test_read_chat_orders_oldest_first_and_rejects_unknown(self):
        self.live([chat("a", (ME, True, "Text"), (ANN, False, "Text"))])
        self.assertEqual([m["direction"] for m in teams_bridge.read_chat("a")], ["outgoing", "incoming"])
        self.assertEqual(teams_bridge.find_message("a", "a-1")["creator"], ANN)
        with self.assertRaisesRegex(RuntimeError, "Unknown chat_id"):
            teams_bridge.read_chat("nope")


if __name__ == "__main__":
    unittest.main()

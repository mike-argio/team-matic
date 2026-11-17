"""Offline outbox, formatting and skill tests: no real cache, Teams, CDP, or network."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import teammatic
from teammatic_format import blocks, inline, render, squash
from teams_send import NotSent, Outbox, exclusive_lock, expand, expected_text, normalize_message, verify_content

ROOT = Path(__file__).resolve().parents[1]


class FormatTests(unittest.TestCase):
    def test_inline_marks_links_and_escapes(self):
        runs = inline(r"a **b *c*** ~~d~~ `e*f` [g](https://x.test/p) \*h\* 2*3*4 snake_case_name")
        self.assertEqual("".join(r["t"] for r in runs), "a b c d e*f g *h* 2*3*4 snake_case_name")
        marks = {r["t"]: r["m"] for r in runs}
        self.assertEqual(marks["b "], ["bold"])
        self.assertEqual(marks["c"], ["bold", "em"])
        self.assertEqual(marks["d"], ["strike"])
        self.assertEqual(marks["e*f"], ["code"])
        self.assertEqual(next(r["href"] for r in runs if r["t"] == "g"), "https://x.test/p")

    def test_unsafe_link_schemes_stay_literal(self):
        out = render({"text": "[x](javascript:alert(1)) <script>", "format": "markdown"})
        self.assertEqual(out["html"], "")
        self.assertIn("javascript:", out["plain"])

    def test_blocks(self):
        kinds = [b["type"] for b in blocks("# T\n\npara\nline2\n\n- a\n- b\n\n1. x\n2) y\n\n> q\n\n```\ncode **raw**\n```")]
        self.assertEqual(kinds, ["h1", "p", "ul", "ol", "quote", "pre"])
        with self.assertRaises(ValueError):
            blocks("```\nnever closed")

    def test_render_escapes_and_requires(self):
        out = render({"title": "T<1>", "text": "**b** & <i>", "format": "markdown",
                      "table": [["H"], ["c"]], "table_header": True})
        self.assertEqual(out["html"], "<p><strong>T&lt;1&gt;</strong></p><p><strong>b</strong> &amp; &lt;i&gt;</p>"
                         "<table><tbody><tr><td><strong>H</strong></td></tr><tr><td>c</td></tr></tbody></table>")
        self.assertEqual(out["plain"], "T<1>\nb & <i>\nH\nc")
        self.assertEqual([name for name, _ in out["require"]], ["bold", "table"])

    def test_plain_text_takes_typing_path(self):
        for message in ({"text": "Hello 🐾\nsecond", "format": "markdown"}, {"text": "**kept**", "format": "plain"},
                        {"text": "old stored job"}):
            out = render(message)
            self.assertEqual((out["html"], out["require"]), ("", []))
        self.assertEqual(render({"text": "**kept**", "format": "plain"})["type_text"], "**kept**")

    def test_persisted_teams_markup_matches(self):
        payload = normalize_message({"chat_id": "c", "title": "Demo", "text": "Hi 🙂 *there*\n\n- one",
                                     "table": [["A", "B"]]})
        verify_content(payload, '<p><b>Demo</b></p><p>Hi <span class="x"><img alt="🙂" itemtype="http://schema.skype.com/Emoji"></span>'
                                ' <i>there</i></p><ul><li>one</li></ul><table><tr><td><strong>A</strong></td><td>B</td></tr></table>')
        with self.assertRaises(RuntimeError):
            verify_content(payload, "<p>Demo</p><p>Hi 🙂 there</p><p>one</p><p>A</p><p>B</p>")
        self.assertEqual(squash(" a​\n b\xa0"), "ab")


class TemplateTests(unittest.TestCase):
    def test_mail_merge(self):
        messages = expand({"template": {"text": "Hi {{ name }}", "table": [["{{n}}"]]},
                           "recipients": [{"chat_id": "a", "vars": {"name": "Ann", "n": 2}},
                                          {"chat_id": "b", "vars": {"name": "Bob", "n": 3}}]})
        self.assertEqual(messages, [{"text": "Hi Ann", "table": [["2"]], "chat_id": "a"},
                                    {"text": "Hi Bob", "table": [["3"]], "chat_id": "b"}])

    def test_missing_variable_and_bad_shapes(self):
        for document in ({"template": {"text": "{{x}}"}, "recipients": [{"chat_id": "a"}]},
                         {"template": {"text": "x", "chat_id": "a"}, "recipients": []},
                         {"messages": [], "template": {}}, []):
            with self.subTest(document=document), self.assertRaises(ValueError):
                expand(document)


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.skill = ROOT / "skills" / "team-matic"
        self.runner = runpy.run_path(str(self.skill / "scripts" / "run.py"))

    def test_metadata_and_no_private_paths(self):
        content = (self.skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\nname: team-matic\ndescription: \""))
        self.assertNotIn(str(ROOT), content)
        self.assertEqual(self.runner["engine_root"](), ROOT)

    def test_commands_forwarded_and_exit_code_preserved(self):
        for command in self.runner["COMMANDS"]:
            with self.subTest(command=command), patch("subprocess.run") as child:
                child.return_value.returncode = 7
                self.assertEqual(self.runner["main"]([command, "--help"]), 7)
                self.assertEqual(child.call_args.args[0][1:], [str(ROOT / "teammatic.py"), command, "--help"])

    def test_resident_and_unknown_commands_never_start_process(self):
        for command in ("watch", "daemon", "serve", "install-skill", "unknown"):
            with self.subTest(command=command), patch("subprocess.run") as child:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.runner["main"]([command]), 2)
                child.assert_not_called()

    def test_installed_copy_finds_engine_through_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = teammatic.install_skill(tmp, update=False)
            copied = Path(out["skill"])
            self.assertEqual(sorted(p.name for p in copied.rglob("*") if p.is_file()),
                             ["SKILL.md", "engine_root.txt", "run.py"])
            with self.assertRaises(RuntimeError):
                teammatic.install_skill(tmp, update=False)
            result = subprocess.run([sys.executable, str(copied / "scripts" / "run.py"), "plan", "--help"],
                                    capture_output=True, text=True, timeout=20, cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--policy", result.stdout)
            (copied / "engine_root.txt").unlink()
            with patch.dict("os.environ", {"TEAMMATIC_ROOT": ""}):
                missing = subprocess.run([sys.executable, str(copied / "scripts" / "run.py"), "stats"],
                                         capture_output=True, text=True, timeout=20, cwd=tmp)
            self.assertEqual(missing.returncode, 2)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.stored = {}
        self.box = Outbox(root / "outbox.sqlite3", chats=lambda: {"chat-a": "Same name", "chat-b": "Same name"},
                          lookup=lambda chat, message: self.stored.get((chat, message)))
        self.policy = dict(enabled=True, allowed_chat_ids=["chat-a", "chat-b"],
                           own_creator="8:owner", min_interval_seconds=30, max_batch_size=5)

    def tearDown(self):
        self.box.close()
        self.tmp.cleanup()

    def plan(self, messages=None):
        return self.box.plan({"messages": messages or [{"chat_id": "chat-a", "text": "Hello 🐾"}]}, self.policy)

    def success(self, payload):
        return {"ok": True, "message": {"id": "new-id", "creator": "8:owner",
                                        "html": payload["html"] or "<p>" + payload["plain"] + "</p>", "deleted": False}}

    def test_preview_and_idempotent_plan(self):
        view = self.plan()
        self.assertEqual(view, self.plan())
        self.assertEqual(view["jobs"][0]["payload"]["text"], "Hello 🐾")
        self.assertEqual(view["jobs"][0]["preview"], "Hello 🐾")
        self.assertEqual(len(view["confirm"]), 64)
        self.assertEqual(view["jobs"][0]["state"], "pending")

    def test_policy_input_and_allowlist(self):
        for change in ({"enabled": False}, {"min_interval_seconds": 0}, {"max_batch_size": True},
                       {"allowed_chat_ids": []}, {"own_creator": ""}, {"daily_limit": 0}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.box.plan({"messages": [{"chat_id": "chat-a", "text": "Hi"}]}, {**self.policy, **change})
        for message in ({"chat_id": "other", "text": "Hi"}, {"chat_id": "chat-a", "text": ""},
                        {"chat_id": "chat-a", "text": "Hi", "html": "<script>bad</script>"},
                        {"chat_id": "chat-a", "text": "Hi", "format": "html"},
                        {"chat_id": "chat-a", "text": "```\nopen"},
                        {"chat_id": "chat-a", "text": "Hi", "table": [["A"], ["B", "C"]]}):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.plan([message])
        with self.assertRaises(ValueError):
            self.box.plan({"messages": [{"chat_id": "unknown", "text": "Hi"}]},
                          {**self.policy, "allowed_chat_ids": ["unknown"]})

    def test_examples_plan(self):
        for name in ("message.json", "mail-merge.json"):
            document = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
            targets = document.get("messages") or document["recipients"]
            for target, chat in zip(targets, ("chat-a", "chat-b")):
                target["chat_id"] = chat
            view = self.box.plan(document, self.policy)
            self.assertEqual(len(view["jobs"]), len(targets))
            self.assertNotIn("{{", json.dumps(view["jobs"]))
        policy = json.loads((ROOT / "examples" / "send-policy.example.json").read_text(encoding="utf-8"))
        self.assertIs(policy["enabled"], False)

    def test_dedup_is_atomic_and_occurrence_allows_intended_repeat(self):
        self.plan()
        with self.assertRaisesRegex(ValueError, "occurrence"):
            self.plan([{"chat_id": "chat-b", "text": "Other"}, {"chat_id": "chat-a", "text": "Hello 🐾"}])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM batches").fetchone()[0], 1)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        repeat = self.plan([{"chat_id": "chat-a", "text": "Hello 🐾", "occurrence": "2026-W47"}])
        self.assertEqual(repeat["jobs"][0]["payload"]["occurrence"], "2026-W47")

    def test_confirmation_and_policy_changed_no_backend(self):
        view = self.plan()
        backend = Mock()
        for confirmation, policy in (("wrong", self.policy),
                                     (view["batch"], {**self.policy, "enabled": False}),
                                     (view["batch"], {**self.policy, "min_interval_seconds": 60})):
            with self.assertRaises(ValueError):
                self.box.run(view["batch"], confirmation, policy, backend)
        backend.assert_not_called()

    def test_approval_digest_covers_queued_content(self):
        view = self.plan()
        with self.box.db:
            self.box.db.execute("UPDATE jobs SET payload=?", (json.dumps({
                "chat_id": "chat-b", "text": "Changed", "title": "", "table": []}),))
        backend = Mock()
        with self.assertRaises(ValueError):
            self.box.run(view["batch"], view["confirm"], self.policy, backend)
        backend.assert_not_called()

    def test_sent_is_not_repeated(self):
        view = self.plan()
        backend = Mock(side_effect=self.success)
        for _ in range(2):
            self.box.run(view["batch"], view["confirm"], self.policy, backend)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(self.box.show(view["batch"])["jobs"][0]["state"], "sent")

    def test_backend_receives_rendered_message(self):
        view = self.plan([{"chat_id": "chat-a", "text": "**Hi**", "title": "T"}])
        backend = Mock(side_effect=self.success)
        self.box.run(view["batch"], view["confirm"], self.policy, backend)
        sent = backend.call_args.args[0]
        self.assertEqual(sent["html"], "<p><strong>T</strong></p><p><strong>Hi</strong></p>")
        self.assertEqual((sent["plain"], sent["draft_only"], sent["allow_navigation"]), ("T\nHi", False, False))

    def test_uncertain_stops_batch_and_other_batches(self):
        view = self.plan([{"chat_id": "chat-a", "text": "One"}, {"chat_id": "chat-b", "text": "Two"}])
        backend = Mock(side_effect=TimeoutError("after click"))
        with self.assertRaises(TimeoutError):
            self.box.run(view["batch"], view["confirm"], self.policy, backend)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual([x["state"] for x in self.box.show(view["batch"])["jobs"]], ["uncertain", "pending"])
        with self.assertRaises(ValueError):
            self.box.run(view["batch"], view["confirm"], self.policy, backend)
        other = self.plan([{"chat_id": "chat-a", "text": "Three"}])
        with self.assertRaises(ValueError):
            self.box.run(other["batch"], other["confirm"], self.policy, backend)
        self.assertEqual(backend.call_count, 1)

    def test_stop_before_click_stays_retryable(self):
        view = self.plan()
        with self.assertRaises(NotSent):
            self.box.run(view["batch"], view["confirm"], self.policy, Mock(side_effect=NotSent("NONEMPTY_DRAFT")))
        job = self.box.show(view["batch"])["jobs"][0]
        self.assertEqual((job["state"], job["error"]), ("pending", "NONEMPTY_DRAFT"))
        self.box.run(view["batch"], view["confirm"], self.policy, self.success, clock=lambda: 10**10)
        self.assertEqual(self.box.show(view["batch"])["jobs"][0]["state"], "sent")

    def test_abandon_releases_queue_only_for_unresolved_jobs(self):
        view = self.plan()
        job = view["jobs"][0]["id"]
        with self.assertRaises(ValueError):
            self.box.abandon(job)
        with self.assertRaises(TimeoutError):
            self.box.run(view["batch"], view["confirm"], self.policy, Mock(side_effect=TimeoutError()))
        self.box.abandon(job)
        other = self.plan([{"chat_id": "chat-a", "text": "Next"}])
        self.box.run(other["batch"], other["confirm"], self.policy, self.success, clock=lambda: 10**10)

    def test_draft_only_composes_one_and_never_sends_it_later(self):
        view = self.plan([{"chat_id": "chat-a", "text": "One"}, {"chat_id": "chat-b", "text": "Two"}])
        backend = Mock(return_value={"ok": True, "drafted": True})
        out = self.box.run(view["batch"], view["confirm"], self.policy, backend, draft_only=True)
        self.assertEqual(out["results"], [{"job": view["jobs"][0]["id"], "state": "drafted"}])
        self.assertIs(backend.call_args.args[0]["draft_only"], True)
        sender = Mock(side_effect=self.success)
        self.box.run(view["batch"], view["confirm"], self.policy, sender, clock=lambda: 10**10)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual([x["state"] for x in self.box.show(view["batch"])["jobs"]], ["drafted", "sent"])

    def test_daily_limit(self):
        policy = {**self.policy, "daily_limit": 1}
        view = self.box.plan({"messages": [{"chat_id": "chat-a", "text": "One"},
                                           {"chat_id": "chat-b", "text": "Two"}]}, policy)
        with self.assertRaisesRegex(ValueError, "daily_limit"):
            self.box.run(view["batch"], view["confirm"], policy, self.success, clock=lambda: 100, sleep=Mock())
        self.assertEqual([x["state"] for x in self.box.show(view["batch"])["jobs"]], ["sent", "pending"])

    def test_wrong_persisted_content_or_creator_is_uncertain(self):
        view = self.plan()
        backend = Mock(return_value={"ok": True, "message": {
            "id": "x", "creator": "8:someone-else", "html": "Hello 🐾", "deleted": False}})
        with self.assertRaises(RuntimeError):
            self.box.run(view["batch"], view["confirm"], self.policy, backend)
        self.assertEqual(self.box.show(view["batch"])["jobs"][0]["state"], "uncertain")

    def test_local_echo_id_is_not_proof_of_delivery(self):
        view = self.plan()
        echo = Mock(return_value={"ok": True, "message": {
            "id": "-1", "creator": "8:owner", "html": "<p>Hello 🐾</p>", "deleted": False}})
        with self.assertRaises(RuntimeError):
            self.box.run(view["batch"], view["confirm"], self.policy, echo)
        self.assertEqual(self.box.show(view["batch"])["jobs"][0]["state"], "uncertain")

    def test_locks_fail_closed_and_stale_lock_is_replaced(self):
        view = self.plan()
        backend = Mock()
        lock = self.box.path.parent / ".send.lock"
        with exclusive_lock(lock):
            with self.assertRaisesRegex(RuntimeError, "holds"):
                self.box.run(view["batch"], view["confirm"], self.policy, backend)
        backend.assert_not_called()
        self.assertFalse(lock.exists())
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        lock.write_text(str(gone.pid), encoding="ascii")
        self.box.run(view["batch"], view["confirm"], self.policy, self.success)
        self.assertFalse(lock.exists())

    def test_spacing_and_mid_batch_revocation(self):
        view = self.plan([{"chat_id": "chat-a", "text": "One"}, {"chat_id": "chat-b", "text": "Two"}])
        backend = Mock(side_effect=self.success)
        sleeper = Mock()
        self.box.run(view["batch"], view["confirm"], self.policy, backend,
                     clock=lambda: 100, sleep=sleeper)
        sleeper.assert_called_once_with(30)
        second = self.plan([{"chat_id": "chat-a", "text": "Changed policy"}])
        with self.assertRaises(ValueError):
            self.box.run(second["batch"], second["confirm"], self.policy, backend,
                         clock=lambda: 200, sleep=sleeper,
                         policy_loader=lambda: {**self.policy, "enabled": False})
        self.assertEqual(backend.call_count, 2)

    def test_attempting_after_crash_blocks_retry(self):
        view = self.plan()
        with self.box.db:
            self.box.db.execute("UPDATE jobs SET state='attempting'")
        with self.assertRaises(ValueError):
            self.box.run(view["batch"], view["confirm"], self.policy, Mock())

    def test_delivered_without_formatting_is_sent_with_warning(self):
        view = self.plan([{"chat_id": "chat-a", "text": "Demo", "table": [["A", "B"]]}])
        flat = Mock(return_value={"ok": True, "message": {
            "id": "m1", "creator": "8:owner", "html": "<p>Demo</p><p>A</p><p>B</p>", "deleted": False}})
        out = self.box.run(view["batch"], view["confirm"], self.policy, flat)
        self.assertIn("table", out["results"][0]["warning"])
        job = self.box.show(view["batch"])["jobs"][0]
        self.assertEqual((job["state"], job["message_id"]), ("sent", "m1"))

    def test_cancel_only_unsent_preserves_history(self):
        view = self.plan()
        self.box.cancel(view["batch"])
        self.assertEqual(self.box.show(view["batch"])["jobs"][0]["state"], "cancelled")
        with self.assertRaises(ValueError):
            self.box.run(view["batch"], view["confirm"], self.policy, Mock())

    def test_reconcile_requires_persisted_proof(self):
        view = self.plan()
        with self.assertRaises(TimeoutError):
            self.box.run(view["batch"], view["confirm"], self.policy, Mock(side_effect=TimeoutError()))
        self.stored[("chat-a", "proof")] = {"creator": "8:owner", "direction": "outgoing", "deleted": 0,
                                            "time_ms": 9999999999999, "html": "<p>Hello 🐾</p>"}
        job = view["jobs"][0]["id"]
        with self.assertRaises(ValueError):
            self.box.reconcile(job, "wrong-id")
        self.box.reconcile(job, "proof")
        self.assertEqual(self.box.show(view["batch"])["jobs"][0]["state"], "sent")
        self.assertEqual(expected_text(view["jobs"][0]["payload"]), "Hello 🐾")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from sips.adapters import claude_code, codex, generic
from sips.analyze import analyze
from sips.classify import classify, feedback_type
from sips.model import AgentAction, HumanTurn
from sips.redact import redact


def _write(lines, name="s.jsonl"):
    d = Path(tempfile.mkdtemp()) / "proj"
    d.mkdir()
    p = d / name
    p.write_text("\n".join(json.dumps(l) for l in lines))
    return p


def cc_user(text, uuid, ts="2026-11-01T10:00:00Z"):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": text}}


def cc_tool(name, path, uuid, ts="2026-11-01T10:00:05Z"):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts,
            "message": {"content": [{"type": "tool_use", "name": name, "input": {"file_path": path}}]}}


class Adapters(unittest.TestCase):
    def test_claude_code_skips_injected_and_dedupes_resumed_copies(self):
        p = _write([
            cc_user("<system-reminder>x</system-reminder>fix the parser", "u1"),
            cc_tool("Edit", "/repo/parser.py", "a1"),
            cc_user("<bash-input>ls</bash-input>", "u2"),
            cc_user("ok", "u3"),
        ])
        # a resumed session: same message copied into a second file with a new uuid
        (p.parent / "s2.jsonl").write_text(json.dumps(cc_user("<system-reminder>x</system-reminder>fix the parser", "u9")))
        sessions = list(claude_code(str(p.parent)))
        texts = [t.text for s in sessions for t in s.turns]
        self.assertEqual(texts, ["fix the parser", "ok"])
        self.assertEqual(sessions[0].turns[0].actions_after[0].target, "/repo/parser.py")

    def test_codex(self):
        p = _write([
            {"type": "session_meta", "timestamp": "2026-11-01T10:00:00Z", "payload": {"cwd": "/x/myrepo"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>e</environment_context>"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "add a test for empty input"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "arguments": "tests/test_x.py"}},
        ])
        s = list(codex(str(p)))[0]
        self.assertEqual(s.project, "myrepo")
        self.assertEqual([t.text for t in s.turns], ["add a test for empty input"])
        self.assertEqual(len(s.turns[0].actions_after), 1)

    def test_generic(self):
        p = _write([[{"role": "user", "content": "never commit to main"}, {"role": "assistant", "content": "ok"}]], "c.jsonl")
        s = list(generic(str(p)))[0]
        self.assertEqual(s.turns[0].text, "never commit to main")


class Classifier(unittest.TestCase):
    def tier(self, text, actions=()):
        t = HumanTurn("x", text)
        t.actions_after = list(actions)
        return classify(t)

    def test_tiers(self):
        self.assertEqual(self.tier("ok").tier, 4)
        self.assertEqual(self.tier("sí, sigue con la página 14 y sube a main").tier, 4)
        self.assertEqual(self.tier("de ahora en adelante nunca toques las migraciones").tier, 1)
        self.assertEqual(self.tier("add a test for the empty case, for example [] should return 0").tier, 2)
        self.assertEqual(self.tier("does this still hold after the refactor?").tier, 3)

    def test_persisted_rule_needs_the_person_to_ask(self):
        asked = self.tier("add to CLAUDE.md: never edit generated files", [AgentAction("tool", "Edit", "/r/CLAUDE.md")])
        self.assertTrue(asked.persisted)
        not_asked = self.tier("ok go on", [AgentAction("tool", "Edit", "/r/CLAUDE.md")])
        self.assertFalse(not_asked.persisted)

    def test_feedback_information(self):
        self.assertLess(feedback_type("no funciona")[1], feedback_type("falla porque el token caduca")[1])
        self.assertEqual(feedback_type("I LITERALLY SAID DONT TOUCH THAT FILE")[0], "restated")
        self.assertTrue(self.tier("I LITERALLY SAID DONT TOUCH THAT FILE").frustrated)
        self.assertFalse(self.tier("Major revision for ACM CHI IEEE TMC with THE GOLDEN THREAD").frustrated)


class Scoring(unittest.TestCase):
    def test_repeats_collapse_leverage_and_produce_a_finding(self):
        p = _write([cc_user("never edit the generated api client files", f"u{i}", f"2026-11-0{i+1}T10:00:00Z") for i in range(4)])
        res = analyze(list(claude_code(str(p.parent))))
        self.assertTrue(res.repeats)
        self.assertTrue(all(r.leverage == 1.0 for r in res.turns if r.repeat_of))
        self.assertIn("rules", res.findings[0]["title"])
        self.assertTrue(0 <= res.sips <= 100)


class Redaction(unittest.TestCase):
    def test_secrets(self):
        out = redact("el pass es hunter2, key sk-abcdefghijklmnop, mail a@b.co")
        for secret in ("hunter2", "sk-abcdefghijklmnop", "a@b.co"):
            self.assertNotIn(secret, out)


if __name__ == "__main__":
    unittest.main()


class Loop(unittest.TestCase):
    """The agent gets its track record back: at session start and when a prompt repeats a past correction."""
    def setUp(self):
        import subprocess, sys
        subprocess.run([sys.executable, "examples/make_examples.py"], check=True, capture_output=True)
        self.res = analyze(list(claude_code("examples/low-leverage")))

    def test_brief_lists_repeated_rule_once(self):
        from sips.brief import brief
        b = brief(self.res)
        self.assertIn("don't touch the migrations folder", b)
        self.assertEqual(b.count("“don't touch the migrations folder”"), 2)   # once as repeated, once as not persisted

    def test_hooks_answer_in_claude_code_format(self):
        import io, sys, contextlib
        from sips.brief import hook
        for event, payload in (("session-start", {"cwd": "/sips-test-repo"}), ("prompt", {"cwd": "/sips-test-repo", "prompt": "please don't touch the migrations folder"})):
            sys.stdin = io.StringIO(json.dumps(payload))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hook(event, lambda cwd: self.res)
            sys.stdin = sys.__stdin__
            msg = json.loads(out.getvalue())["hookSpecificOutput"]
            self.assertEqual(msg["hookEventName"], "SessionStart" if event == "session-start" else "UserPromptSubmit")
            self.assertTrue(msg["additionalContext"])


class Repo(unittest.TestCase):
    """Static repo scoring: designed loops score high, step-by-step approval scores low, and nothing is executed."""
    @classmethod
    def setUpClass(cls):
        import subprocess, sys
        subprocess.run([sys.executable, "examples/make_loop_examples.py"], check=True, capture_output=True)

    def test_designed_loop_beats_approve_every_step(self):
        from sips.repo import scan
        good, bad = scan("examples/loops/designed-loop"), scan("examples/loops/approve-every-step")
        self.assertGreater(good.sips, bad.sips + 30)
        self.assertTrue(any(h.signal == "escalation" for h in good.hits))
        self.assertTrue(any(h.signal == "per-step-approval" for h in bad.hits))
        self.assertIn("escalation", bad.findings[0]["title"].lower())

    def test_ui_copy_is_not_counted(self):
        from sips.repo import _in_string, _prose
        self.assertTrue(_in_string('label = "Dashboard and metrics"', 12))
        self.assertTrue(_prose("We monitor every metric so the team can review decisions daily"))

    def test_askf1(self):
        from sips.repo import askf1
        r = askf1([{"blocker": True, "asked": True}, {"blocker": True, "asked": False}, {"blocker": False, "asked": True}])
        self.assertAlmostEqual(r["precision"], 0.5)
        self.assertAlmostEqual(r["recall"], 0.5)
        self.assertAlmostEqual(r["ask_f1"], 0.5)

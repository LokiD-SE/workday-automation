import base64
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ["WORKDAY_NO_NOTIFY"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workday import backup, gitflow, recorder, report, vpn, watch  # noqa: E402
from workday.config import CONFIG  # noqa: E402


class VpnProtocol(unittest.TestCase):
    def test_challenge_reply(self):
        reply = vpn.challenge_reply("p@ss", "123456")
        tag, pw, code = reply.split(":")
        self.assertEqual(tag, "SCRV1")
        self.assertEqual(base64.b64decode(pw).decode(), "p@ss")
        self.assertEqual(base64.b64decode(code).decode(), "123456")

    def test_quote_escapes(self):
        self.assertEqual(vpn.quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_parse_state(self):
        state = vpn.parse_state(">STATE:1696000000,CONNECTED,SUCCESS,10.8.0.6,1.2.3.4,1194,,")
        self.assertEqual((state["state"], state["ip"]), ("CONNECTED", "10.8.0.6"))
        self.assertIsNone(vpn.parse_state(">INFO:hello"))

    def test_agent_answers_static_challenge(self):
        agent = vpn.Agent("/nonexistent")
        sent = []
        agent.send = sent.append
        with mock.patch.object(vpn, "credentials", return_value=("lokesh", "secret")), \
             mock.patch.object(vpn, "ask_code", return_value="654321"):
            agent.handle(">HOLD:Waiting for hold release:0")
            agent.handle(">PASSWORD:Need 'Auth' username/password SC:1,Enter Authenticator Code")
        self.assertEqual(sent[0], "hold release")
        self.assertEqual(sent[1], 'username "Auth" "lokesh"')
        self.assertTrue(sent[2].startswith('password "Auth" "SCRV1:'))

    def test_agent_cancel_stops_vpn(self):
        agent = vpn.Agent("/nonexistent")
        sent = []
        agent.send = sent.append
        with mock.patch.object(vpn, "credentials", return_value=("u", "p")), \
             mock.patch.object(vpn, "ask_code", return_value=None):
            agent.handle(">PASSWORD:Need 'Auth' username/password SC:1,Enter Authenticator Code")
        self.assertEqual(sent, ["signal SIGTERM"])


class Watch(unittest.TestCase):
    def test_diff(self):
        old = {"PR1-T1": {"kind": "task", "name": "a", "status": "Open"},
               "PR1-T2": {"kind": "task", "name": "b", "status": "Open"}}
        new = {"PR1-T1": {"kind": "task", "name": "a", "status": "Dev Done"},
               "PR1-T3": {"kind": "task", "name": "c", "status": "Open"}}
        events = watch.diff_items(old, new, first_run=False)
        titles = [t for _, t, _ in events]
        self.assertIn("PR1-T1: Open → Dev Done - ready for QA", titles)
        self.assertIn("New task assigned: PR1-T3", titles)
        self.assertIn("PR1-T2 closed / unassigned", titles)
        self.assertEqual(events[0][0], "critical")
        self.assertEqual(watch.diff_items(old, new, first_run=True), [])

    def test_key_pattern_is_anchored(self):
        pattern = watch.key_pattern(["PR1-T100", "PR1-T1000"])
        self.assertEqual(pattern.findall("Merged in PR1-T1000 (pull request #1)"), ["PR1-T1000"])
        self.assertEqual(pattern.findall("PR1-T100_fix"), ["PR1-T100"])
        self.assertEqual(pattern.findall("XPR1-T100"), [])


class Report(unittest.TestCase):
    def test_run_summary_counts_only_that_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp, "PR1-T9_X", "Execution_2026-10-02")
            run_dir.mkdir(parents=True)
            (run_dir / "RUN_STATE.json").write_text(json.dumps({
                "TC_1": {"status": "Passed", "ts": "2026-10-02 10:00:00"},
                "TC_2": {"status": "Failed", "ts": "2026-10-02 11:00:00", "defect": "DEF-01"},
                "TC_3": {"status": "Blocked", "ts": "2026-10-01 11:00:00"},
                "note": "free text",
            }))
            summary = report.run_summary(run_dir / "RUN_STATE.json", date(2026, 10, 2))
            self.assertEqual(summary["today"], {"Pass": 1, "Fail": 1})
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary["defects"], ["DEF-01"])
            self.assertIsNone(report.run_summary(run_dir / "RUN_STATE.json", date(2026, 9, 30)))

    def test_short_subject(self):
        self.assertEqual(report._short("#Release20260903 - Fix X [ai:heavy] [tool:claude]"), "Fix X")


class Recorder(unittest.TestCase):
    def test_newly_failed(self):
        self.assertEqual(recorder.newly_failed({"a": "Pass", "b": "Fail"}, {"a": "Fail", "b": "Fail", "c": "Fail"}),
                         ["a", "c"])

    def test_active_run_respects_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp, "T1", "Execution_1"); old.mkdir(parents=True)
            new = Path(tmp, "T2", "Execution_1"); new.mkdir(parents=True)
            (old / "RUN_STATE.json").write_text("{}")
            (new / "RUN_STATE.json").write_text("{}")
            os.utime(old / "RUN_STATE.json", (time.time() - 3600, time.time() - 3600))
            self.assertEqual(recorder.active_run(Path(tmp), 15), new)
            os.utime(new / "RUN_STATE.json", (time.time() - 3600, time.time() - 3600))
            self.assertIsNone(recorder.active_run(Path(tmp), 15))


class Backup(unittest.TestCase):
    def test_plan_stale_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            tpr = Path(tmp, "TPR"); tpr.mkdir()
            old = tpr / "PR1-T1_Old"; old.mkdir(); (old / "a.csv").write_text("x")
            fresh = tpr / "PR1-T2_New"; fresh.mkdir(); (fresh / "b.csv").write_text("y")
            mixed = tpr / "PR1-T3_Mixed"; mixed.mkdir(); (mixed / "old.csv").write_text("z"); (mixed / "new.csv").write_text("w")
            twenty_days = time.time() - 20 * 86400
            for path in (old / "a.csv", old, mixed / "old.csv"):
                os.utime(path, (twenty_days, twenty_days))
            apks = []
            for i in range(4):
                apk = Path(tmp, f"bizom_{i}.apk"); apk.write_text("apk")
                stamp = twenty_days + i
                os.utime(apk, (stamp, stamp)); apks.append(apk)
            dump = Path(tmp, "java_error.hprof"); dump.write_text("dump")
            os.utime(dump, (twenty_days, twenty_days))
            cfg = {**CONFIG["backup"], "folders": {"TPR": [str(tpr), "TestPlanReport"]},
                   "apk_glob": str(Path(tmp, "bizom*.apk")), "crash_globs": [str(Path(tmp, "*.hprof"))]}
            with mock.patch.dict(CONFIG, {"backup": cfg}):
                plan = backup.build_plan()
            stale = {Path(u.path).name for u in plan.to_delete}
            self.assertEqual(stale, {"PR1-T1_Old", "bizom_0.apk", "bizom_1.apk"})
            self.assertEqual([Path(c[0]).name for c in plan.crash], ["java_error.hprof"])
            self.assertTrue(all(u.remote.startswith("gdrive:Bizom-QA-Backup/") for u in plan.units))


class Git(unittest.TestCase):
    def test_secret_filter(self):
        for name in (".env", "graph/.env.local", "client.ovpn", "keys/server.pem", ".git-credentials"):
            self.assertTrue(gitflow.SECRET_RE.search(name), name)
        for name in ("graph/env_loader.py", "README.md", "tests/test_env.py"):
            self.assertFalse(gitflow.SECRET_RE.search(name), name)

    def test_prefix(self):
        self.assertEqual(gitflow.PREFIX_RE.match("#Release20260903 - x").group(1), "#Release20260903 - ")


if __name__ == "__main__":
    unittest.main()

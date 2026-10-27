"""Coverage-only quiet-policy checks with an asserted source or installed target.

No browser, systemd or Telegram process is started. Real SQLite persistence is used.
Source is the default; GUARDIAN_QUIET_TEST_TARGET=installed checks this Python's
installed package without adding the repository library to the import path.
"""

import json
import os
import sqlite3
import tempfile
import unittest
import sys
import sysconfig
from pathlib import Path
from unittest.mock import patch

TEST_TARGET = os.environ.get("GUARDIAN_QUIET_TEST_TARGET", "source")
if TEST_TARGET == "source":
    PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "lib"
    sys.path.insert(0, str(PACKAGE_ROOT))
elif TEST_TARGET == "installed":
    PACKAGE_ROOT = Path(sysconfig.get_path("purelib"))
else:
    raise RuntimeError("GUARDIAN_QUIET_TEST_TARGET must be source or installed")

from agenttools_chatgpt import coverage

EXPECTED_COVERAGE = (PACKAGE_ROOT / "agenttools_chatgpt/coverage.py").resolve()
ACTUAL_COVERAGE = Path(coverage.__file__).resolve()
if ACTUAL_COVERAGE != EXPECTED_COVERAGE:
    raise RuntimeError(f"{TEST_TARGET} coverage target mismatch: {ACTUAL_COVERAGE} != {EXPECTED_COVERAGE}")

from agenttools_chatgpt.core import Store
from agenttools_chatgpt.monitor import MonitorStore, project_snapshot, read_source
from agenttools_chatgpt.startup import Browser, Listener, profile_path

NOW = 1793115761.0
CHAT = "https://chatgpt.com/c/quiet-policy-example"


class LocalCoverage(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.state = self.home / "guardian.sqlite"
        self.store = Store(self.state, account="home")
        self.pages = [{"type": "page", "id": "page-1", "url": CHAT}]
        self.active = "active"
        self.started = []

    def browsers(self):
        return [Browser(123, ("chrome", "--user-data-dir=" + str(profile_path("home", self.home)),
                              "--remote-debugging-port=9226"))]

    def listeners(self, port):
        self.assertEqual(9226, port)
        return [Listener("127.0.0.1", 123)]

    def show(self, unit):
        return {"ActiveState": self.active, "SubState": "x"}

    def start(self, unit):
        self.started.append(unit)

    def never_notify(self, _text):
        self.fail("local coverage attempted to notify the owner")

    def evidence(self, now):
        controls, attempts = read_source(self.state, "home")
        snapshot = project_snapshot("home", controls, attempts, [{
            "chat": CHAT, "checked_at": now, "status": "working", "running": True,
            "progress_token": "progress", "turn": "turn-1",
        }], None, {"connection": "connected", "heartbeat_at": now}, now)
        MonitorStore(self.state, "home").record(snapshot)

    def run_coverage(self, now, **flags):
        test = self
        class DevTools:
            def pages(self, timeout):
                return test.pages

            def create(self, url, timeout):
                test.fail("unexpected browser mutation in this scenario")

        return coverage.reconcile("home", state=self.state, port=9226, apply=True,
            config=coverage.Config(coverage_enabled=True, **flags), processes=self,
            devtools=DevTools(), systemd=self, notify=self.never_notify, now=now,
            home=self.home)

    def test_faults_remain_available_and_delivery_history_is_retained(self):
        self.store.enroll(CHAT)
        self.store.set_control("stop:*", "owner pause")
        self.active = "failed"
        ledger = coverage.Ledger(coverage.ledger_path(self.state), "home")
        records = {
            "old-unknown": ("service-failed", "delivery-failed", "TimeoutExpired", 1),
            "old-sent": ("stale-observation", "sent", "OK", 1),
        }
        ledger.settle_alerts(NOW, set(records), set(), records)
        before = ledger.alert_rows()
        for offset in (0, 1801, 86401):
            report = self.run_coverage(NOW + offset, recovery_autostart=True)
            self.assertEqual([], report["plan"]["alerts"])
            self.assertEqual([], report["effects"]["alerts"])
            self.assertTrue({"service-failed", "recovery-hold", "stale-observation"}
                            <= {item["kind"] for item in report["diagnostics"]})
        self.assertEqual(before, ledger.alert_rows())
        self.assertEqual([], self.started)
        self.assertEqual("owner pause", read_source(self.state, "home")[0]["stop:*"])

    def test_auto_enrollment_and_failed_service_repair_still_work(self):
        self.evidence(NOW)
        self.active = "failed"
        report = self.run_coverage(NOW, auto_watch_active=True, recovery_autostart=True)
        self.assertEqual([CHAT.rsplit("/", 1)[1]], report["effects"]["enrolled"])
        self.assertEqual("1", read_source(self.state, "home")[0]["watch:" + CHAT])
        self.assertEqual({"outcome": "started"}, report["effects"]["autostart"])
        ledger = coverage.Ledger(coverage.ledger_path(self.state), "home")
        self.assertEqual([(NOW, "started")], ledger.autostart_rows())
        self.assertEqual([], report["effects"]["alerts"])
        self.assertTrue(report["diagnostics"])

    def test_malformed_legacy_alert_table_does_not_disable_safe_repairs(self):
        self.evidence(NOW)
        self.active = "failed"
        ledger = coverage.Ledger(coverage.ledger_path(self.state), "home")
        ledger.record_autostart(NOW - 1000, "failed")
        with sqlite3.connect(ledger.path) as db:
            db.execute("DROP TABLE alerts")
            db.execute("CREATE TABLE alerts(key TEXT PRIMARY KEY, legacy_receipt TEXT)")
            db.execute("INSERT INTO alerts VALUES ('old-unknown', 'unconfirmed receipt')")
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='alerts'").fetchone()
            records = db.execute("SELECT * FROM alerts").fetchall()

        report = self.run_coverage(NOW, auto_watch_active=True, recovery_autostart=True)

        self.assertEqual([], report["blocked"])
        self.assertEqual(["quiet-policy-example"], report["effects"]["enrolled"])
        self.assertEqual("1", read_source(self.state, "home")[0]["watch:" + CHAT])
        self.assertEqual({"outcome": "started"}, report["effects"]["autostart"])
        self.assertEqual([(NOW - 1000, "failed"), (NOW, "started")], ledger.autostart_rows())
        with sqlite3.connect(ledger.path) as db:
            self.assertEqual(schema, db.execute("SELECT sql FROM sqlite_master WHERE name='alerts'").fetchone())
            self.assertEqual(records, db.execute("SELECT * FROM alerts").fetchall())

    def test_early_failure_report_keeps_the_same_schema(self):
        report = self.run_coverage(NOW)
        broken = self.home / "missing.sqlite"
        self.state = broken
        early = self.run_coverage(NOW)
        self.assertEqual(set(report), set(early))
        self.assertEqual([], early["diagnostics"])
        self.assertEqual("coordinator-only", early["notification_mode"])


    def test_compatibility_sender_never_starts_a_process(self):
        with patch.object(coverage.subprocess, "run") as process:
            result = coverage.tg_notifier("Guardian: observation is stale")
        process.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("local-only", result.detail)

    def test_legacy_and_explicit_configs_remain_quiet(self):
        path = self.home / "coverage-config.json"
        for mode in (None, "coordinator-only"):
            config = {"workspace": "home", "coverage_enabled": True}
            if mode:
                config["notification_mode"] = mode
            path.write_text(json.dumps(config))
            loaded = coverage.load_config("home", path)
            self.assertTrue(loaded.coverage_enabled)
            self.assertEqual("coordinator-only", loaded.notification_mode)
        for invalid in ("telegram", None, True, [], {}):
            with self.subTest(notification_mode=invalid):
                path.write_text(json.dumps({"workspace": "home", "notification_mode": invalid}))
                with self.assertRaises(ValueError):
                    coverage.load_config("home", path)


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from unittest import mock

import db
import monitor


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = db.DATA_DIR
        self.old_db_path = db.DB_PATH
        db.DATA_DIR = self.tempdir.name
        db.DB_PATH = os.path.join(self.tempdir.name, "traffic.db")
        db.init_db()
        ok, self.group_id = db.create_group("group", 1)
        self.assertTrue(ok)
        db.add_vm_to_group(
            self.group_id, 100, "qemu", "vm",
            initial_in_mb=2,
        )

    def tearDown(self):
        db.DATA_DIR = self.old_data_dir
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_shutdown_is_not_mistaken_for_restart_while_still_running(self):
        db.record_shutdown_request(100, "qemu", 1000)
        status = {"status": "running", "uptime": 100}
        with mock.patch.object(monitor.pve, "get_vm_status", return_value=status), \
             mock.patch.object(monitor.pve, "get_vm_boot_time", return_value=1000):
            self.assertFalse(monitor.check_auto_reset(100, "qemu"))

        summary = db.get_vm_traffic_summary(100, "qemu", self.group_id)
        self.assertEqual(summary["total_in_mb"], 2)
        self.assertIsNotNone(db.get_shutdown_state(100, "qemu"))

    def test_restart_resets_only_after_boot_identity_changes(self):
        db.record_shutdown_request(100, "qemu", 1000)
        status = {"status": "running", "uptime": 5}
        with mock.patch.object(monitor.pve, "get_vm_status", return_value=status), \
             mock.patch.object(monitor.pve, "get_vm_boot_time", return_value=2000):
            self.assertTrue(monitor.check_auto_reset(100, "qemu"))

        summary = db.get_vm_traffic_summary(100, "qemu", self.group_id)
        self.assertEqual(summary["total_in_mb"], 0)
        self.assertIsNone(db.get_shutdown_state(100, "qemu"))

    def test_successful_shutdown_request_is_deduplicated(self):
        status = {"status": "running", "uptime": 100}
        with mock.patch.object(monitor.pve, "get_vm_status", return_value=status), \
             mock.patch.object(monitor.pve, "get_vm_boot_time", return_value=1000), \
             mock.patch.object(monitor.pve, "shutdown_vm", return_value=(True, "UPID")) as shutdown:
            first = monitor.check_and_shutdown(
                100, "qemu", "vm", self.group_id, "group", 1
            )
            second = monitor.check_and_shutdown(
                100, "qemu", "vm", self.group_id, "group", 1
            )

        self.assertTrue(first[0])
        self.assertFalse(second[0])
        shutdown.assert_called_once()

    def test_notify_placeholders_are_shell_escaped(self):
        with mock.patch.object(monitor.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "ok"
            monitor.execute_notify(
                "notify {vm_name} {group}", 100, "x; touch /tmp/bad",
                "g && false", 2, 1, "qemu"
            )
        command = run.call_args.args[0]
        self.assertIn("'x; touch /tmp/bad'", command)
        self.assertIn("'g && false'", command)

    def test_telegram_warning_is_sent_once_per_traffic_cycle(self):
        with mock.patch.object(
            monitor.telegram_service, "notifications_ready", return_value=True
        ), mock.patch.object(
            monitor.telegram_service, "send_message", return_value=(True, "ok")
        ) as send:
            first = monitor.check_usage_warning(
                100, "qemu", "vm", self.group_id, "group", 1
            )
            second = monitor.check_usage_warning(
                100, "qemu", "vm", self.group_id, "group", 1
            )

        self.assertTrue(first)
        self.assertFalse(second)
        send.assert_called_once()

    def test_shutdown_notification_is_sent_after_pve_accepts_request(self):
        status = {"status": "running", "uptime": 100}
        with mock.patch.object(
            monitor.telegram_service, "notifications_ready", return_value=True
        ), mock.patch.object(
            monitor.telegram_service, "send_message", return_value=(True, "ok")
        ) as send, mock.patch.object(
            monitor.pve, "get_vm_status", return_value=status
        ), mock.patch.object(
            monitor.pve, "get_vm_boot_time", return_value=1000
        ), mock.patch.object(
            monitor.pve, "shutdown_vm", return_value=(True, "UPID")
        ):
            monitor.check_and_shutdown(
                100, "qemu", "vm", self.group_id, "group", 1
            )
            monitor.check_and_shutdown(
                100, "qemu", "vm", self.group_id, "group", 1
            )

        send.assert_called_once()

    def test_failed_telegram_warning_is_retried(self):
        with mock.patch.object(
            monitor.telegram_service, "notifications_ready", return_value=True
        ), mock.patch.object(
            monitor.telegram_service, "send_message", return_value=(False, "timeout")
        ) as send:
            self.assertFalse(monitor.check_usage_warning(
                100, "qemu", "vm", self.group_id, "group", 1
            ))
            self.assertFalse(monitor.check_usage_warning(
                100, "qemu", "vm", self.group_id, "group", 1
            ))

        self.assertEqual(send.call_count, 2)

    def test_pending_shutdown_notification_retries_for_triggering_group(self):
        ok, second_group = db.create_group("second", 1)
        self.assertTrue(ok)
        db.add_vm_to_group(second_group, 100, "qemu", "vm", initial_in_mb=2)
        db.record_shutdown_request(100, "qemu", 1000, group_id=second_group)
        with mock.patch.object(
            monitor.telegram_service, "notifications_ready", return_value=True
        ), mock.patch.object(
            monitor.telegram_service, "send_message", return_value=(True, "ok")
        ) as send:
            monitor.check_and_shutdown(
                100, "qemu", "vm", self.group_id, "group", 1
            )
            monitor.check_and_shutdown(
                100, "qemu", "vm", second_group, "second", 1
            )

        send.assert_called_once()
        self.assertIn("second", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

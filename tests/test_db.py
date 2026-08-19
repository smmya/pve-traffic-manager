import os
import sqlite3
import tempfile
import unittest

import db


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = db.DATA_DIR
        self.old_db_path = db.DB_PATH
        db.DATA_DIR = self.tempdir.name
        db.DB_PATH = os.path.join(self.tempdir.name, "traffic.db")
        db.init_db()

    def tearDown(self):
        db.DATA_DIR = self.old_data_dir
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def create_group(self, name="group", limit=1024):
        ok, group_id = db.create_group(name, limit)
        self.assertTrue(ok)
        return group_id

    def test_counter_directions_reset_independently(self):
        group_id = self.create_group()
        self.assertTrue(db.add_vm_to_group(group_id, 100, "qemu")[0])

        self.assertEqual(db.record_traffic_sample(100, "qemu", 1000, 2000, 100), (0, 0))
        delta = db.record_traffic_sample(100, "qemu", 100, 2500, 100)

        self.assertEqual(delta, (100, 500))
        summary = db.get_vm_traffic_summary(100, "qemu", group_id)
        self.assertAlmostEqual(summary["total_in_mb"], 100 / (1024 * 1024))
        self.assertAlmostEqual(summary["total_out_mb"], 500 / (1024 * 1024))

    def test_boot_change_counts_current_counters_after_restart(self):
        group_id = self.create_group()
        db.add_vm_to_group(group_id, 100, "qemu")
        db.record_traffic_sample(100, "qemu", 100, 200, 1000)

        delta = db.record_traffic_sample(100, "qemu", 150, 260, 2000)

        self.assertEqual(delta, (150, 260))

    def test_adding_second_group_advances_existing_group_atomically(self):
        first = self.create_group("first")
        second = self.create_group("second")
        mb = 1024 * 1024
        ok, _ = db.add_vm_to_group(
            first, 100, "qemu", "vm-name",
            initial_in_mb=100 / mb,
            baseline_in_bytes=100,
            baseline_out_bytes=0,
            boot_time=1000,
        )
        self.assertTrue(ok)

        ok, _ = db.add_vm_to_group(
            second, 100, "qemu", "vm-name",
            initial_in_mb=160 / mb,
            baseline_in_bytes=160,
            baseline_out_bytes=0,
            boot_time=1000,
        )

        self.assertTrue(ok)
        self.assertAlmostEqual(
            db.get_vm_traffic_summary(100, "qemu", first)["total_in_mb"],
            160 / mb,
        )
        self.assertAlmostEqual(
            db.get_vm_traffic_summary(100, "qemu", second)["total_in_mb"],
            160 / mb,
        )
        self.assertEqual(db.get_last_traffic_log(100, "qemu")["netin_bytes"], 160)
        self.assertEqual(db.get_group_vms(second)[0]["vm_name"], "vm-name")

    def test_adding_another_group_refreshes_name_in_existing_groups(self):
        first = self.create_group("first")
        second = self.create_group("second")
        db.add_vm_to_group(first, 100, "qemu", "")

        db.add_vm_to_group(second, 100, "qemu", "current-name")

        self.assertEqual(db.get_group_vms(first)[0]["vm_name"], "current-name")
        self.assertEqual(db.get_all_managed_vms()[0]["vm_name"], "current-name")

    def test_latest_log_uses_id_when_timestamps_match(self):
        db.insert_traffic_log(100, "qemu", 1, 1, 0, 0)
        db.insert_traffic_log(100, "qemu", 2, 2, 1, 1)
        self.assertEqual(db.get_last_traffic_log(100, "qemu")["netin_bytes"], 2)

    def test_deleting_last_group_clears_pending_shutdown(self):
        group_id = self.create_group()
        db.add_vm_to_group(group_id, 100, "qemu")
        db.record_shutdown_request(100, "qemu", 1000)

        db.delete_group(group_id)

        self.assertIsNone(db.get_shutdown_state(100, "qemu"))

    def test_old_database_is_migrated_in_place(self):
        os.remove(db.DB_PATH)
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute(
            """CREATE TABLE traffic_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vm_id INTEGER NOT NULL,
                vm_type TEXT NOT NULL,
                netin_bytes INTEGER DEFAULT 0,
                netout_bytes INTEGER DEFAULT 0,
                delta_in_bytes INTEGER DEFAULT 0,
                delta_out_bytes INTEGER DEFAULT 0,
                timestamp TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE traffic_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vm_id INTEGER NOT NULL,
                vm_type TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                last_reset TEXT NOT NULL,
                total_in_mb REAL DEFAULT 0,
                total_out_mb REAL DEFAULT 0,
                UNIQUE(vm_id, vm_type, group_id)
            )"""
        )
        conn.commit()
        conn.close()

        db.init_db()

        conn = db.get_conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(traffic_logs)")}
        summary_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(traffic_summary)")
        }
        state_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shutdown_state'"
        ).fetchone()
        shutdown_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(shutdown_state)")
        }
        telegram_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_settings'"
        ).fetchone()
        conn.close()
        self.assertIn("boot_time", columns)
        self.assertIn("warning_sent", summary_columns)
        self.assertIn("shutdown_notified", summary_columns)
        self.assertIsNotNone(state_table)
        self.assertIn("group_id", shutdown_columns)
        self.assertIsNotNone(telegram_table)

    def test_auto_restart_resets_only_target_vm_in_same_group(self):
        group_id = self.create_group()
        db.add_vm_to_group(group_id, 100, "qemu", "target", initial_in_mb=900)
        db.add_vm_to_group(group_id, 101, "qemu", "other", initial_in_mb=700)
        db.record_shutdown_request(100, "qemu", 1000)

        groups = db.auto_reset_vm_traffic(100, "qemu", "restart")

        self.assertEqual([item["group_id"] for item in groups], [group_id])
        self.assertEqual(
            db.get_vm_traffic_summary(100, "qemu", group_id)["total_in_mb"], 0
        )
        self.assertEqual(
            db.get_vm_traffic_summary(101, "qemu", group_id)["total_in_mb"], 700
        )
        self.assertIsNone(db.get_shutdown_state(100, "qemu"))

    def test_notification_claim_is_once_per_reset_cycle(self):
        group_id = self.create_group()
        db.add_vm_to_group(group_id, 100, "qemu", initial_in_mb=900)

        self.assertTrue(db.claim_traffic_notification(100, "qemu", group_id, "warning"))
        self.assertFalse(db.claim_traffic_notification(100, "qemu", group_id, "warning"))
        db.reset_vm_traffic(100, "qemu", group_id)
        self.assertTrue(db.claim_traffic_notification(100, "qemu", group_id, "warning"))

    def test_telegram_settings_validate_chat_and_warning_percent(self):
        self.assertFalse(db.update_telegram_settings(chat_id="not-an-id")[0])
        self.assertFalse(db.update_telegram_settings(warning_percent=100)[0])
        self.assertFalse(db.update_telegram_settings(enabled=True)[0])
        self.assertTrue(db.update_telegram_settings(
            bot_token="123:secret", chat_id="-100123", enabled=True,
            warning_percent=85,
        )[0])
        settings = db.get_telegram_settings()
        self.assertEqual(settings["bot_token"], "123:secret")
        self.assertEqual(settings["chat_id"], "-100123")
        self.assertEqual(settings["enabled"], 1)
        self.assertEqual(settings["warning_percent"], 85)
        self.assertTrue(db.update_telegram_settings(chat_id="")[0])
        self.assertEqual(db.get_telegram_settings()["enabled"], 0)


if __name__ == "__main__":
    unittest.main()

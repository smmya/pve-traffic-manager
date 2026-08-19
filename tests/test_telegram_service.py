import os
import tempfile
import unittest
from unittest import mock

import db
import telegram_service


class TelegramServiceTestCase(unittest.TestCase):
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

    def test_token_is_masked(self):
        masked = telegram_service.mask_token("123456:abcdef-secret")
        self.assertTrue(masked.startswith("123456"))
        self.assertTrue(masked.endswith("cret"))
        self.assertNotIn("abcdef-secret", masked)

    def test_startup_diagnosis_reports_unconfigured_token_without_api_call(self):
        with mock.patch.object(
            telegram_service, "check_network", return_value=(True, "ok")
        ), mock.patch.object(
            telegram_service, "dependency_status", return_value=(True, "22.8")
        ), mock.patch.object(telegram_service, "_probe_bot") as probe:
            result = telegram_service.diagnose_startup()

        self.assertTrue(result["network_ok"])
        self.assertIsNone(result["token_ok"])
        probe.assert_not_called()

    def test_send_message_does_not_send_when_notifications_disabled(self):
        db.update_telegram_settings(bot_token="123:secret", chat_id="1")
        with mock.patch.object(telegram_service, "_send") as send:
            ok, detail = telegram_service.send_message("test")
        self.assertFalse(ok)
        self.assertIn("未启用", detail)
        send.assert_not_called()

    def test_safe_error_redacts_token(self):
        token = "123456:very-secret"
        message = telegram_service._safe_error(RuntimeError(f"bad {token}"), token)
        self.assertNotIn(token, message)
        self.assertIn("已隐藏", message)

    def test_dependency_install_targets_project_private_directory(self):
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(
            telegram_service.subprocess, "run", return_value=completed
        ) as run:
            ok, _ = telegram_service.install_dependency()
        self.assertTrue(ok)
        command = run.call_args.args[0]
        self.assertIn("--target", command)
        self.assertIn(telegram_service.PYTHON_PACKAGES_DIR, command)

    def test_systemd_working_directory_is_unquoted_absolute_path(self):
        unit = telegram_service.build_bot_service_unit(
            "/root/pve-traffic-manager",
            "/usr/bin/python3",
            "/root/pve-traffic-manager/telegram_bot.py",
        )
        self.assertIn("WorkingDirectory=/root/pve-traffic-manager\n", unit)
        self.assertNotIn('WorkingDirectory="', unit)

    def test_systemd_working_directory_escapes_spaces(self):
        unit = telegram_service.build_bot_service_unit(
            "/root/pve traffic-manager",
            "/usr/bin/python3",
            "/root/pve traffic-manager/telegram_bot.py",
        )
        self.assertIn(r"WorkingDirectory=/root/pve\x20traffic-manager", unit)

    def test_systemd_working_directory_rejects_relative_path(self):
        with self.assertRaises(ValueError):
            telegram_service.build_bot_service_unit(
                "pve-traffic-manager", "python3", "telegram_bot.py"
            )


if __name__ == "__main__":
    unittest.main()

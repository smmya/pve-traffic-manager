import importlib.util
import os
import tempfile
import unittest
from unittest import mock

import db
import telegram_bot


class TelegramBotTestCase(unittest.TestCase):
    def test_reset_workflow_is_not_advertised_as_parameter_commands(self):
        visible_commands = {
            command for command, _ in telegram_bot.BOT_COMMAND_DEFINITIONS
        }
        self.assertNotIn("resetgroup", visible_commands)
        self.assertNotIn("resetvm", visible_commands)
        self.assertNotIn("/resetgroup", telegram_bot._help_text())
        self.assertNotIn("/resetvm", telegram_bot._help_text())
        self.assertIn("按钮", telegram_bot._help_text())

    @unittest.skipUnless(
        importlib.util.find_spec("telegram"),
        "python-telegram-bot is not installed in the local test runtime",
    )
    def test_ptb_22_application_builds_without_network(self):
        application = telegram_bot.build_application(
            "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        )
        self.assertGreaterEqual(len(application.handlers[0]), 10)
        self.assertIsNotNone(application.error_handlers)

    @unittest.skipUnless(
        importlib.util.find_spec("telegram"),
        "python-telegram-bot is not installed in the local test runtime",
    )
    def test_main_menu_has_no_collect_button_and_subpages_have_back(self):
        main_callbacks = {
            button.callback_data
            for row in telegram_bot._menu_markup().inline_keyboard
            for button in row
        }
        self.assertNotIn("menu:collect", main_callbacks)
        self.assertIn("reset:menu", main_callbacks)
        reset_callbacks = {
            button.callback_data
            for row in telegram_bot._reset_menu_markup().inline_keyboard
            for button in row
        }
        self.assertIn("reset:groups", reset_callbacks)
        self.assertIn("reset:vms", reset_callbacks)
        self.assertIn("menu:main", reset_callbacks)
        status_callbacks = {
            button.callback_data
            for row in telegram_bot._status_markup().inline_keyboard
            for button in row
        }
        self.assertIn("menu:main", status_callbacks)


class NetworkMonitorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = db.DATA_DIR
        self.old_db_path = db.DB_PATH
        db.DATA_DIR = self.tempdir.name
        db.DB_PATH = os.path.join(self.tempdir.name, "traffic.db")
        db.init_db()
        db.update_telegram_settings(bot_token="123:secret", chat_id="6180442847")
        db.update_network_check_settings(targets="1.1.1.1;8.8.8.8", enabled=True)

    def tearDown(self):
        db.DATA_DIR = self.old_data_dir
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def test_cycle_checks_only_running_lxc_with_30_second_spacing(self):
        bot = mock.AsyncMock()
        containers = [
            {"vmid": 100, "name": "bad", "status": "running", "type": "lxc"},
            {"vmid": 101, "name": "stopped", "status": "stopped", "type": "lxc"},
            {"vmid": 102, "name": "good", "status": "running", "type": "lxc"},
        ]
        sleep = mock.AsyncMock()
        with mock.patch.object(
            telegram_bot.pve, "get_all_lxc_vms", return_value=containers
        ), mock.patch.object(
            telegram_bot.pve, "ping_from_lxc",
            side_effect=[(False, "timeout"), (True, "3 received")],
        ) as ping, mock.patch.object(
            telegram_bot.random, "choice", side_effect=["1.1.1.1", "8.8.8.8"]
        ), mock.patch.object(telegram_bot.asyncio, "sleep", sleep):
            result = await telegram_bot.run_network_check_cycle(bot, force=True)

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(ping.call_count, 2)
        sleep.assert_awaited_once_with(30)
        bot.send_message.assert_awaited_once()
        self.assertIn("3 次 ping 均未成功", bot.send_message.call_args.kwargs["text"])

    async def test_successful_ping_does_not_notify(self):
        bot = mock.AsyncMock()
        with mock.patch.object(
            telegram_bot.pve, "get_all_lxc_vms",
            return_value=[{"vmid": 100, "name": "ok", "status": "running"}],
        ), mock.patch.object(
            telegram_bot.pve, "ping_from_lxc", return_value=(True, "ok")
        ):
            result = await telegram_bot.run_network_check_cycle(bot, force=True)

        self.assertEqual(result["failed"], 0)
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

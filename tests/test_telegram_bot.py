import importlib.util
import unittest

import telegram_bot


class TelegramBotTestCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

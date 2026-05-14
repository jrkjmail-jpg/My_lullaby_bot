import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot


class DatabaseHelpersTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = bot.DB_PATH
        bot.DB_PATH = f"{self.temp_dir.name}/test.db"
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_nuts_balance_cannot_go_below_zero(self):
        user = SimpleNamespace(id=1001, username="test_user")
        bot.create_user_if_not_exists(user)

        self.assertEqual(bot.get_nuts(user.id), 0)
        self.assertFalse(bot.remove_nuts(user.id, 1))

        bot.add_nuts(user.id, 2)

        self.assertEqual(bot.get_nuts(user.id), 2)
        self.assertTrue(bot.remove_nuts(user.id, 1))
        self.assertEqual(bot.get_nuts(user.id), 1)

        self.assertFalse(bot.remove_nuts(user.id, 2))
        self.assertEqual(bot.get_nuts(user.id), 1)

    def test_paid_order_is_credited_once(self):
        user = SimpleNamespace(id=1002, username="buyer")
        bot.create_user_if_not_exists(user)

        local_payment_id = bot.create_local_payment_order(
            user.id,
            "🌰 Купить 2 орешка",
            "buyer@example.com",
        )
        bot.update_payment_order(local_payment_id, "yk_payment_1", "pending", "https://pay.test")

        order, credited_now = bot.credit_payment_if_needed(local_payment_id, "yk_payment_1")

        self.assertTrue(credited_now)
        self.assertEqual(order, {"user_id": user.id, "nuts": 2})
        self.assertEqual(bot.get_nuts(user.id), 2)

        order, credited_now = bot.credit_payment_if_needed(local_payment_id, "yk_payment_1")

        self.assertFalse(credited_now)
        self.assertEqual(order, {"user_id": user.id, "nuts": 2})
        self.assertEqual(bot.get_nuts(user.id), 2)

    def test_yookassa_webhook_credits_paid_order_once(self):
        user = SimpleNamespace(id=1003, username="webhook_buyer")
        bot.create_user_if_not_exists(user)

        local_payment_id = bot.create_local_payment_order(
            user.id,
            "🌰 Купить 3 орешка",
            "webhook@example.com",
        )
        bot.update_payment_order(local_payment_id, "yk_payment_2", "pending", "https://pay.test")

        payload = {
            "type": "notification",
            "event": "payment.succeeded",
            "object": {
                "id": "yk_payment_2",
                "status": "succeeded",
                "paid": True,
                "metadata": {
                    "local_payment_id": local_payment_id,
                    "user_id": str(user.id),
                    "nuts": "3",
                },
            },
        }

        with patch("bot.get_yookassa_payment") as get_yookassa_payment:
            get_yookassa_payment.return_value = {
                "id": "yk_payment_2",
                "status": "succeeded",
                "paid": True,
            }

            result = bot.process_yookassa_webhook(payload)
            repeated_result = bot.process_yookassa_webhook(payload)

        self.assertEqual(result["action"], "credited")
        self.assertEqual(repeated_result["action"], "already_credited")
        self.assertEqual(bot.get_nuts(user.id), 3)

    def test_child_safety_allows_good_ambiguous_characters(self):
        safe, message = bot.validate_child_safe_text("Алладин и добрый джин")

        self.assertTrue(safe)
        self.assertEqual(message, "")

    def test_child_safety_blocks_clear_adult_topics(self):
        safe, message = bot.validate_child_safe_text("песня про алкоголь и казино")

        self.assertFalse(safe)
        self.assertIn("не подходит", message)

    def test_name_phonetics_preserves_display_name_and_singing_hint(self):
        plain_name, stressed_name, error = bot.make_stressed_name("МарсЭль")

        self.assertEqual(error, "")
        self.assertEqual(plain_name, "Марсель")
        self.assertEqual(stressed_name, "Марсэ́ль")

    def test_name_phonetics_keeps_initial_eh(self):
        plain_name, stressed_name, error = bot.make_stressed_name("Эмма")

        self.assertEqual(error, "")
        self.assertEqual(plain_name, "Эмма")
        self.assertEqual(stressed_name, "Э́мма")


if __name__ == "__main__":
    unittest.main()

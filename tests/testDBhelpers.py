import os
import shutil
import sqlite3
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

    def test_create_user_does_not_reset_existing_nuts(self):
        user = SimpleNamespace(id=1010, username="first_name")
        bot.create_user_if_not_exists(user)
        bot.add_nuts(user.id, 3)

        same_user = SimpleNamespace(id=1010, username="new_name")
        bot.create_user_if_not_exists(same_user)

        self.assertEqual(bot.get_nuts(user.id), 3)

    def test_database_backup_copy_preserves_balances(self):
        user = SimpleNamespace(id=1011, username="backup_user")
        bot.create_user_if_not_exists(user)
        bot.add_nuts(user.id, 7)

        backup_path, backup_dir = bot.create_database_backup_copy()

        try:
            ok, error = bot.validate_sqlite_backup_file(backup_path)
            self.assertTrue(ok, error)

            conn = sqlite3.connect(backup_path)
            try:
                nuts = conn.execute(
                    "SELECT nuts FROM users WHERE user_id = ?",
                    (user.id,),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(nuts, 7)
        finally:
            shutil.rmtree(backup_dir, ignore_errors=True)

    def test_restore_database_from_backup_file(self):
        user = SimpleNamespace(id=1012, username="restore_user")
        bot.create_user_if_not_exists(user)
        bot.add_nuts(user.id, 5)
        backup_path, backup_dir = bot.create_database_backup_copy()

        try:
            bot.remove_nuts(user.id, 5)
            self.assertEqual(bot.get_nuts(user.id), 0)

            previous_backup_path = bot.replace_database_with_backup_file(backup_path)

            self.assertEqual(bot.get_nuts(user.id), 5)
            self.assertTrue(os.path.exists(previous_backup_path))
        finally:
            shutil.rmtree(backup_dir, ignore_errors=True)

    def test_auto_backup_interval_limits_repeated_backups(self):
        original_interval = bot.AUTO_DB_BACKUP_INTERVAL_HOURS
        bot.AUTO_DB_BACKUP_INTERVAL_HOURS = 6

        try:
            self.assertTrue(bot.should_send_auto_db_backup())

            bot.mark_auto_backup_sent()

            self.assertFalse(bot.should_send_auto_db_backup())
        finally:
            bot.AUTO_DB_BACKUP_INTERVAL_HOURS = original_interval

    def test_support_keywords_escalate_to_admin(self):
        self.assertTrue(bot.support_needs_admin_by_keywords("Я оплатил, но орешки не пришли"))
        self.assertTrue(bot.support_needs_admin_by_keywords("Можно позвать оператора?"))
        self.assertFalse(bot.support_needs_admin_by_keywords("Как создать новую колыбельную?"))

    def test_yookassa_secret_kind_hides_secret_value(self):
        original_secret = bot.YOOKASSA_SECRET_KEY

        try:
            bot.YOOKASSA_SECRET_KEY = "test_secret_value"
            self.assertEqual(bot.get_yookassa_secret_kind(), "похоже тестовый")

            bot.YOOKASSA_SECRET_KEY = "live_secret_value"
            self.assertEqual(bot.get_yookassa_secret_kind(), "похоже боевой")
        finally:
            bot.YOOKASSA_SECRET_KEY = original_secret

    def test_parse_age_allows_up_to_119_years(self):
        self.assertEqual(bot.parse_age("119")["display"], "119 лет")
        self.assertEqual(bot.parse_age("21")["display"], "21 год")
        self.assertEqual(bot.parse_age("22")["display"], "22 года")
        self.assertEqual(bot.parse_age("5,1")["display"], "5 лет 1 месяц")

        self.assertIsNone(bot.parse_age("119,1"))
        self.assertIsNone(bot.parse_age("120"))

    def test_commands_text_includes_admin_commands(self):
        commands_text = bot.build_commands_text()

        self.assertIn("/commands", commands_text)
        self.assertIn("/addnuts user_id количество", commands_text)
        self.assertIn("/paystatus", commands_text)
        self.assertIn("/supportchatid", commands_text)

    def test_support_messages_are_saved(self):
        user_id = 1013

        bot.log_support_message(user_id, "user", "Здравствуйте")
        bot.log_support_message(user_id, "ai", "Здравствуйте, чем помочь?")

        messages = bot.get_recent_support_messages(user_id)

        self.assertEqual(messages[-2]["sender"], "user")
        self.assertEqual(messages[-1]["sender"], "ai")

    def test_support_admin_message_mapping(self):
        bot.remember_support_admin_message(-100123, 55, 1014)

        self.assertEqual(bot.get_support_user_by_admin_message(-100123, 55), 1014)
        self.assertIsNone(bot.get_support_user_by_admin_message(-100123, 56))

    def test_last_support_user_for_admin_chat(self):
        bot.remember_support_admin_message(-100124, 10, 1015)
        bot.remember_support_admin_message(-100124, 11, 1016)

        self.assertEqual(bot.get_last_support_user_for_admin_chat(-100124), 1016)

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

    def test_yookassa_webhook_recovers_missing_local_order(self):
        user_id = 1004
        local_payment_id = "nuts_recovered"

        payload = {
            "type": "notification",
            "event": "payment.succeeded",
            "object": {
                "id": "yk_payment_recovered",
                "status": "succeeded",
                "paid": True,
            },
        }

        with patch("bot.get_yookassa_payment") as get_yookassa_payment:
            get_yookassa_payment.return_value = {
                "id": "yk_payment_recovered",
                "status": "succeeded",
                "paid": True,
                "amount": {
                    "value": "499.00",
                    "currency": "RUB",
                },
                "metadata": {
                    "local_payment_id": local_payment_id,
                    "user_id": str(user_id),
                    "nuts": "2",
                },
            }

            result = bot.process_yookassa_webhook(payload)
            repeated_result = bot.process_yookassa_webhook(payload)

        self.assertEqual(result["action"], "credited")
        self.assertEqual(repeated_result["action"], "already_credited")
        self.assertEqual(bot.get_nuts(user_id), 2)

    def test_yookassa_webhook_missing_order_does_not_raise(self):
        payload = {
            "type": "notification",
            "event": "payment.succeeded",
            "object": {
                "id": "yk_payment_without_metadata",
                "status": "succeeded",
                "paid": True,
            },
        }

        with patch("bot.get_yookassa_payment") as get_yookassa_payment:
            get_yookassa_payment.return_value = {
                "id": "yk_payment_without_metadata",
                "status": "succeeded",
                "paid": True,
            }

            result = bot.process_yookassa_webhook(payload)

        self.assertEqual(result["action"], "missing_order")
        self.assertEqual(result["yookassa_payment_id"], "yk_payment_without_metadata")

    def test_reminders_skip_recent_and_disabled_users(self):
        bot.create_user_id_if_not_exists(2001)
        bot.create_user_id_if_not_exists(2002)
        bot.create_user_id_if_not_exists(2003)
        bot.set_reminders_enabled(2003, False)

        with bot.db_connection() as conn:
            conn.execute("""
                UPDATE users
                SET last_seen_at = datetime('now', '-20 days')
                WHERE user_id IN (2001, 2003)
            """)
            conn.execute("""
                UPDATE users
                SET last_seen_at = CURRENT_TIMESTAMP
                WHERE user_id = 2002
            """)

        self.assertEqual(bot.get_users_for_reminder(), [2001])

        bot.mark_reminder_sent(2001)

        self.assertEqual(bot.get_users_for_reminder(), [])

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

    def test_make_genitive_name_uses_gender_for_soft_sign_names(self):
        self.assertEqual(bot.make_genitive_name("Марсель", "👦 Мальчик"), "Марселя")
        self.assertEqual(bot.make_genitive_name("Игорь", "👦 Мальчик"), "Игоря")
        self.assertEqual(bot.make_genitive_name("Алиса", "👧 Девочка"), "Алисы")
        self.assertEqual(bot.make_genitive_name("София", "👧 Девочка"), "Софии")


if __name__ == "__main__":
    unittest.main()

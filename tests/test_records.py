"""运行记录按全部有内容的日期归类。用临时库，不碰正在用的邮件。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import db_init


class RecordDays(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db_init.DB_PATH
        db_init.DB_PATH = Path(self.tmp.name) / "records.db"
        db_init.init_db()

    def tearDown(self):
        db_init.DB_PATH = self.old
        self.tmp.cleanup()

    def test_old_mail_stays_and_today_uses_the_task_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with db_init.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO mail_info (message_id, subject, received_at, usefulness, todo_items)
                VALUES ('old-mail', '旧信', '2026-01-02 10:00:00', 'useful', '去对接')
                """
            )
            mail_id = conn.execute(
                "SELECT id FROM mail_info WHERE message_id='old-mail'"
            ).fetchone()["id"]
            conn.execute(
                """
                INSERT INTO customer_task (
                    customer_name, scope_rule, source_mail_id, status, execute_at, created_at
                ) VALUES ('签约', '拿上合同', ?, 'pending', ?, '2026-01-02 09:00:00')
                """,
                (mail_id, today + " 14:25:00"),
            )
        from demo_ui import _records

        rows = {row["date"]: row for row in _records()}
        self.assertIn("2026-01-02", rows)
        self.assertEqual(rows["2026-01-02"]["mail_count"], 1)
        self.assertEqual(rows["2026-01-02"]["label"], "1月2日")
        self.assertIn(today, rows)
        self.assertEqual(rows[today]["label"], "今天")
        self.assertEqual(rows[today]["mail_count"], 0)
        self.assertEqual(rows[today]["task_count"], 1)
        self.assertEqual(rows[today]["tasks"][0]["mail_day"], "2026-01-02")
        self.assertEqual(rows[today]["tasks"][0]["source_mail_id"], mail_id)


if __name__ == "__main__":
    unittest.main()

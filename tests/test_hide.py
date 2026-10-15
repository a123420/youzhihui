"""单条拿掉和撤回。用临时库，不碰正在用的邮件。"""

import tempfile
import unittest
from pathlib import Path

import db_init


class HideScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db_init.DB_PATH
        db_init.DB_PATH = Path(self.tmp.name) / "hide.db"
        db_init.init_db()

    def tearDown(self):
        db_init.DB_PATH = self.old
        self.tmp.cleanup()

    def _pair(self, message_id: str) -> tuple[int, int]:
        with db_init.get_conn() as conn:
            conn.execute(
                "INSERT INTO mail_info (message_id, subject) VALUES (?, '一封信')",
                (message_id,),
            )
            mail_id = conn.execute(
                "SELECT id FROM mail_info WHERE message_id=?",
                (message_id,),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO customer_task (customer_name, source_mail_id, status) VALUES ('去做', ?, 'pending')",
                (mail_id,),
            )
            task_id = conn.execute(
                "SELECT id FROM customer_task WHERE source_mail_id=?",
                (mail_id,),
            ).fetchone()["id"]
        return int(mail_id), int(task_id)

    def _flags(self, mail_id: int, task_id: int) -> tuple[int, int, int]:
        with db_init.get_conn() as conn:
            mail = conn.execute("SELECT hidden FROM mail_info WHERE id=?", (mail_id,)).fetchone()
            task = conn.execute(
                "SELECT hidden, hide_with FROM customer_task WHERE id=?",
                (task_id,),
            ).fetchone()
        return int(mail["hidden"] or 0), int(task["hidden"] or 0), int(task["hide_with"] or 0)

    def test_undo_mail_restores_only_the_task_hidden_with_it(self):
        mail_id, task_id = self._pair("with-mail")
        self.assertTrue(db_init.delete_mail(mail_id))
        self.assertEqual(self._flags(mail_id, task_id), (1, 1, 2))
        self.assertTrue(db_init.undo_mail(mail_id))
        self.assertEqual(self._flags(mail_id, task_id), (0, 0, 0))

    def test_undo_mail_leaves_a_separately_hidden_task(self):
        mail_id, task_id = self._pair("own-task")
        self.assertTrue(db_init.delete_task(task_id))
        self.assertTrue(db_init.delete_mail(mail_id))
        self.assertTrue(db_init.undo_mail(mail_id))
        self.assertEqual(self._flags(mail_id, task_id), (0, 1, 1))
        self.assertTrue(db_init.undo_task(task_id))
        self.assertEqual(self._flags(mail_id, task_id), (0, 0, 0))

    def test_undo_task_does_not_pull_back_a_mail_hide(self):
        mail_id, task_id = self._pair("mail-only")
        self.assertTrue(db_init.delete_mail(mail_id))
        self.assertFalse(db_init.undo_task(task_id))
        self.assertEqual(self._flags(mail_id, task_id), (1, 1, 2))


if __name__ == "__main__":
    unittest.main()

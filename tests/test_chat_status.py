"""对话失败时只认状态码，不把接口原文交给页面。"""

import unittest

from llm_parser import DeepSeekUnreachable, _chat, _http_status
from operator_rule import _asks_capabilities


class Status(Exception):
    def __init__(self, code: int):
        super().__init__("hidden")
        self.status_code = code


class Busy(Exception):
    def __init__(self):
        super().__init__("Error code: 503")
        self.status_code = 503


class Slow(Exception):
    def __init__(self):
        super().__init__("The read operation timed out")


class _Completions:
    def __init__(self, errors):
        self.errors = list(errors)

    def create(self, **kwargs):
        raise self.errors.pop(0)


class _Client:
    def __init__(self, errors):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(errors)

    def with_options(self, timeout=None):
        return self


class ChatStatus(unittest.TestCase):
    def test_reads_status_code(self):
        self.assertEqual(_http_status(Status(503)), 503)

    def test_reads_error_code_text(self):
        self.assertEqual(_http_status(Exception("Error code: 429 - busy")), 429)

    def test_busy_then_timeout_says_try_later(self):
        import llm_parser

        llm_parser._client = None
        original = llm_parser._get_client
        llm_parser._get_client = lambda: _Client([Busy(), Slow()])
        try:
            with self.assertRaises(DeepSeekUnreachable) as caught:
                llm_parser._chat(
                    [{"role": "user", "content": "好"}],
                    json_mode=False,
                    max_tokens=8,
                    temperature=0,
                    tries=2,
                )
        finally:
            llm_parser._get_client = original
            llm_parser._client = None
        self.assertIn("过一会儿再试", str(caught.exception))
        self.assertNotIn("代理", str(caught.exception))

    def test_one_try_stops_when_the_service_is_busy(self):
        import llm_parser

        calls = {"n": 0}
        original = llm_parser._get_client

        class Once(_Client):
            def __init__(self):
                super().__init__([Busy(), Slow()])

            def with_options(self, timeout=None):
                return self

        client = Once()
        real_create = client.chat.completions.create

        def counting(**kwargs):
            calls["n"] += 1
            return real_create(**kwargs)

        client.chat.completions.create = counting
        llm_parser._client = None
        llm_parser._get_client = lambda: client
        try:
            with self.assertRaises(DeepSeekUnreachable) as caught:
                _chat(
                    [{"role": "user", "content": "好"}],
                    json_mode=False,
                    max_tokens=8,
                    temperature=0,
                    tries=1,
                )
        finally:
            llm_parser._get_client = original
            llm_parser._client = None
        self.assertEqual(calls["n"], 1)
        self.assertIn("过一会儿再试", str(caught.exception))

    def test_pull_sentence_stays_short(self):
        from operator_rule import blank_rule, describe_rule

        text = describe_rule(blank_rule())
        self.assertIn("拉未读", text)
        self.assertNotIn("扫完再看", text)
        self.assertLess(len(text), 48)

    def test_capability_question_stays_local(self):
        self.assertTrue(_asks_capabilities("还能添加什么规则"))
        self.assertFalse(_asks_capabilities("每 10 分钟拉取"))

    def test_common_questions_answer_without_the_model(self):
        import tempfile
        from pathlib import Path

        import db_init
        import llm_parser
        import operator_rule

        calls = []

        def answer(history, summary):
            calls.append(history[-1]["content"])
            return "我是邮智汇的助手。"

        llm_parser.answer_question = answer
        tmp = tempfile.TemporaryDirectory()
        old = db_init.DB_PATH
        db_init.DB_PATH = Path(tmp.name) / "chat.db"
        operator_rule._pending_rule = None
        operator_rule._pending_fetch = False
        try:
            db_init.init_db()
            current = operator_rule.ask_for_advice("现在的规则")
            self.assertIn("现在是：", current)
            self.assertIsNone(operator_rule._pending_rule)
            added = operator_rule.ask_for_advice("还能添加什么规则")
            self.assertIn("确认后才会保存", added)
            self.assertIsNone(operator_rule._pending_rule)
            self.assertEqual(calls, [])
            other = operator_rule.ask_for_advice("你是谁")
            self.assertIn("调度助手", other)
            self.assertEqual(calls, [])
            self.assertIsNone(operator_rule._pending_rule)
            here = operator_rule.ask_for_advice("还在吗")
            self.assertIn("在。", here)
            self.assertEqual(calls, [])
            changed = operator_rule.ask_for_advice("每 10 分钟拉取")
            self.assertIn("每 10 分钟", changed)
            self.assertEqual(calls, [])
            self.assertIsNotNone(operator_rule._pending_rule)
        finally:
            operator_rule._pending_rule = None
            operator_rule._pending_fetch = False
            db_init.DB_PATH = old
            tmp.cleanup()

    def test_spoken_rule_is_proposed_without_the_model(self):
        import tempfile
        from pathlib import Path

        import db_init
        import llm_parser
        import operator_rule

        def fail_remote(*args, **kwargs):
            raise AssertionError("改规则不应再调用模型")

        llm_parser.answer_question = fail_remote
        tmp = tempfile.TemporaryDirectory()
        old = db_init.DB_PATH
        db_init.DB_PATH = Path(tmp.name) / "chat.db"
        operator_rule._pending_rule = None
        operator_rule._pending_fetch = False
        try:
            db_init.init_db()
            reply = operator_rule.ask_for_advice("能不能做到每分钟都拉取并下午五点给我提醒")
            self.assertIn("每 1 分钟", reply)
            self.assertIn("17:00", reply)
            self.assertIn("确认后才执行", reply)
            self.assertNotIn("连不上", reply)
            pending = operator_rule._pending_rule
            self.assertIsNotNone(pending)
            self.assertEqual(pending["poll_minutes"], 1)
            self.assertEqual(pending["report_time"], "17:00")
            half = operator_rule._direct_schedule("下午五点半提醒", operator_rule.blank_rule())
            self.assertEqual(half["report_time"], "17:30")
            hours = operator_rule._direct_schedule("每两小时拉取", operator_rule.blank_rule())
            self.assertEqual(hours["poll_minutes"], 120)
            batch = operator_rule._direct_schedule("每批二十封", operator_rule.blank_rule())
            self.assertEqual(batch["fetch_limit"], 20)
            gap = operator_rule._direct_schedule("间隔十分钟", operator_rule.blank_rule())
            self.assertEqual(gap["poll_minutes"], 10)
            once = operator_rule._direct_schedule("十分钟一次", operator_rule.blank_rule())
            self.assertEqual(once["poll_minutes"], 10)
            quicker = operator_rule._direct_schedule("快一点", operator_rule.blank_rule())
            self.assertEqual(quicker["poll_minutes"], 2)
            lighter = operator_rule._direct_schedule("少拉一点", operator_rule.blank_rule())
            self.assertEqual(lighter["fetch_limit"], 15)
            optimized = operator_rule.ask_for_advice("优化规则")
            self.assertIn("每 10 分钟", optimized)
            self.assertIn("15 封", optimized)
            self.assertIn("确认后才执行", optimized)
            self.assertIsNotNone(operator_rule._pending_rule)
            operator_rule._pending_rule = None
            link = operator_rule.ask_for_advice("现在连上了吗")
            self.assertIn("不用等它", link)
            self.assertNotIn("连不上 DeepSeek", link)
            self.assertIsNone(operator_rule._pending_rule)
        finally:
            operator_rule._pending_rule = None
            operator_rule._pending_fetch = False
            db_init.DB_PATH = old
            tmp.cleanup()

    def test_other_question_can_report_disconnect(self):
        import tempfile
        from pathlib import Path

        import db_init
        import llm_parser
        import operator_rule

        def down(history, summary):
            raise DeepSeekUnreachable("连不上 DeepSeek。请检查网络或代理后再试。")

        llm_parser.answer_question = down
        tmp = tempfile.TemporaryDirectory()
        old = db_init.DB_PATH
        db_init.DB_PATH = Path(tmp.name) / "chat.db"
        operator_rule._pending_rule = None
        operator_rule._pending_fetch = False
        try:
            db_init.init_db()
            with self.assertRaises(operator_rule.AdviceUnavailable) as caught:
                operator_rule.ask_for_advice("明天天气怎么样")
            self.assertIn("连不上", str(caught.exception))
            self.assertIsNone(operator_rule._pending_rule)
        finally:
            operator_rule._pending_rule = None
            operator_rule._pending_fetch = False
            db_init.DB_PATH = old
            tmp.cleanup()

    def test_unreachable_message_stays_short(self):
        message = "DeepSeek 这会儿没有回应，过一会儿再试。"
        self.assertNotIn("key", str(DeepSeekUnreachable(message)).lower())
        self.assertIn("过一会儿再试", message)


if __name__ == "__main__":
    unittest.main()

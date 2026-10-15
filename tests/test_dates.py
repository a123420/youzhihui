"""日期识别的固定例子。改提示词或时间规则后先跑这一组。"""

import unittest
from datetime import date

from scheduler_task import (
    _calendar_day_number,
    _status_for_time,
    normalize_execute_time,
    reminder_execute_at,
    requested_lookback_days,
)


class DateRules(unittest.TestCase):
    def test_exact_clock_is_kept(self):
        moment = reminder_execute_at("主题", "正文", "去做", "2026-10-14 09:00:00", "2026-11-02 09:30:00")
        self.assertEqual(moment, "2026-11-02 09:30:00")

    def test_deadline_wins_over_the_incident_clock(self):
        moment = reminder_execute_at(
            "告警",
            "今天09:40设备告警，18点前完成处置",
            "",
            "2026-10-14 09:40:00",
        )
        self.assertEqual(moment, "2026-10-14 18:00:00")

    def test_equipment_number_is_not_a_calendar_day(self):
        self.assertIsNone(_calendar_day_number("核查3号塔机"))
        self.assertIsNone(_calendar_day_number("2号楼今天检查"))
        self.assertIsNone(_calendar_day_number("5号车间"))
        moment = reminder_execute_at(
            "巡检",
            "核查3号塔机，今天18点前完成",
            "",
            "2026-10-15 08:00:00",
        )
        self.assertEqual(moment, "2026-10-15 18:00:00")

    def test_today_and_tomorrow_follow_the_mail_date(self):
        tomorrow = reminder_execute_at("回复", "请明天18点前回复", "", "2026-10-14 09:00:00")
        self.assertEqual(tomorrow, "2026-10-15 18:00:00")
        today = reminder_execute_at("回复", "请今天16点前回复", "", "2026-10-14 09:00:00")
        self.assertEqual(today, "2026-10-14 16:00:00")

    def test_old_mail_keeps_the_day_number_in_its_own_month(self):
        self.assertLess(date(2026, 9, 10), date.today())
        moment = reminder_execute_at("汇总", "请在15号18点前交报告", "", "2026-09-10 08:00:00")
        self.assertEqual(moment, "2026-09-15 18:00:00")

    def test_weekday_uses_the_mail_date(self):
        self.assertEqual(date(2026, 10, 14).weekday(), 2)
        moment = reminder_execute_at("会议", "本周五18点前提交", "", "2026-10-14 09:00:00")
        self.assertEqual(moment, "2026-10-16 18:00:00")

    def test_quoted_header_is_not_the_deadline(self):
        moment = reminder_execute_at(
            "",
            "发送时间：2020-01-01 08:00\n请明天18点前回复",
            "",
            "2026-10-14 09:00:00",
        )
        self.assertEqual(moment, "2026-10-15 18:00:00")

    def test_past_time_waits_for_a_person(self):
        self.assertEqual(_status_for_time("2020-01-01 08:00:00"), "need_time")
        self.assertEqual(_status_for_time("2099-01-01 08:00:00"), "pending")

    def test_immediate_words_still_queue(self):
        moment = normalize_execute_time("马上")
        self.assertRegex(moment, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.assertEqual(_status_for_time(moment, run_now=True), "pending")

    def test_lookback_phrases(self):
        self.assertEqual(requested_lookback_days("近3天所有扬尘监测"), 3)
        self.assertEqual(requested_lookback_days("最近一周"), 7)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from commands import schedule
from commands.ui import schedule_views

KST = ZoneInfo("Asia/Seoul")


def _member(uid, name):
    return SimpleNamespace(id=uid, display_name=name, mention=f"<@{uid}>")


class ScheduleReminderTest(unittest.IsolatedAsyncioTestCase):
    def test_next_reminder_is_sunday_22(self):
        tue = datetime(2026, 10, 6, 15, 0, tzinfo=KST)
        self.assertEqual(schedule._next_reminder(tue), datetime(2026, 10, 11, 22, 0, tzinfo=KST))
        sun_late = datetime(2026, 10, 11, 22, 30, tzinfo=KST)
        self.assertEqual(schedule._next_reminder(sun_late), datetime(2026, 10, 18, 22, 0, tzinfo=KST))

    async def _run(self, *, week_start_offset=timedelta(hours=3), all_rest=False):
        now = datetime(2026, 10, 11, 22, 0, tzinfo=KST)
        mgr = SimpleNamespace(
            week_start=now + week_start_offset, week_label="10/12 ~ 10/17",
            get_responded_user_ids=lambda: {"1"}, reminder_message_ids=[7], reminder_shown=None, save_backup=MagicMock(),
            all_rest=all_rest,
        )
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=8)))
        guild = SimpleNamespace(get_channel=lambda cid: channel)
        client = SimpleNamespace(get_guild=lambda gid: guild)
        with patch.object(schedule.BotManager, "get_instance", return_value=SimpleNamespace(get_schedule_manager=lambda: mgr)), \
             patch.object(schedule, "schedule_members", return_value=[_member(1, "a"), _member(2, "b"), _member(3, "c")]), \
             patch.object(schedule, "get_current_kst_time", return_value=now):
            sent = await schedule.send_schedule_reminder(client)
        self.mgr = mgr
        return sent, channel.send

    async def test_mentions_only_non_responders(self):
        sent, send = await self._run()
        self.assertEqual(sent, 2)
        text = send.await_args.kwargs['view'].children[0].children[0].content
        self.assertEqual(text, "## 일정 미응답\n<@2> <@3>\n위 메시지에서 참가나 불참을 선택해주세요.")
        self.assertEqual(len(send.await_args.kwargs['view'].children[0].children), 1)
        self.assertEqual(self.mgr.reminder_message_ids, [7, 8])
        self.assertEqual(self.mgr.reminder_shown, ["2", "3"])
        self.mgr.save_backup.assert_called_once()

    async def test_skips_week_of_rest_days(self):
        self.assertEqual((await self._run(all_rest=True))[0], 0)

    async def test_skips_when_week_not_open(self):
        self.assertEqual((await self._run(week_start_offset=-timedelta(days=1)))[0], 0)


class DeleteOldRemindersTest(unittest.IsolatedAsyncioTestCase):
    async def test_skips_deleted_and_keeps_failed(self):
        response = SimpleNamespace(status=0, reason="")
        errors = {1: None, 2: schedule.discord.NotFound(response, "gone"), 3: schedule.discord.HTTPException(response, "fail")}

        def partial(message_id):
            return SimpleNamespace(delete=AsyncMock(side_effect=errors[message_id]))

        channel = SimpleNamespace(get_partial_message=partial)
        mgr = SimpleNamespace(reminder_message_ids=[1, 2, 3], reminder_shown=["1"], save_backup=MagicMock())
        await schedule.delete_old_reminders(channel, mgr)
        self.assertEqual(mgr.reminder_message_ids, [3])
        mgr.save_backup.assert_called_once()


class AnsweredReminderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deleted, self.edited = AsyncMock(), AsyncMock()
        self.mgr = SimpleNamespace(
            reminder_message_ids=[7], reminder_shown=["1", "2", "3"], get_status_text=lambda admins, groups: "일정",
            week_start=None, actual_deployments={}, rest_days=set(), all_rest=False,
            status_channel_id=5, status_message_id=9, save_backup=MagicMock(),
        )

    async def _refresh(self, responded):
        channel = SimpleNamespace(
            id=5, get_partial_message=lambda message_id: SimpleNamespace(delete=self.deleted, edit=self.edited),
        )
        guild = SimpleNamespace(get_channel=lambda cid: channel)
        self.mgr.get_responded_user_ids = lambda: responded
        members = [_member(1, "a"), _member(2, "b"), _member(3, "c")]
        with patch.object(schedule_views, "schedule_members", return_value=members), \
             patch.object(schedule_views, "upsert_persistent_message", new=AsyncMock(return_value=9)):
            await schedule_views.refresh_dashboard(guild, schedule_mgr=self.mgr)

    async def test_reminder_is_deleted_once_everyone_answered(self):
        await self._refresh({"1", "2", "3"})
        self.assertEqual(self.mgr.reminder_message_ids, [])
        self.deleted.assert_awaited_once()
        self.edited.assert_not_awaited()

    async def test_reminder_keeps_only_the_operators_who_have_not_answered(self):
        await self._refresh({"1"})
        self.assertEqual(self.mgr.reminder_message_ids, [7])
        self.deleted.assert_not_awaited()
        text = self.edited.await_args.kwargs["view"].children[0].children[0].content
        self.assertEqual(text, "## 일정 미응답\n<@2> <@3>\n위 메시지에서 참가나 불참을 선택해주세요.")

        # 남은 사람이 그대로면 다시 편집하지 않음
        await self._refresh({"1"})
        self.edited.assert_awaited_once()

    async def test_reminder_is_deleted_in_a_week_of_rest_days(self):
        self.mgr.all_rest = True
        await self._refresh({"1"})
        self.assertEqual(self.mgr.reminder_message_ids, [])
        self.deleted.assert_awaited_once()


class WeekOpenTest(unittest.TestCase):
    def test_week_opens_friday_night(self):
        mgr = SimpleNamespace(week_start=datetime(2026, 10, 5, tzinfo=KST))
        with patch.object(schedule, "get_current_kst_time", return_value=datetime(2026, 10, 9, 21, 59, tzinfo=KST)):
            self.assertFalse(schedule._should_auto_reset(mgr))
        with patch.object(schedule, "get_current_kst_time", return_value=datetime(2026, 10, 9, 22, 0, tzinfo=KST)):
            self.assertTrue(schedule._should_auto_reset(mgr))


if __name__ == "__main__":
    unittest.main()

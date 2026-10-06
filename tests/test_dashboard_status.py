import asyncio
import unittest
from datetime import datetime
from unittest import mock

from config.settings import settings
from models import team_data_manager as tdm_module
from models.team_data import TeamData
from models.team_data_manager import (
    PHASE_ASSIGNED, PHASE_CANCELLED, PHASE_CLOSED, PHASE_OPEN, TeamDataManager,
)
from tests.test_force_cancel import _collect_text_contents
from tests.test_lifecycle_assignment import AssignmentHarness
from utils.helpers import KST

DEADLINE = settings.TEAM_REGISTRATION_DEADLINE_HOUR


def _at(hour, minute=0, day=6):
    return datetime(2026, 10, day, hour, minute, tzinfo=KST)


def _phase_manager(*, started=False, last=None, groups=None):
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr.scrim_day, mgr.scrim_month = 6, 10
    mgr.is_team_assignment_started = started
    mgr.last_auto_assignment = last
    mgr.groups = groups
    return mgr


def _buttons(view):
    return {
        button.label: button
        for button in (view.add_team_button, view.cancel_team_button, view.my_sanctions_button, view.manage_button)
    }


class RegistrationPhaseTest(unittest.TestCase):
    def test_phase_matrix(self):
        cases = [
            ('마감 전', _phase_manager(), _at(DEADLINE - 1, 59), PHASE_OPEN),
            ('전날 22시 이후 신청', _phase_manager(), _at(23, day=5), PHASE_OPEN),
            ('마감 뒤 조편성 전', _phase_manager(), _at(DEADLINE, 0), PHASE_CLOSED),
            ('조편성 진행 중', _phase_manager(started=True), _at(DEADLINE, 3), PHASE_CLOSED),
            ('조편성 완료', _phase_manager(started=True, last=_at(DEADLINE, 5), groups=[[]]), _at(DEADLINE, 6), PHASE_ASSIGNED),
            ('팀 부족 취소', _phase_manager(last=_at(DEADLINE, 1)), _at(DEADLINE, 2), PHASE_CANCELLED),
        ]
        for name, mgr, now, expected in cases:
            with self.subTest(name):
                self.assertEqual(mgr.registration_phase(now), expected)


class DashboardViewTest(unittest.TestCase):
    def _view(self, phase, team_count=0):
        from commands.ui.views import TeamInputView

        return TeamInputView(
            scrim_day=6, scrim_month=10, scrim_weekday="화요일", phase=phase, team_count=team_count,
        )

    def test_status_line_per_phase(self):
        expected = {
            PHASE_OPEN: f"현재 5팀 신청, {settings.TEAMS_PER_GROUP}팀 미만이면 취소됩니다.",
            PHASE_CLOSED: (
                f"{DEADLINE}시에 신청이 마감되었습니다. "
                f"{settings.NEXT_SCRIM_OPEN_HOUR}시에 다음 스크림 신청이 열립니다."
            ),
            PHASE_ASSIGNED: f"조편성을 마쳤습니다. 다음 스크림 신청은 {settings.NEXT_SCRIM_OPEN_HOUR}시에 열립니다.",
            PHASE_CANCELLED: (
                f"오늘 스크림은 팀 부족으로 취소되었습니다. "
                f"다음 스크림 신청은 {settings.NEXT_SCRIM_OPEN_HOUR}시에 열립니다."
            ),
        }
        for phase, line in expected.items():
            with self.subTest(phase):
                body = "\n".join(_collect_text_contents(self._view(phase, team_count=5)))
                self.assertIn(line, body)

    def test_buttons_disabled_after_deadline(self):
        self.assertEqual(
            [b.label for b in self._view(PHASE_OPEN).children[1].children],
            ["신청 및 수정", "취소", "내 제재", "관리"],
        )
        for phase in (PHASE_OPEN, PHASE_CLOSED, PHASE_ASSIGNED, PHASE_CANCELLED):
            with self.subTest(phase):
                buttons = _buttons(self._view(phase))
                closed = phase != PHASE_OPEN
                self.assertEqual(buttons["신청 및 수정"].disabled, closed)
                self.assertEqual(buttons["취소"].disabled, closed)
                self.assertFalse(buttons["내 제재"].disabled)
                self.assertFalse(buttons["관리"].disabled)

    def test_custom_ids_fixed_across_refreshes(self):
        first = {label: b.custom_id for label, b in _buttons(self._view(PHASE_OPEN)).items()}
        second = {label: b.custom_id for label, b in _buttons(self._view(PHASE_CLOSED)).items()}
        self.assertEqual(first, second)
        self.assertEqual(len(set(first.values())), 4)
        self.assertTrue(all(cid.startswith("scrim_dashboard_") for cid in first.values()))


class DebouncedRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from commands import scrim
        self.scrim = scrim
        self.tdm = mock.Mock(dashboard_message_id=55)
        self.channel = object()
        bot = mock.Mock()
        bot.get_team_data_manager.return_value = self.tdm
        bot.get_client.return_value.get_channel.return_value = self.channel
        self.refresh = mock.AsyncMock()
        for target in (
            mock.patch.object(scrim, 'DASHBOARD_REFRESH_DELAY_SECONDS', 0),
            mock.patch.object(scrim, 'is_scrim_expired', return_value=False),
            mock.patch.object(scrim, '_refresh_scrim_dashboard', self.refresh),
            mock.patch.object(scrim, '_dashboard_refresh_task', None),
        ):
            target.start()
        mock.patch.object(scrim, 'BotManager').start().get_instance.return_value = bot
        self.addCleanup(mock.patch.stopall)

    async def _drain(self):
        for _ in range(3):
            task = self.scrim._dashboard_refresh_task
            if task is None:
                return
            await task

    async def test_burst_is_one_edit(self):
        for _ in range(10):
            self.scrim.request_dashboard_refresh()
        await self._drain()
        self.refresh.assert_awaited_once_with(self.channel)

    async def test_request_during_edit_schedules_another(self):
        async def edit(_channel):
            if self.refresh.await_count == 1:
                self.scrim.request_dashboard_refresh()

        self.refresh.side_effect = edit
        self.scrim.request_dashboard_refresh()
        await self._drain()
        self.assertEqual(self.refresh.await_count, 2)

    async def test_skips_without_dashboard_message(self):
        self.tdm.dashboard_message_id = None
        self.scrim.request_dashboard_refresh()
        await self._drain()
        self.refresh.assert_not_awaited()

    async def test_skips_while_transition_pending(self):
        self.scrim.is_scrim_expired.return_value = True
        self.scrim.request_dashboard_refresh()
        await self._drain()
        self.refresh.assert_not_awaited()


class DashboardLockTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refreshes_do_not_overlap(self):
        from commands import scrim

        order = []

        async def edit(_channel):
            order.append('start')
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            order.append('end')

        with mock.patch.object(scrim, '_edit_scrim_dashboard', side_effect=edit), \
             mock.patch.object(scrim, '_dashboard_lock', asyncio.Lock()):
            await asyncio.gather(
                scrim._refresh_scrim_dashboard(object()),
                scrim._refresh_scrim_dashboard(object()),
            )
        self.assertEqual(order, ['start', 'end', 'start', 'end'])


class ManagerRefreshTriggerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        tdm_module.set_dashboard_refresh_hook(lambda: self.calls.append(1))
        self.addCleanup(tdm_module.set_dashboard_refresh_hook, None)
        self.mgr = TeamDataManager()
        self.mgr.save_backup = lambda: None
        self.mgr.scrim_day, self.mgr.scrim_month = 7, 10
        patcher = mock.patch('models.team_data_manager.get_current_kst_time', return_value=_at(12))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_add_edit_cancel_request_refresh(self):
        user = mock.Mock(id=1)
        ok, _ = await self.mgr.add_team("알파", TeamData(name="알파", players=["a"]), user)
        self.assertTrue(ok)
        ok, _ = await self.mgr.replace_team("알파", TeamData(name="베타", players=["b"]), 1.0)
        self.assertTrue(ok)
        ok, _ = await self.mgr.remove_team("베타")
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 3)

    async def test_roster_change_after_assignment_does_not_refresh(self):
        self.mgr.teams["알파"] = TeamData(name="알파", players=["a"])
        ok, _ = await self.mgr.replace_team(
            "알파", TeamData(name="알파", players=["b"]), 1.0, enforce_rules=False,
        )
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])

    async def test_rejected_add_does_not_refresh(self):
        self.mgr.is_team_assignment_started = True
        ok, _ = await self.mgr.add_team("알파", TeamData(name="알파", players=["a"]), mock.Mock(id=1))
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])


class AssignmentRefreshTriggerTest(AssignmentHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.phases = []
        self.mgr = None
        tdm_module.set_dashboard_refresh_hook(lambda: self.phases.append(self.mgr.registration_phase()))
        self.addCleanup(tdm_module.set_dashboard_refresh_hook, None)

    async def test_deadline_and_completion_refresh(self):
        self.mgr = mgr = self.make_manager()
        self.use_groups(mgr, 2)
        self.use_service(mgr)

        await mgr.start_team_assignment()

        self.assertEqual(self.phases, [PHASE_CLOSED, PHASE_ASSIGNED])

    async def test_lack_of_teams_refresh(self):
        self.mgr = mgr = self.make_manager(team_count=3)

        await mgr.start_team_assignment()

        self.assertEqual(self.phases, [PHASE_CLOSED, PHASE_CANCELLED])

    async def test_no_refresh_outside_scrim_day(self):
        self.mgr = mgr = self.make_manager()
        mgr.scrim_day = 7

        await mgr.start_team_assignment()

        self.assertEqual(self.phases, [])


if __name__ == '__main__':
    unittest.main()

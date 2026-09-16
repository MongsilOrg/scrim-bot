import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

from config.logging_config import get_logger
from utils.helpers import save_json_atomic
from .team_data import TeamData

if TYPE_CHECKING:
    from .team_data_manager import TeamDataManager

logger = get_logger('team_backup')


def _iso(dt):
    return dt.isoformat() if dt else None


def _from_iso(raw):
    return datetime.fromisoformat(raw) if raw else None


# 항목 순서: 저장 키, 매니저 속성, to_json, from_json, 로드 기본값
_META_FIELDS = [
    ('scrim_day', 'scrim_day', None, None, None),
    ('scrim_month', 'scrim_month', None, None, None),
    ('scrim_channel_id', 'scrim_channel_id', None, None, None),
    ('is_team_assignment_started', 'is_team_assignment_started', None, None, False),
    ('last_auto_assignment', 'last_auto_assignment', _iso, _from_iso, None),
    ('is_maintenance', 'is_maintenance', None, None, False),
    ('last_success_time', '_last_success_time', None, None, ''),
]
_TOP_FIELDS = [
    ('group_message_ids', 'group_message_ids', None, None, dict),
    ('group_message_texts', 'group_message_texts', None, None, dict),
    ('dashboard_message_id', 'dashboard_message_id', None, None, None),
    ('mmr_message_id', 'mmr_message_id', None, None, None),
    ('selected_weathers', '_selected_weathers', None, None, dict),
    ('unverified_teams', 'unverified_teams', list, set, set),
]


class TeamBackup:
    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager

    @property
    def backup_file(self) -> str:
        return self._manager.BACKUP_FILE

    def save(self) -> None:
        try:
            serialized_groups = None
            if self._manager.groups is not None:
                serialized_groups = []
                for group in self._manager.groups:
                    serialized_group = []
                    for team_name, team_data, mmr in group:
                        serialized_group.append([team_name, team_data.to_dict(), mmr])
                    serialized_groups.append(serialized_group)

            meta = {}
            for key, attr, to_json, _, _ in _META_FIELDS:
                value = getattr(self._manager, attr)
                meta[key] = to_json(value) if to_json else value

            data = {
                '_meta': meta,
                'teams': {
                    name: team.to_dict()
                    for name, team in self._manager.teams.items()
                },
                'groups': serialized_groups,
            }
            for key, attr, to_json, _, _ in _TOP_FIELDS:
                value = getattr(self._manager, attr)
                data[key] = to_json(value) if to_json else value

            save_json_atomic(self.backup_file, data, indent=2)
        except Exception as e:
            logger.error(f"[팀데이터] 백업 저장 실패: {e}", exc_info=True)

    def load(self) -> bool:
        try:
            if not os.path.exists(self.backup_file):
                return False
            with open(self.backup_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not data:
                return False

            mgr = self._manager

            if '_meta' not in data:
                return False

            meta = data['_meta']
            teams_data = data.get('teams', {})

            for section, fields in ((meta, _META_FIELDS), (data, _TOP_FIELDS)):
                for key, attr, _, from_json, default in fields:
                    raw = section.get(key)
                    if raw is None:
                        value = default() if callable(default) else default
                    else:
                        value = from_json(raw) if from_json else raw
                    setattr(mgr, attr, value)

            for name, team_dict in teams_data.items():
                team = TeamData.from_dict(name, team_dict)
                mgr.teams[name] = team
                mgr._add_member_index(name, team)
            saved_groups = data.get('groups')
            if saved_groups is not None:
                mgr.groups = []
                for group in saved_groups:
                    restored_group = []
                    for team_name, team_dict, mmr in group:
                        restored_group.append((team_name, TeamData.from_dict(team_name, team_dict), mmr))
                    mgr.groups.append(restored_group)

            logger.info(
                f"[팀데이터] 백업에서 {len(teams_data)}개 팀 복구 완료 "
                f"(스크림 날짜: {mgr.scrim_month}/{mgr.scrim_day})"
            )
            return True
        except Exception as e:
            logger.error(f"[팀데이터] 백업 복구 실패: {e}", exc_info=True)
            return False

    def should_restore(self) -> bool:
        """초기화는 transition_to_next_scrim 담당이라 날짜 미검사."""
        try:
            if not os.path.exists(self.backup_file):
                return False
            with open(self.backup_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            meta = data.get('_meta')
            if not meta:
                return False
            return True
        except Exception as e:
            logger.error(f"[팀데이터] 백업 유효성 검사 실패: {e}", exc_info=True)
            return False

    def clear(self) -> None:
        try:
            if os.path.exists(self.backup_file):
                os.remove(self.backup_file)
        except Exception as e:
            logger.error(f"[팀데이터] 백업 삭제 실패: {e}", exc_info=True)

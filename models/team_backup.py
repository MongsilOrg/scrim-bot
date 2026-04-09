"""팀 데이터 백업/복구 모듈

팀 데이터의 JSON 백업 저장, 복구, 유효성 검사 기능을 담당합니다.
"""
import json
import os
from typing import TYPE_CHECKING

from config.logging_config import get_logger
from .team_data import TeamData

if TYPE_CHECKING:
    from .team_data_manager import TeamDataManager

logger = get_logger('team_backup')


class TeamBackup:
    """팀 데이터 백업/복구를 담당하는 클래스"""

    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager

    @property
    def backup_file(self) -> str:
        return self._manager.BACKUP_FILE

    def save(self) -> None:
        """팀 데이터를 JSON 파일로 백업합니다 (날짜 메타데이터 포함)."""
        try:
            backup_dir = os.path.dirname(self.backup_file)
            if backup_dir:
                os.makedirs(backup_dir, exist_ok=True)
            # BotManager에서 밴/날씨 데이터 가져오기
            from bot.manager import BotManager
            bot_manager = BotManager.get_instance()

            # groups 직렬화
            serialized_groups = None
            if self._manager.groups is not None:
                serialized_groups = []
                for group in self._manager.groups:
                    serialized_group = []
                    for team_name, team_data, mmr in group:
                        serialized_group.append([team_name, team_data.to_dict(), mmr])
                    serialized_groups.append(serialized_group)

            data = {
                '_meta': {
                    'scrim_day': self._manager.scrim_day,
                    'scrim_month': self._manager.scrim_month,
                    'scrim_channel_id': self._manager.scrim_channel_id,
                    'is_team_assignment_started': self._manager.is_team_assignment_started,
                    'last_auto_assignment': self._manager.last_auto_assignment.isoformat() if self._manager.last_auto_assignment else None,
                },
                'teams': {
                    name: team.to_dict()
                    for name, team in self._manager.teams.items()
                },
                'groups': serialized_groups,
                'group_message_ids': self._manager.group_message_ids,
                'group_message_texts': self._manager.group_message_texts,
                'dashboard_message_id': self._manager.dashboard_message_id,
                'mmr_message_id': self._manager.mmr_message_id,
                'ban_lists': bot_manager._ban_lists,
                'selected_weathers': bot_manager._selected_weathers,
                'unverified_teams': list(self._manager.unverified_teams),
            }
            tmp_path = self.backup_file + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.backup_file)
        except Exception as e:
            logger.error(f"[팀데이터] 백업 저장 실패: {e}", exc_info=True)

    def load(self) -> bool:
        """JSON 백업에서 팀 데이터를 복구합니다. 성공 시 True 반환."""
        try:
            if not os.path.exists(self.backup_file):
                return False
            with open(self.backup_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not data:
                return False

            mgr = self._manager
            from datetime import datetime

            # 메타데이터가 있는 새 형식과 레거시 형식 모두 지원
            if '_meta' in data:
                meta = data['_meta']
                teams_data = data.get('teams', {})
                mgr.scrim_day = meta.get('scrim_day')
                mgr.scrim_month = meta.get('scrim_month')
                mgr.scrim_channel_id = meta.get('scrim_channel_id')
                mgr.is_team_assignment_started = meta.get('is_team_assignment_started', False)
                last_assign = meta.get('last_auto_assignment')
                mgr.last_auto_assignment = datetime.fromisoformat(last_assign) if last_assign else None
            else:
                # 레거시 형식: 메타데이터 없이 팀 데이터만 저장
                teams_data = data

            for name, team_dict in teams_data.items():
                team = TeamData.from_dict(name, team_dict)
                mgr.teams[name] = team
                mgr._add_member_index(name, team)
                mgr._update_mmr_index(name, 0.0, team.mmr)
                for member in team.all_members:
                    key = mgr._normalize_member_key(member)
                    mgr.user_teams[key] = name
            # groups 복구
            saved_groups = data.get('groups')
            if saved_groups is not None:
                mgr.groups = []
                for group in saved_groups:
                    restored_group = []
                    for team_name, team_dict, mmr in group:
                        restored_group.append((team_name, TeamData.from_dict(team_name, team_dict), mmr))
                    mgr.groups.append(restored_group)

            # group_message_ids / texts 복구
            saved_msg_ids = data.get('group_message_ids')
            if saved_msg_ids:
                mgr.group_message_ids = saved_msg_ids
            saved_msg_texts = data.get('group_message_texts')
            if saved_msg_texts:
                mgr.group_message_texts = saved_msg_texts

            mgr.dashboard_message_id = data.get('dashboard_message_id')
            mgr.mmr_message_id = data.get('mmr_message_id')
            mgr.unverified_teams = set(data.get('unverified_teams', []))

            # BotManager에 밴/날씨 데이터 주입
            from bot.manager import BotManager
            bot_manager = BotManager.get_instance()
            saved_bans = data.get('ban_lists')
            if saved_bans:
                bot_manager._ban_lists = saved_bans
            saved_weathers = data.get('selected_weathers')
            if saved_weathers:
                bot_manager._selected_weathers = saved_weathers

            logger.info(
                f"[팀데이터] 백업에서 {len(teams_data)}개 팀 복구 완료 "
                f"(스크림 날짜: {mgr.scrim_month}/{mgr.scrim_day})"
            )
            return True
        except Exception as e:
            logger.error(f"[팀데이터] 백업 복구 실패: {e}", exc_info=True)
            return False

    def should_restore(self) -> bool:
        """백업 파일이 유효한지 확인합니다.

        백업 파일이 존재하고 메타데이터가 있으면 항상 유효합니다.
        초기화는 오직 /스크림 명령어(reset_team_data())로만 수행됩니다.
        """
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
        """백업 파일을 삭제합니다."""
        try:
            if os.path.exists(self.backup_file):
                os.remove(self.backup_file)
        except Exception as e:
            logger.error(f"[팀데이터] 백업 삭제 실패: {e}", exc_info=True)

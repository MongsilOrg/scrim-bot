from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from utils.helpers import get_current_kst_time

MMR_PENDING_LABEL = "미확정"


def format_team_mmr(mmr: float, confirmed: bool) -> str:
    """미확정과 실제 0점을 같은 0.00으로 보여주지 않기 위한 단일 표기 규칙."""
    return f"{mmr:.2f}" if confirmed else MMR_PENDING_LABEL


@dataclass(frozen=True)
class TeamMmrResult:
    """confirmed는 인원 전원의 MMR을 알아냈는지, mmr 0.0과는 별개."""
    mmr: float = 0.0
    confirmed: bool = False
    failed_players: Tuple[str, ...] = ()


@dataclass
class TeamData:
    name: str
    players: List[str] = field(default_factory=list)
    staff: List[str] = field(default_factory=list)
    user_id: Optional[str] = None
    mmr: float = 0.0
    mmr_confirmed: bool = False
    # 마지막 갱신에서 MMR을 못 가져온 멤버, 표기에서 흐리게 처리
    mmr_failed_players: List[str] = field(default_factory=list)
    mmr_updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_seed: bool = False
    seed_name: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = get_current_kst_time()
        if self.updated_at is None:
            self.updated_at = get_current_kst_time()
    
    @property
    def all_members(self) -> List[str]:
        return self.players + self.staff

    @property
    def mmr_display(self) -> str:
        return format_team_mmr(self.mmr, self.mmr_confirmed)

    def to_dict(self) -> Dict:
        result = {
            'players': self.players,
            'staff': self.staff,
            'user_id': self.user_id,
            'mmr': self.mmr,
            'mmr_confirmed': self.mmr_confirmed,
            'mmr_failed_players': self.mmr_failed_players,
            'is_seed': self.is_seed,
            'seed_name': self.seed_name,
        }
        if self.mmr_updated_at:
            result['mmr_updated_at'] = self.mmr_updated_at.isoformat()
        if self.created_at:
            result['created_at'] = self.created_at.isoformat()
        if self.updated_at:
            result['updated_at'] = self.updated_at.isoformat()
        return result

    @classmethod
    def from_dict(cls, name: str, data: Dict) -> 'TeamData':
        team = cls(
            name=name,
            players=data.get('players', []),
            staff=data.get('staff', []),
            user_id=data.get('user_id')
        )
        team.mmr = data.get('mmr', 0.0)
        # 확정 여부가 없는 구버전 백업은 0보다 큰 값을 확정으로 간주
        team.mmr_confirmed = data.get('mmr_confirmed', team.mmr > 0)
        team.mmr_failed_players = list(data.get('mmr_failed_players', []))
        team.is_seed = data.get('is_seed', False)
        team.seed_name = data.get('seed_name')
        for attr in ('mmr_updated_at', 'created_at', 'updated_at'):
            val = data.get(attr)
            if val:
                setattr(team, attr, datetime.fromisoformat(val))
        return team
    
    def __str__(self) -> str:
        return f"TeamData(name='{self.name}', players={len(self.players)}, staff={len(self.staff)}, mmr={self.mmr_display})"
    
    def __repr__(self) -> str:
        return self.__str__()

"""
팀 데이터 구조 정의
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from utils.helpers import get_current_kst_time


@dataclass
class TeamData:
    """팀 데이터 구조"""
    name: str
    players: List[str] = field(default_factory=list)
    staff: List[str] = field(default_factory=list)
    user_id: Optional[str] = None
    mmr: float = 0.0
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
        """모든 멤버 (플레이어 + 스태프) 반환"""
        return self.players + self.staff
    
    def to_dict(self) -> Dict:
        """딕셔너리로 변환 (백업/직렬화 포함)"""
        result = {
            'players': self.players,
            'staff': self.staff,
            'user_id': self.user_id,
            'mmr': self.mmr,
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
        team.is_seed = data.get('is_seed', False)
        team.seed_name = data.get('seed_name')
        for attr in ('mmr_updated_at', 'created_at', 'updated_at'):
            val = data.get(attr)
            if val:
                setattr(team, attr, datetime.fromisoformat(val))
        return team
    
    def __str__(self) -> str:
        return f"TeamData(name='{self.name}', players={len(self.players)}, staff={len(self.staff)}, mmr={self.mmr:.2f})"
    
    def __repr__(self) -> str:
        return self.__str__()



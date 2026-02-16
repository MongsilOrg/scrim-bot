"""
팀 데이터 구조 정의
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


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
    
    def __post_init__(self):
        """초기화 후 처리"""
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
    
    @property
    def all_members(self) -> List[str]:
        """모든 멤버 (플레이어 + 스태프) 반환"""
        return self.players + self.staff
    
    @property
    def player_count(self) -> int:
        """플레이어 수 반환"""
        return len(self.players)
    
    @property
    def staff_count(self) -> int:
        """스태프 수 반환"""
        return len(self.staff)
    
    @property
    def total_members(self) -> int:
        """총 멤버 수 반환"""
        return len(self.all_members)
    
    def add_player(self, player: str) -> bool:
        """플레이어 추가"""
        if player and player.strip() and player not in self.players:
            self.players.append(player.strip())
            self.updated_at = datetime.now()
            return True
        return False
    
    def remove_player(self, player: str) -> bool:
        """플레이어 제거"""
        if player in self.players:
            self.players.remove(player)
            self.updated_at = datetime.now()
            return True
        return False
    
    def add_staff(self, staff: str) -> bool:
        """스태프 추가"""
        if staff and staff.strip() and staff not in self.staff:
            self.staff.append(staff.strip())
            self.updated_at = datetime.now()
            return True
        return False
    
    def remove_staff(self, staff: str) -> bool:
        """스태프 제거"""
        if staff in self.staff:
            self.staff.remove(staff)
            self.updated_at = datetime.now()
            return True
        return False
    
    def has_member(self, member: str) -> bool:
        """멤버 포함 여부 확인"""
        return member in self.all_members
    
    def to_dict(self) -> Dict:
        """딕셔너리로 변환 (백업/직렬화 포함)"""
        return {
            'players': self.players,
            'staff': self.staff,
            'user_id': self.user_id,
            'mmr': self.mmr
        }

    @classmethod
    def from_dict(cls, name: str, data: Dict) -> 'TeamData':
        """딕셔너리에서 생성"""
        team = cls(
            name=name,
            players=data.get('players', []),
            staff=data.get('staff', []),
            user_id=data.get('user_id')
        )
        team.mmr = data.get('mmr', 0.0)
        return team
    
    def __str__(self) -> str:
        return f"TeamData(name='{self.name}', players={len(self.players)}, staff={len(self.staff)}, mmr={self.mmr:.2f})"
    
    def __repr__(self) -> str:
        return self.__str__()



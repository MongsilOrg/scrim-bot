"""
조편성 기록으로 기간 통계를 냄. 사용법: python scripts/season_stats.py 2026-08-13 2026-11-12
"""
import json
import sys
from collections import Counter

path = 'data/scrim_history.jsonl'
since, until = (sys.argv[1:3] + ['0000', '9999'])[:2]

# 같은 날 다시 편성했으면 마지막 줄
days = {}
for line in open(path, encoding='utf-8'):
    row = json.loads(line)
    if since <= row['date'] < until:
        days[row['date']] = row

played = {d: r for d, r in days.items() if r['groups']}
teams = [t for r in played.values() for g in r['groups'] for t in g]
players = Counter(p.lower() for t in teams for p in t['players'])
staff = Counter(p.lower() for t in teams for p in t['staff'])

print(f"기간 {min(days, default='-')} ~ {max(days, default='-')}")
print(f"진행일 {len(played)}일, 조 {sum(len(r['groups']) for r in played.values())}개")
print(f"출전 팀 {len(teams)}팀, 하루 평균 {len(teams) / max(len(played), 1):.1f}팀, 예비팀 {sum(len(r['reserve']) for r in days.values())}팀")
print(f"선수 {len(players)}명, 연인원 {sum(players.values())}명")
print(f"스태프 {len(staff)}명, 연인원 {sum(staff.values())}명")
print("최다 출전 " + ", ".join(f"{p} {n}회" for p, n in players.most_common(5)))

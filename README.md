# scrim-bot

이터널 리턴 스크림(내전) 대회를 운영하는 Discord 봇. 팀 신청부터 조 편성, 방 코드·날씨 공지, 결과 집계, 패널티 관리까지 대회 하루의 흐름을 자동 처리한다.

## 주요 기능

- 채널 상주 대시보드로 팀 신청·수정·취소. 매일 17시 등록 마감
- MMR 기반 자동 조 편성(8팀 단위, 스네이크 드래프트로 균형). 시드팀은 Google Sheets 연동
- `/방코드` — 방 코드와 라운드별 날씨(맵) 공지
- 경기 결과 CSV를 모아 팀별 누적 점수 집계 → 점수표 이미지 생성
- 지각·노쇼 패널티 관리(주의 2회 → 경고, 경고 시 등록 차단). Google Sheets에 기록
- 운영진 주간 편성 일정 관리

## 동작 방식

하루 사이클(17시 편성 → 20시 경기 공지·집계 → 22시 다음날 전환)을 백그라운드 태스크로 돌린다. 팀 데이터는 메모리 관리 + JSON 백업으로 재시작 시 복구한다. UI는 discord.py Components V2, 점수표·MMR 표는 HTML을 wkhtmltoimage로 렌더한다.

## 기술 스택

Python, discord.py, pandas, Pillow/wkhtmltoimage. 연동: bser Open API, Google Sheets, Notion, 공휴일 API. NAS 상시 구동 + main push 시 GitHub Actions 자동 배포.

## 제약

단일 서버 전제. 조 편성은 8팀 이상·8의 배수. 시간 기준(17/20/22시)은 코드 고정.

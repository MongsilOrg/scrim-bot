# scrim-bot

이터널 리턴 스크림 대회를 운영하는 Discord 봇입니다. 팀 신청, 조 편성, 방 코드 공지, 결과 집계, 패널티 관리를 자동으로 처리합니다.

## 기능

- 채널에 떠 있는 대시보드에서 팀 신청, 수정, 취소. 매일 17시에 등록 마감
- MMR 기반 자동 조 편성. 8팀 단위로 나누고 스네이크 드래프트로 균형을 맞춤. 시드팀 명단은 Google Sheets에서 읽음
- `/방코드`: 조별 채널에 방 코드와 라운드별 날씨를 공지. 서브 날씨는 관리자가 버튼으로 고름
- 경기 결과 CSV를 올리면 팀별 누적 점수를 집계해 점수표 이미지로 응답
- 제재 부여 컨텍스트 메뉴로 주의와 경고를 관리. 주의 2회는 경고 1회로 바뀌고, 경고로 제한된 인원이 있는 팀은 등록이 막힘. 기록은 Google Sheets에 남음
- 운영진 주간 편성 일정 대시보드. 매주 토요일 22시에 다음 주로 넘어감

## 동작 방식

하루 사이클을 백그라운드 태스크로 돌립니다. 순서는 17시 조 편성, 20시 경기 시작, 22시 다음 날 스크림 전환입니다. 팀 데이터는 메모리에 두고 JSON 백업으로 재시작 때 되살리며, 팀 MMR은 이터널 리턴 Open API에서 주기적으로 갱신합니다.

## 실행

점수표와 MMR 표 이미지는 wkhtmltoimage로 그리므로 시스템에 설치되어 있어야 합니다. Google Sheets 서비스 계정 인증 파일은 `credentials/google_sheets_credentials.json`에 둡니다.

```bash
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## 설정

| 키 | 구분 | 설명 |
|---|---|---|
| DISCORD_TOKEN | 필수 | Discord 봇 토큰 |
| GUILD_ID | 필수 | Discord 서버 ID |
| ADMIN_ROLE_IDS | 필수 | 관리자 역할 ID, 쉼표 구분 |
| NOTICE_CHANNEL_ID | 필수 | 스크림 공지 채널 ID |
| BACKUP_ANALYSIS_CHANNEL_ID | 필수 | 전적 백업 채널 ID |
| GROUP_CHANNEL_IDS | 필수 | 조별 채널 ID, `A:채널ID,B:채널ID` 형식 |
| BSER_API_KEY | 필수 | 이터널 리턴 Open API 키 |
| GOOGLE_SHEETS_MAIN_SPREADSHEET_ID | 필수 | 시드팀, 테스트 계정, 패널티 공용 스프레드시트 ID |
| GOOGLE_SHEETS_CREDENTIALS_PATH | 선택 | 서비스 계정 인증 파일 경로. 기본값 `credentials/google_sheets_credentials.json` |
| SCRIM_CHANNEL_ID | 선택 | 스크림 대시보드 채널 ID |
| LOG_CHANNEL_ID | 선택 | 운영 로그 채널 ID |
| SCHEDULE_CHANNEL_ID | 선택 | 주간 편성 일정 대시보드 채널 ID |
| TEST_ACCOUNT_CONTACT_ID | 선택 | 테스트 계정 문의 안내에 표시할 사용자 ID |
| GROUP_CATEGORY_PATTERN | 선택 | 조별 음성채널 카테고리 이름 패턴. `{letter}` 자리에 조 문자가 들어감 |
| ANNOUNCEMENT_MESSAGE | 선택 | 대시보드에 표시할 공지 문구 |
| EMBED_FOOTER_TEXT | 선택 | 메시지 푸터 텍스트 |
| LOG_LEVEL | 선택 | 로그 레벨. 기본값 INFO |
| LOG_FILE | 선택 | 로그 파일 경로. 기본값 scrimbot.log |
| TEAM_BACKUP_PATH | 선택 | 팀 데이터 백업 파일 경로. 기본값 `data/teams_backup.json` |
| MASTERS_STATE_PATH | 선택 | 마스터즈 진행일 처리 상태 파일 경로. 기본값 `data/masters_state.json` |
| SENTRY_DSN | 선택 | Sentry 에러 추적 DSN |
| NOTION_TOKEN | 선택 | 서버 상태 조회용 Notion 토큰 |
| NOTION_DATABASE_ID | 선택 | 일정 조회용 Notion 데이터베이스 ID |

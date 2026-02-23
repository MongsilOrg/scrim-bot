# ScrimBot - 이터널 리턴 스크림 대회 관리 봇

이터널 리턴(Eternal Return) 스크림(연습 경기) 대회를 관리하는 Discord 봇입니다.
팀 등록, 자동 조편성, MMR 관리, 전적 처리, 패널티 시스템을 제공합니다.

---

## 목차

1. [시작하기](#시작하기)
2. [환경변수 설정](#환경변수-설정)
3. [프로젝트 구조](#프로젝트-구조)
4. [아키텍처](#아키텍처)
5. [핵심 기능](#핵심-기능)
6. [데이터 흐름](#데이터-흐름)
7. [외부 의존성](#외부-의존성)
8. [테스트](#테스트)
9. [운영 가이드](#운영-가이드)

---

## 시작하기

### 사전 요구사항

- **Python 3.10+**
- **wkhtmltoimage** (이미지 생성에 필요)
- **Discord Bot Token** ([Discord Developer Portal](https://discord.com/developers/applications)에서 발급)
- **BSER API Key** ([Eternal Return 개발자 포털](https://developer.eternalreturn.io/)에서 발급)
- **Google Sheets 서비스 계정** (시드팀/패널티 관리용)

### 설치

```bash
# 1. 저장소 클론
git clone <repository-url>
cd scrimbot

# 2. 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# 3. 의존성 설치
pip install -r requirements.txt

# 4. wkhtmltoimage 설치
# macOS
brew install --cask wkhtmltopdf

# Ubuntu/Docker
apt-get update && apt-get install -y wkhtmltopdf

# 5. 환경변수 설정
cp .env.example .env
# .env 파일을 편집하여 실제 값을 입력

# 6. Google Sheets 인증 파일 배치
mkdir -p credentials
# google_sheets_credentials.json 파일을 credentials/ 디렉토리에 복사

# 7. 실행
python main.py
```

### Discord Bot 필수 Intents

Discord Developer Portal에서 다음 **Privileged Gateway Intents**를 활성화해야 합니다:

- **Message Content Intent** - 메시지 내용 읽기 (CSV 파일 처리)
- **Server Members Intent** - 서버 멤버 목록 접근 (닉네임 검증)

---

## 환경변수 설정

프로젝트 루트에 `.env` 파일을 생성하고 아래 항목을 설정합니다.

### `.env.example`

```bash
# ============================================================
# Discord 설정 (필수)
# ============================================================

# Discord 봇 토큰 (Developer Portal > Bot > Token)
DISCORD_TOKEN=MTAzNTUwODY3NzY4OTQ3NTA5Mg.XXXXXX.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# Discord 서버(길드) ID
# 서버 우클릭 > 서버 ID 복사 (개발자 모드 필요)
GUILD_ID=1035508677689475092

# 관리자 역할 ID (쉼표로 구분, 여러 개 가능)
# 이 역할을 가진 사용자만 /스크림 등 관리 명령어 사용 가능
ADMIN_ROLE_IDS=1035511183073099777,1178295996862713916

# ============================================================
# 채널 ID 설정 (필수)
# ============================================================

# 스크림 공지 채널 (조편성 결과, 공지사항이 전송됨)
NOTICE_CHANNEL_ID=1173422674626748417

# 팀 배정 채널 (팀 등록 버튼이 전송되는 채널)
TEAM_ASSIGNMENT_CHANNEL_ID=1212383364258992128

# 자동 조편성 시작 채널 (조편성 시작 알림이 전송됨)
AUTO_ASSIGNMENT_START_CHANNEL_ID=1390999095962767380

# 팀 목록 채널 (등록된 팀 목록이 표시됨)
TEAM_LIST_CHANNEL_ID=1390999095962767380

# 백업 분석 채널 (전적 결과 백업이 전송됨)
BACKUP_ANALYSIS_CHANNEL_ID=1400785133489098842

# 조별 채널 ID (A~F조, 형식: 조:채널ID, 쉼표로 구분)
# 각 조의 CSV 업로드 및 전적 결과가 이 채널에 표시됨
GROUP_CHANNEL_IDS=A:1337238342730776668,B:1337238366667669595,C:1337238442605543455,D:1337238460905553951,E:1337238477879906397,F:1337238497408585779

# ============================================================
# BSER API 설정 (필수)
# ============================================================

# 이터널 리턴 Open API 키
# https://developer.eternalreturn.io/ 에서 발급
BSER_API_KEY=your_bser_api_key_here

# ============================================================
# Google Sheets 설정 (필수)
# ============================================================

# Google Sheets 서비스 계정 인증 파일 경로
GOOGLE_SHEETS_CREDENTIALS_PATH=credentials/google_sheets_credentials.json

# 메인 스프레드시트 ID (시드팀, 테스트 계정, 패널티 공통)
# 스프레드시트 URL에서 /d/ 뒤의 값
# 예: https://docs.google.com/spreadsheets/d/{이 부분}/edit
GOOGLE_SHEETS_MAIN_SPREADSHEET_ID=REDACTED-SHEET-ID

# 패널티 스프레드시트 ID (메인과 동일하게 설정 가능)
GOOGLE_SHEETS_WARNING_SPREADSHEET_ID=REDACTED-SHEET-ID

# ============================================================
# 로깅 설정 (선택)
# ============================================================

# 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL=INFO

# 로그 파일 경로
LOG_FILE=scrimbot.log

# ============================================================
# UI 설정 (선택)
# ============================================================

# 임베드 푸터 텍스트
EMBED_FOOTER_TEXT=ER Scrim | Powered by Mongsil
```

### 환경변수 상세 설명

| 변수명 | 필수 | 기본값 | 설명 |
|--------|------|--------|------|
| `DISCORD_TOKEN` | O | - | Discord 봇 인증 토큰 |
| `GUILD_ID` | O | - | 봇이 운영되는 Discord 서버 ID |
| `ADMIN_ROLE_IDS` | O | - | 관리자 역할 ID (쉼표 구분) |
| `BSER_API_KEY` | O | - | BSER Open API 키 |
| `NOTICE_CHANNEL_ID` | O | - | 공지 채널 ID |
| `TEAM_ASSIGNMENT_CHANNEL_ID` | O | - | 팀 배정 채널 ID |
| `AUTO_ASSIGNMENT_START_CHANNEL_ID` | O | - | 자동 조편성 알림 채널 ID |
| `TEAM_LIST_CHANNEL_ID` | O | - | 팀 목록 채널 ID |
| `BACKUP_ANALYSIS_CHANNEL_ID` | O | - | 전적 백업 채널 ID |
| `GROUP_CHANNEL_IDS` | O | - | 조별 채널 ID (A:id,B:id,...) |
| `GOOGLE_SHEETS_CREDENTIALS_PATH` | O | `credentials/google_sheets_credentials.json` | 서비스 계정 키 파일 경로 |
| `GOOGLE_SHEETS_MAIN_SPREADSHEET_ID` | O | - | 메인 스프레드시트 ID |
| `GOOGLE_SHEETS_WARNING_SPREADSHEET_ID` | O | - | 패널티 스프레드시트 ID |
| `LOG_LEVEL` | X | `INFO` | 로그 출력 레벨 |
| `LOG_FILE` | X | `scrimbot.log` | 로그 파일 경로 |
| `EMBED_FOOTER_TEXT` | X | `ER Scrim \| Powered by Mongsil` | 임베드 하단 텍스트 |

---

## 프로젝트 구조

```
scrimbot/
├── main.py                          # 진입점: 봇 생성, 이벤트/명령어 등록
├── requirements.txt                 # Python 의존성
├── .env                             # 환경변수 (git 추적 제외)
├── example.csv                      # CSV 입력 예시 파일
│
├── bot/                             # 봇 핵심
│   ├── client.py                    #   ScrimBot (discord.py Bot 서브클래스)
│   ├── manager.py                   #   BotManager 싱글톤 (전역 상태 접근점)
│   └── events.py                    #   on_message 핸들러 (CSV 업로드 처리)
│
├── commands/                        # Discord 슬래시 명령어 & 컨텍스트 메뉴
│   ├── scrim.py                     #   /스크림 - 스크림 시작/초기화
│   ├── room_code.py                 #   /방코드 - 방 코드 공지
│   ├── scrim_csv_assign.py          #   컨텍스트 메뉴: CSV 기반 조편성
│   ├── warning.py                   #   컨텍스트 메뉴: 제재 부여
│   └── ui/                          #   Discord UI 컴포넌트
│       ├── views.py                 #     버튼, 셀렉트 메뉴 (쿨다운 포함)
│       └── modals.py                #     모달 (팀 입력, 방코드, 제재 사유)
│
├── models/                          # 비즈니스 로직
│   ├── team_data.py                 #   TeamData 데이터클래스
│   ├── team_data_manager.py         #   팀 등록/취소/백업/MMR 관리
│   ├── team_processor.py            #   BSER API 연동, 조편성, 이미지 생성
│   └── warning_manager.py           #   Google Sheets 기반 패널티 관리
│
├── services/                        # 외부 서비스 연동
│   ├── bser_api.py                  #   BSER API 클라이언트 (캐싱, 재시도)
│   └── image_generator.py           #   HTML → PNG 변환 (MMR/점수표 이미지)
│
├── utils/                           # 유틸리티
│   ├── helpers.py                   #   KST 시간, 관리자 권한 체크
│   ├── validators.py                #   입력 검증 (팀명, 멤버, 중복)
│   ├── error_handlers.py            #   에러 처리 유틸
│   └── file_manager.py              #   임시 파일 관리
│
├── config/                          # 설정
│   ├── settings.py                  #   환경변수 기반 전역 설정
│   └── logging_config.py            #   KST 시간대 로깅 시스템
│
├── assets/
│   └── NanumGothic.ttf              # 한글 폰트 (이미지 생성용)
│
├── tests/
│   └── test_events_logic.py         # 이벤트 로직 단위 테스트
│
├── data/                            # 런타임 데이터 (git 추적 제외)
│   └── teams_backup.json            #   팀 상태 자동 백업
│
└── credentials/                     # 인증 정보 (git 추적 제외)
    └── google_sheets_credentials.json
```

---

## 아키텍처

### 핵심 설계 원칙

```
                    main.py (진입점)
                        │
                        ▼
                ┌──────────────┐
                │   ScrimBot   │  discord.py Bot 서브클래스
                │  (client.py) │  Intents: message_content, guilds, members
                └──────┬───────┘
                       │
                       ▼
              ┌────────────────┐
              │   BotManager   │  싱글톤 - 모든 매니저의 접근점
              │  (manager.py)  │  BotManager.get_instance()
              └───┬────┬────┬──┘
                  │    │    │
        ┌─────────┘    │    └─────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌────────────┐ ┌───────────────┐
│TeamDataMgr   │ │TeamProcessor│ │WarningManager │
│  팀 등록/관리 │ │ 조편성/MMR  │ │  패널티 관리   │
│  백업/복구    │ │ 이미지 생성  │ │  Google Sheets │
└──────┬───────┘ └──────┬─────┘ └───────────────┘
       │                │
       │         ┌──────┴──────┐
       │         ▼             ▼
       │   ┌──────────┐  ┌──────────────┐
       │   │ BSER API │  │ImageGenerator│
       │   │ (캐싱)   │  │ HTML → PNG   │
       │   └──────────┘  └──────────────┘
       │
       ▼
  data/teams_backup.json
```

### BotManager 싱글톤 패턴

`BotManager`는 프로젝트 전체에서 단일 인스턴스로 동작하며, 모든 매니저 객체에 대한 접근점 역할을 합니다.

```python
# 어디서든 동일한 인스턴스에 접근
bot_manager = BotManager.get_instance()

# 각 매니저 획득
team_data_manager = bot_manager.get_team_data_manager()   # 팀 데이터
team_processor = bot_manager.get_team_processor()          # 조편성
warning_manager = bot_manager.get_warning_manager()        # 패널티
```

### 동시성 제어

- `asyncio.Lock()`: 팀 데이터 변경 시 동시성 보호
- `async/await`: 모든 I/O 작업에 비동기 패턴 적용
- 백그라운드 태스크: 자동 조편성, MMR 갱신, 경고 정리

### 캐싱 전략

| 대상 | TTL | 설명 |
|------|-----|------|
| 닉네임 → UserID | 24시간 | BSER API. 변하지 않는 데이터 |
| UserID → MMR | 60초 | BSER API. 5분 주기 갱신 시 API 부하 감소 |
| 조별 이미지 | LRU 50MB | wkhtmltoimage 결과. 최대 10개 보관 |
| 테스트 계정 | 요청 시 | Google Sheets에서 로드 |

---

## 핵심 기능

### 1. 스크림 시작 (`/스크림`)

관리자가 스크림 대회를 시작합니다.

- 관리자 권한 체크 (`ADMIN_ROLE_IDS`)
- 기존 스크림이 있으면 초기화 확인
- 팀 등록 버튼이 포함된 임베드 전송
- 백그라운드 태스크 시작:
  - 자동 조편성 (17:00 KST에 실행, 30초마다 체크)
  - MMR 갱신 (5분 주기)

### 2. 팀 등록

사용자가 버튼을 클릭하여 팀을 등록합니다.

- **팀명**: 2~8자
- **플레이어**: 3~4명 (이터널 리턴 인게임 닉네임)
- **스태프**: 0~3명 (선택)
- 검증:
  - 팀명 글자수
  - 플레이어/스태프 인원수
  - 팀원 중복 검사
  - Discord 서버 멤버 존재 여부
  - 다른 팀과의 멤버 중복 검사
- 등록 즉시 BSER API로 MMR 조회

### 3. 자동 조편성

17:00 KST에 자동으로 팀을 조별로 배정합니다.

- 8팀 단위로 조 구성 (`TEAMS_PER_GROUP = 8`)
- 시드팀 분산 배치 (Google Sheets에서 시드 등급 로드)
- MMR 기반 균형 잡힌 조 편성
- 조별 이미지 생성 (HTML/CSS → wkhtmltoimage → PNG)
- 각 조 채널에 결과 공지
- 음성채널 이름 업데이트

### 4. CSV 전적 처리

조별 채널에 CSV 파일을 업로드하면 자동으로 전적을 처리합니다.

- 이터널 리턴 게임 결과 CSV 파싱
- gameId 추출 및 라운드별 정렬
- 팀별 점수 합산
- 전적 테이블 이미지 생성
- 밴 리스트 추출 (3회 이상 픽된 캐릭터)
- 4라운드 완료 시 결과 백업 전송

### 5. 방 코드 공지 (`/방코드`)

관리자가 게임 방 코드를 공지합니다.

### 6. 제재 시스템 (컨텍스트 메뉴: `제재 부여`)

관리자가 사용자에게 경고/주의를 부여합니다.

- Google Sheets 기반 관리
- 주의 2회 = 경고 1회 자동 환산
- 경고 횟수에 따른 참가 제한 기간 계산
- 만료된 경고 자동 정리 (일일 태스크)
- 패널티 시트(내부용) + 패널티로그 시트(외부 공개용) 이중 관리

---

## 데이터 흐름

### 스크림 생명주기

```
관리자: /스크림 실행
    │
    ▼
[스크림 초기화]
    │  TeamDataManager 생성
    │  자동 조편성 태스크 시작 (17:00 대기)
    │  MMR 갱신 태스크 시작 (5분 주기)
    │
    ▼
[팀 등록 기간] ◄────────────────┐
    │  사용자: 신청/수정 버튼 클릭    │
    │  → TeamModal 표시              │
    │  → 유효성 검사                  │
    │  → BSER API MMR 조회           │
    │  → 백업 저장                    │  반복
    │  → 임베드 업데이트              │
    │                                │
    ├────────────────────────────────┘
    │
    ▼
[17:00 KST 자동 조편성]
    │  시드팀 로드 (Google Sheets)
    │  MMR 기반 조편성
    │  이미지 생성 (HTML → PNG)
    │  조별 채널 공지
    │  음성채널 업데이트
    │
    ▼
[스크림 진행]
    │  관리자: /방코드 공지
    │  매 라운드 CSV 업로드 → 전적 처리
    │  밴 리스트 표시
    │
    ▼
[스크림 종료]
    다음 /스크림 시 초기화
```

### 백업 및 복구

봇이 예기치 않게 종료되어도 팀 데이터가 유지됩니다.

```
[상태 변경 시마다]
    TeamDataManager → data/teams_backup.json 저장

[봇 재시작 시]
    main.py → 백업 파일 확인
    → 날짜 일치 확인 (month, day)
    → 팀 데이터 복구
    → 17시 이전이면 자동 조편성 태스크 재시작
    → MMR 갱신 태스크 재시작
```

**백업 파일 구조** (`data/teams_backup.json`):
```json
{
  "_meta": {
    "scrim_day": 21,
    "scrim_month": 2,
    "scrim_channel_id": 123456789,
    "is_team_assignment_started": false,
    "last_auto_assignment": null
  },
  "teams": {
    "팀이름": {
      "players": ["플레이어1", "플레이어2", "플레이어3"],
      "staff": ["스태프1"],
      "user_id": "123456789012345678",
      "mmr": 5432.1
    }
  }
}
```

---

## 외부 의존성

### Python 패키지

| 패키지 | 버전 | 용도 |
|--------|------|------|
| `discord.py` | >= 2.6.0 | Discord 봇 프레임워크 |
| `aiohttp` | >= 3.9.0 | 비동기 HTTP 클라이언트 (BSER API) |
| `pandas` | >= 2.0.0 | CSV 데이터 처리 |
| `Pillow` | >= 10.0.0 | 이미지 처리 |
| `imgkit` | >= 1.2.0 | HTML → 이미지 변환 (wkhtmltoimage 래퍼) |
| `pytz` | >= 2023.3 | KST 시간대 처리 |
| `python-dotenv` | >= 1.0.0 | .env 파일 환경변수 로드 |
| `gspread` | >= 5.12.0 | Google Sheets API 클라이언트 |
| `google-auth` | >= 2.23.0 | Google API 인증 |

### 시스템 의존성

#### wkhtmltoimage

HTML/CSS를 PNG 이미지로 변환하는 데 사용됩니다. `imgkit`이 내부적으로 호출합니다.

```bash
# macOS
brew install --cask wkhtmltopdf

# Ubuntu / Docker
apt-get update && apt-get install -y wkhtmltopdf
```

설치 후 `wkhtmltoimage` 명령이 PATH에 있어야 합니다. Docker 환경에서는 `/usr/bin/wkhtmltoimage` 경로를 사용합니다.

#### NanumGothic 폰트

이미지에 한글을 렌더링하기 위해 `assets/NanumGothic.ttf` 폰트가 필요합니다. 이 파일은 저장소에 포함되어 있습니다.

### 외부 API

#### BSER API (이터널 리턴)

- **엔드포인트**: `https://open-api.bser.io/v1`
- **인증**: `x-api-key` 헤더
- **사용 API**:
  - `GET /user/nickname?query={닉네임}` - 닉네임으로 유저 ID 조회
  - `GET /user/games/{userNum}` - 유저 게임 정보 (MMR 포함)
- **Rate Limit**: API 키당 요청 제한 있음 (캐싱으로 대응)
- **재시도**: 최대 4회, 지수 백오프 (1s → 2s → 4s → 8s)

#### Google Sheets API

- **인증**: 서비스 계정 (JSON 키 파일)
- **필요한 시트 구조**:

| 시트 이름 | 용도 | 필수 컬럼 |
|-----------|------|-----------|
| `시드팀` | 시드 등급 관리 | 팀명, 시드 등급 |
| `테스트` | 테스트 계정 MMR | 닉네임, MMR |
| `패널티` | 활성 경고 관리 (내부용) | 날짜, 대상, 유형, 사유, 제한 기간 |
| `패널티로그` | 경고 이력 (외부 공개용) | 날짜, 대상, 유형, 사유 |

**Google Sheets 서비스 계정 설정 방법:**

1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트 생성
2. Google Sheets API 활성화
3. 서비스 계정 생성 후 JSON 키 다운로드
4. 키 파일을 `credentials/google_sheets_credentials.json`으로 저장
5. 서비스 계정 이메일을 스프레드시트에 편집자로 공유

---

## 테스트

```bash
# 전체 테스트 실행
python -m pytest tests/

# 단일 파일 실행
python -m pytest tests/test_events_logic.py

# 특정 테스트 실행
python -m pytest tests/test_events_logic.py::EventsLogicTest::test_extract_game_id

# 상세 출력
python -m pytest tests/ -v
```

### 테스트 커버리지

현재 `tests/test_events_logic.py`에서 다음 항목을 테스트합니다:

- CSV 파일명 검증
- 메시지 처리 조건 판별
- gameId 추출 로직
- 팀별 점수 합산
- 밴 리스트 추출
- 임베드 생성

---

## 운영 가이드

### 로깅

로그는 KST 시간대로 기록되며, 파일과 콘솔에 동시 출력됩니다.

- **파일**: `scrimbot.log` (10MB 로테이션, 최대 5개 백업)
- **형식**: `2025-01-15 17:00:00 | scrimbot.module | INFO | [모듈] 동작: 상세내용`
- **레벨**: `LOG_LEVEL` 환경변수로 조정 (기본: `INFO`)
- 외부 라이브러리(discord, aiohttp) 로그는 `ERROR` 레벨만 출력

### Discord 서버 채널 구성 권장사항

```
📢 공지
├── #스크림-공지          ← NOTICE_CHANNEL_ID
├── #팀-배정              ← TEAM_ASSIGNMENT_CHANNEL_ID
└── #팀-목록              ← TEAM_LIST_CHANNEL_ID

📊 조별 채널
├── #group-a              ← GROUP_CHANNEL_IDS의 A
├── #group-b              ← GROUP_CHANNEL_IDS의 B
├── #group-c              ← GROUP_CHANNEL_IDS의 C
├── #group-d              ← GROUP_CHANNEL_IDS의 D
├── #group-e              ← GROUP_CHANNEL_IDS의 E
└── #group-f              ← GROUP_CHANNEL_IDS의 F

🔊 음성 채널 (카테고리별)
├── Group A/
│   ├── 🔊 Team 1
│   └── 🔊 Team 2 ...
├── Group B/ ...
└── ...

🔧 관리
├── #자동조편성            ← AUTO_ASSIGNMENT_START_CHANNEL_ID
└── #전적백업              ← BACKUP_ANALYSIS_CHANNEL_ID
```

### CSV 파일 형식

조별 채널에 업로드하는 CSV 파일은 이터널 리턴 게임 결과 형식을 따릅니다. 참고용 예시 파일: `example.csv`

```csv
team_name,players,staff
팀이름,"플레이어1, 플레이어2, 플레이어3, 플레이어4",스태프1
```

### 문제 해결

| 증상 | 원인 | 해결 방법 |
|------|------|-----------|
| 봇이 시작되지 않음 | 환경변수 누락 | `.env` 파일의 필수 값 확인 |
| 명령어가 표시되지 않음 | 명령어 동기화 실패 | `GUILD_ID` 확인, 봇 재시작 |
| 이미지가 생성되지 않음 | wkhtmltoimage 미설치 | `wkhtmltoimage --version`으로 설치 확인 |
| MMR 조회 실패 | BSER API 키 오류 | `BSER_API_KEY` 값 확인 |
| Google Sheets 접근 실패 | 인증 파일 오류 | `credentials/google_sheets_credentials.json` 확인, 시트 공유 설정 확인 |
| 팀 등록 시 멤버 미발견 | Discord Members Intent 비활성 | Developer Portal에서 Server Members Intent 활성화 |
| 백업 복구 안 됨 | 날짜 불일치 | 백업은 당일(month/day 기준)만 복구됨 |

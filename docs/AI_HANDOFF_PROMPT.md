# srtgo 봇 개발 인수인계 프롬프트 (다른 AI에게 그대로 붙여넣기용)

아래 전체를 다른 AI 코딩 어시스턴트에게 붙여넣으면 이 프로젝트를 바로 이어서 개발할 수 있다.

---

너는 지금부터 `shbshb99/srtgo` 저장소의 텔레그램 열차 예매 봇을 이어서 개발한다.
아래는 프로젝트의 현재 상태, 구조, 운영 방식, 수정·배포 방법이다. 이걸 전제로 작업하라.

## 프로젝트 개요
- 저장소: `https://github.com/shbshb99/srtgo` (원 저장소 `lapis42/srtgo` 의 포크)
- 작업 브랜치: `claude/srtgo-ktx-compatibility-7gczbc`
- 목적: 코레일(KTX)·SRT 열차표를 텔레그램 봇으로 검색·대기·예매하고, Windows 운영 PC에서
  상시 가동하며, 폰(텔레그램)만으로 업데이트·관리까지 하는 시스템.
- 언어/런타임: Python 3.10+ (운영 PC는 3.12). 패키징은 `pyproject.toml` + setuptools-scm.
- 테스트: `python -m pytest tests -q` (현재 112개 통과). 수정 후 반드시 통과시킬 것.

## 소스 파일 (`srtgo/`)
| 파일 | 역할 |
|---|---|
| `ktx.py` | 코레일 API 클라이언트. `Korail` 클래스 — 로그인·열차조회(`search_train`)·예매(`reserve`)·결제·취소. 상단에 `KORAIL_APP_VERSION="7.0.10"`, `KORAIL_API_VERSION="250601003"`. `korail_versions()` 가 `~/.srtgo/korail.json` 값을 우선 사용. |
| `srt.py` | SRT API 클라이언트(원 저장소). 2026-09 KTX 통합 이후 예매엔 미사용. |
| `dynapath.py` | 코레일 요청 헤더 토큰(`x-dynapath-m-token`) 생성. |
| `korail_version.py` | 앱 버전 관리. `is_blocked(ex)`(-8204 판정), `probe()`(로그인 없이 조회로 버전 확인), `heal()`(버전 탐색), `GuardedSession`(세션 래퍼). `AUTO_HEAL=False`. |
| `bot.py` | 봇 본체(~2,900줄). `Bot(OpsMixin)` 클래스. 화면·대기루프·예매 전부. |
| `ops.py` | 오너용 `/admin` 관리 화면(업데이트·재시작·기록·버전 점검·앱버전 입력). `Bot` 믹스인. |
| `updater.py` | 자동 업데이트. 저장소 `updates/latest.json` 확인→휠 다운로드→SHA256 검증→설치. |
| `watchdog.py` | 봇 상시 가동. 죽거나 하트비트 끊기면 재시작. 업데이트 설치 수행. 봇 코드를 import하지 않음. |
| `srtgo.py` | 원래 CLI(터미널 메뉴): 텔레그램/로그인/카드 설정 입력. |

진입점(`[project.scripts]`): `srtgo`→`srtgo.srtgo:srtgo`, `srtgo-bot`→`srtgo.bot:main`, `srtgo-watchdog`→`srtgo.watchdog:main`.

## 봇 구조 (`bot.py`)
- 명령: `/start /status /stop /settings /help /admin`(`COMMANDS` 튜플, `/admin`은 오너 전용).
- 버튼: 콜백데이터 `action:arg` → `btn_<action>(self, r, arg)` 메서드로 분기.
  새 버튼은 `btn_<이름>` 메서드 추가 + 키보드에 `("라벨","<이름>:인자")`.
- 화면: `Reply` 객체의 `await r.show(text, markup)`. `kb([[("라벨","data")]])` 로 인라인 키보드. `HOME=("↩︎ 처음으로","home")`.
- 대기루프: `_watch(self, u, w)` — 로그인→조회→(자리나면)예매 반복.
  오류는 `classify(ex)` 로 `login/netfunnel/benign/blocked/error` 분류.
- 상태: `StateStore`(`bot_state.json`). 대기=`Watch`, 사용자별 설정=`UserContext`.
- 다중 사용자: 모르는 사람이 말 걸면 오너 승인 요청. 승인자는 자기 코레일 계정 연결.
  상수: `MAX_CONCURRENT_WATCHES`, `MAX_WATCHES_PER_USER`, `RELOGIN_EVERY_ERRORS`, `HEARTBEAT_SECONDS` 등.
- 계정 비밀번호·카드는 파일이 아니라 Windows keyring(`CRED_SERVICE="srtgo-bot-cred"`)에만 저장.
- 2026-09-01 SRT→KTX 통합 반영: 코레일 계정 하나로 수서·동탄·평택지제 열차까지 예매(`BOOKING_RAIL="KTX"`).

## 운영 PC 구조 (Windows)
```
작업 스케줄러('srtgo-bot', AtStartup)
  └ pythonw -m srtgo.watchdog          ← 워치독(상시)
      ├ 봇을 자식으로: python -m srtgo.bot (20초마다 heartbeat 기록)
      ├ 하트비트 끊기면 봇 강제종료 후 재시작
      └ 30분마다 updates/ 확인 → 새 버전 설치
```
데이터 폴더 `%USERPROFILE%\.srtgo`(= `SRTGO_HOME`): `bot_state.json`, `watchdog.log`,
`bot.log`, `bot.console.log`, `heartbeat`, `korail.json`, `update_*.json`,
`update_approved`, `restart.request`, `autoupdate.off|auto`.

## 자동 업데이트 (`updater.py` + `watchdog.py` + `ops.py`)
- 채널: 저장소의 `updates/latest.json` = `{version, wheel, sha256, notes}` + 같은 폴더의 휠.
  확인은 GitHub API 먼저(`contents/updates/latest.json?ref=<branch>`), 실패 시 raw.
- 흐름: 새 버전 감지→휠 다운로드→SHA256 검증→봇 종료(`restart.request`)→`pip install`→봇 재시작.
  새 버전이 2분 내 죽으면 이전 코드로 자동 롤백 후 그 버전 skip.
- 모드(폰 `/admin` 또는 `~/.srtgo` 파일): `승인 후 설치`(기본)/`자동 설치`(autoupdate.auto)/`꺼짐`(autoupdate.off).
- 폰 관리(`/admin`, 오너): 버전 표시 + 🔄 지금 확인 · 모드 전환 · 🚅 코레일 버전 점검 · ✏️ 앱버전 입력 · 📜 기록 · 🔁 재시작.
- 옛 워치독은 새 봇이 감지해 새 워치독으로 교체(워치독도 원격 업데이트됨).

## 수정 → 배포 방법
```bash
git clone https://github.com/shbshb99/srtgo && cd srtgo
git checkout claude/srtgo-ktx-compatibility-7gczbc
python -m pip install -e .
python -m pytest tests -q            # 수정 전/후 모두

# 코드 수정 후, 새 버전 배포:
python scripts/publish_update.py 2.2.2+ktx.N "바뀐 점(오너 폰에 표시)"
git add -A && git commit -m "feat: ..." && git push origin claude/srtgo-ktx-compatibility-7gczbc
```
`publish_update.py`: 휠 빌드 → `updates/` 에 그 휠만 남기고 복사 → `updates/latest.json` 갱신.
운영 PC는 이걸 30분 내 받아 설치(또는 폰 승인). 버전은 `2.2.2+ktx.N` 형식, N 증가.

## 현재 상태 / 열린 과제
- 코레일 서버가 현재 이 클라이언트의 KTX 조회·예매 요청에 `-8204`("최신 버전으로 업데이트")를
  반환한다. 요청 `AppVersion` 을 7.0.8→7.0.10으로, 요청 형식도 최신 앱에 맞춰 바꿨으나
  응답은 동일하다(버전 번호 변경으로는 응답이 바뀌지 않음). 대기 루프는 이 응답을 만나면
  멈추고 오너에게 통보한다(`bot.py: _blocked`). 버전 자동 탐색은 `AUTO_HEAL=False` 로 꺼져 있다.
- 봇의 설치·관리·자동 업데이트·워치독·다중 사용자·상태 저장 체계는 정상 동작한다.
- 테스트 112개 통과 상태. 기능 추가/수정 시 `tests/` 에 대응 테스트를 추가하라.

## 코드 스타일
- 한국어 주석/사용자 문구. 기존 코드의 주석 밀도·명명·관용구에 맞춘다.
- 외부 의존성 최소화(워치독·updater는 표준 라이브러리만).
- 커밋 메시지는 한국어 요약 한 줄 + 본문.

여기까지가 현재 상태다. 무엇부터 할지 사용자의 지시를 받아 진행하라.

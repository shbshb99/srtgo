# srtgo 봇 — 인수인계 문서 (직접 고치고 운영하기)

이 포크(`shbshb99/srtgo`)에 추가한 **텔레그램 예매 봇**과 그 운영 체계를 직접
수정·배포·운영하기 위한 문서다. 코레일 API를 호출하는 원래 라이브러리(`ktx.py`,
`srt.py`) 자체의 내부는 필요한 부분만 짚는다.

- 저장소: `https://github.com/shbshb99/srtgo`
- 작업 브랜치: `claude/srtgo-ktx-compatibility-7gczbc`
- 운영 PC 데이터 폴더: `%USERPROFILE%\.srtgo` (환경변수 `SRTGO_HOME` 으로 변경 가능)
- 파이썬: 3.10+ (운영 PC는 3.12)

> **현재 상태(2026-10):** 코레일이 자동 예매 프로그램의 접속을 차단(`-8204`)해서
> 봇으로 KTX 조회·예매가 되지 않는다. 앱 버전 번호(7.0.8 → 7.0.10) 문제가 아니라
> 접속 자체를 막은 것으로 확인됐다. 봇의 **설치·관리·자동 업데이트 체계는 정상
> 동작**한다. 이 문서는 그 체계를 직접 다루기 위한 것이다.

---

## 1. 소스 파일 지도 (`srtgo/`)

| 파일 | 역할 |
|---|---|
| `ktx.py` | 코레일(KTX) API 클라이언트. 로그인·열차조회·예매·결제·취소. `Korail` 클래스. |
| `srt.py` | SRT API 클라이언트(원 저장소). 2026-09 통합 이후 예매엔 안 씀. |
| `dynapath.py` | 코레일 요청에 붙는 보안 토큰(`x-dynapath-m-token`) 생성. |
| `korail_version.py` | 앱 버전 관리. `-8204`(차단) 판정, 버전 저장/탐색, 세션 래퍼. |
| `bot.py` | **텔레그램 봇 본체**(~2,900줄). 모든 화면·대기 루프·예매 로직. |
| `ops.py` | 오너용 `/admin` 관리 화면(업데이트·재시작·기록·버전). `Bot` 에 믹스인. |
| `updater.py` | 자동 업데이트(저장소 `updates/` 확인→다운로드→검증). 워치독이 사용. |
| `watchdog.py` | 봇을 상시 가동. 죽거나 멈추면 재시작. 업데이트 설치 수행. |
| `srtgo.py` | 원래 CLI(터미널 메뉴). 텔레그램·로그인·카드 설정 입력 등. |

진입점(`pyproject.toml` [project.scripts]):
- `srtgo` → `srtgo.srtgo:srtgo` (CLI)
- `srtgo-bot` → `srtgo.bot:main`
- `srtgo-watchdog` → `srtgo.watchdog:main`

테스트: `tests/` (pytest). `python -m pytest tests -q` — 현재 135개 통과.

---

## 2. 운영 PC에서 돌아가는 구조

```
작업 스케줄러(AtStartup, 'srtgo-bot' 태스크)
   └─ pythonw -m srtgo.watchdog         ← 워치독(상시)
        ├─ 봇을 자식으로 띄움: python -m srtgo.bot
        │     └─ 하트비트 파일에 20초마다 생존 신호
        ├─ 하트비트 끊기면 봇을 강제 종료 후 재시작
        └─ 30분마다 저장소 updates/ 확인 → 새 버전 설치
```

- 워치독은 봇 코드를 **import 하지 않는다**(봇이 깨져도 워치독은 살아남아 재시작·복구).
- 봇은 재시작돼도 `bot_state.json` 에 저장된 대기를 이어간다.
- 같은 텔레그램 봇 토큰을 **두 PC에서 동시에 돌리면 안 된다**(버튼이 번갈아 먹통).

### 데이터 폴더 `%USERPROFILE%\.srtgo`
| 파일 | 내용 |
|---|---|
| `bot_state.json` | 사용자·설정·대기(비밀번호 없음) |
| `watchdog.log` / `bot.log` / `bot.console.log` | 로그 |
| `heartbeat` | 봇 생존 신호(워치독이 감시) |
| `korail.json` | 코레일 앱 버전 저장 `{"app_version","version","updated"}` |
| `update_*.json`, `update_approved`, `restart.request` | 업데이트 주고받는 파일 |
| `autoupdate.off` / `autoupdate.auto` | 업데이트 모드 스위치(파일 유무로) |

계정 비밀번호·카드는 파일이 아니라 **Windows 자격증명 관리자(keyring)** 에만 저장된다
(`CRED_SERVICE = "srtgo-bot-cred"`).

---

## 3. 코레일 버전 / `-8204` 차단 처리

핵심 파일: `srtgo/ktx.py`, `srtgo/korail_version.py`

- 요청에 들어가는 값(`ktx.py` 상단):
  - `KORAIL_APP_VERSION = "7.0.10"` (앱 버전, 요청의 `AppVersion`)
  - `KORAIL_API_VERSION = "250601003"` (API 계약 번호, 요청의 `Version`)
  - `korail.json` 에 값이 있으면 그걸 우선 사용(`korail_versions()`).
- `-8204` 판정: `korail_version.is_blocked(ex)` — 메시지/코드에 `-8204` 포함 여부.
- `AUTO_HEAL = False` (`korail_version.py`): 막혔을 때 **버전을 자동으로 찾아 두드리는
  동작은 꺼 둠**. 켜면 수 분간 코레일에 수백 번 조회를 보내게 돼 위험. 오너가
  `/admin` 에서 직접 점검/입력할 때만 탐색한다.
- 대기 루프가 `-8204` 를 만나면 조회를 멈추고 오너에게 알린다(`bot.py: _blocked`).

### 앱 버전만 바꿔 테스트하고 싶을 때(코드 수정 없이)
운영 PC에서 `korail.json` 을 만들거나, 폰에서 `/admin → ✏️ 앱 버전 입력`.
```json
{ "app_version": "7.0.11", "version": "250601003" }
```
저장 후 봇 재시작(또는 `/admin → 🔁 봇 재시작`).

---

## 4. 텔레그램 봇 뼈대 (`bot.py`)

- `Bot(OpsMixin)` 클래스가 전부를 들고 있다. `build_application(bot, token)` 이
  핸들러를 등록하고, `run_bot()` 이 실행, `main()` 이 진입점.
- **명령**: `/start /status /stop /settings /help /admin` (`COMMANDS` 튜플, `/admin`은 오너).
- **버튼 처리**: 콜백데이터 `action:arg` 를 `btn_<action>(self, r, arg)` 메서드로 보낸다.
  예: 버튼 데이터 `book:KTX` → `btn_book`, `set` → `btn_set`. 새 버튼을 추가하려면
  `btn_<이름>` 메서드를 만들고 키보드에 `("라벨","<이름>:인자")` 를 넣으면 된다.
- **화면 그리기**: `Reply` 객체의 `await r.show(text, markup)`. `kb([[("라벨","data")]])`
  로 인라인 키보드를 만든다. `HOME = ("↩︎ 처음으로","home")`.
- **대기 루프**: `_watch(self, u, w)` — 로그인→조회→(자리나면)예매 반복. 오류는
  `classify(ex)` 로 분류: `login / netfunnel / benign / blocked / error`.
- **상태 저장**: `StateStore`(`bot_state.json`). 대기는 `Watch`, 사용자별 설정은 `UserContext`.

빠른 예) "도움말에 한 줄 추가":
`show_help()` 의 텍스트 리스트에 줄 추가 → `pytest` → 아래 5번으로 배포.

---

## 4-1. 예약대기 챙기기 (`Reminder`)

코레일이 자동 예매를 막은(`-8204`) 뒤에 넣은 기능. 코레일 공식 **예약대기** 의 기한만
챙긴다. **코레일에 요청을 한 번도 보내지 않는다** — 그래서 매크로 판정에 걸리지 않는다.

- 데이터: `Reminder` 클래스, `bot_state.json` 의 `reminders` (사용자별 배열).
  상태는 `등록 → 신청함 → 배정됨 → 결제함`, 그 밖에 `놓침`·`종료`.
- 화면: 홈 첫 줄 `예약대기 챙기기`(`rm`). 등록은 날짜 → 출발역 → 도착역 →
  "열차번호 출발시각 메모" 한 줄(건너뛰기 가능). 건을 열면 `신청했음`·`배정됨`·
  `결제했음`·`결제 기한 바꾸기`·`삭제`.
- 콜백 이름은 전부 알파벳만 쓴다(`rm`, `rmnew`, `rmdate`, `rmdep`, `rmarr`, `rmall`,
  `rmskip`, `rmopen`, `rmstate`, `rmpay`, `rmdel`, `rmdelok`). `_on_button` 이
  `action.isalpha()` 인 것만 처리하기 때문이다.
- 알림: `reminder_notices(m, now)` 가 지금 보낼 (키, 문구, 중요) 목록을 돌려주고,
  `_standby_tick()` 이 보내고 `sent` 에 키를 남긴다. 키는 한 번만 쓴다.

  | 키 | 언제 | 상태 |
  |---|---|---|
  | `apply3` | 출발 3일 전 09:00 | 등록 |
  | `apply2` | 출발 2일 전 09:00 | 등록 |
  | `apply2e` | 출발 2일 전 18:00 | 등록 |
  | `cancelwin` | 출발 전날 09:00 | 등록·신청함 |
  | `payam` | 배정일 09:00 | 배정됨 |
  | `pay2h` | 결제 기한 2시간 전 | 배정됨 |
  | `pay30m` | 결제 기한 30분 전 (중요 알림) | 배정됨 |
  | `paymiss` | 결제 기한 +10분 (중요 알림, 상태를 `놓침` 으로) | 배정됨 |
  | `depday` | 출발일 07:00 | 결제함 |

- **기한이 이미 지난 알림은 보내지 않는다.** 봇이 꺼져 있던 동안 지나간 것이 켜지자마자
  쏟아지지 않게. 출발 시각이 지난 건은 조용히 `종료` 로 바뀐다.
- 도는 자리: 새 루프를 만들지 않고 `_heartbeat` 안에서 `_ops_tick()` 다음에
  `_standby_tick()` 을 부른다(20초마다, 하는 일은 날짜 비교뿐).
- 기본값: 신청 마감 `APPLY_DEADLINE_DAYS = 2`, 결제 기한 `PAY_DEADLINE_DEFAULT = "1800"`,
  한 사람당 `MAX_REMINDERS_PER_USER = 10`.
  결제 기한 18시는 코레일 승차권 안내 기준이다. 배정 문자에 다른 시각이 적혀 있으면
  건별로 고칠 수 있고, 고치면 그 건의 `pay*` 알림 기록을 지워 새 기한으로 다시 울린다.
- 테스트: `tests/test_reminders.py` (23개). 알림은 시각을 넣어 돌리므로 실제 시계에
  의존하지 않는다.

---

## 5. 수정 → 배포 흐름 (내가 직접)

### 5-1. 코드 고치고 확인
```bash
git clone https://github.com/shbshb99/srtgo
cd srtgo
git checkout claude/srtgo-ktx-compatibility-7gczbc
python -m pip install -e .
python -m pytest tests -q          # 고치기 전에 한 번, 고친 뒤 또
```

### 5-2. 새 버전 올리기 (자동 업데이트 채널)
버전은 `2.2.2+ktx.N` 형식으로 하나 올린다(N 증가).
```bash
python scripts/publish_update.py 2.2.2+ktx.18 "바뀐 점 (오너 폰에 표시됨)"
git add -A
git commit -m "feat: ...(설명)"
git push origin claude/srtgo-ktx-compatibility-7gczbc
```
`publish_update.py` 가 하는 일:
1. 휠 빌드(`srtgo-2.2.2+ktx.18-py3-none-any.whl`)
2. `updates/` 에 그 휠만 남기고(이전 것 삭제) 복사
3. `updates/latest.json` 갱신: `{version, wheel, sha256, notes}`

### 5-3. 운영 PC가 받아가는 과정(자동)
- 워치독이 30분마다 `updates/latest.json` 확인(먼저 GitHub API, 실패 시 raw).
- 새 버전이면: 휠 다운로드 → **SHA256 검증** → 봇 끔(`restart.request`) → `pip install`
  → 봇 재시작. 2분 안에 죽으면 이전 버전으로 **자동 롤백** 후 그 버전 건너뜀.
- 모드(`autoupdate.*` 또는 `/admin`):
  - `승인 후 설치`(기본): 폰에 [✅ 지금 설치] 버튼이 오면 눌러야 설치.
  - `자동 설치`: 묻지 않고 설치.
  - `꺼짐`: 업데이트 안 함.
- 결과(설치됨/실패/롤백)는 텔레그램으로 통보.

### 5-4. 폰에서 관리 (`/admin`)
프로그램/워치독 버전, 마지막 확인, 코레일 앱 버전 표시 + 버튼:
🔄 지금 확인 · 업데이트 모드 전환 · 🚅 코레일 버전 점검 · ✏️ 앱 버전 입력 ·
📜 기록 보기 · 🔁 봇 재시작.

### 5-5. 운영 PC에 직접(강제) 설치할 때
(자동을 기다리지 않고 지금 바로) — PowerShell:
```powershell
& {
  $name='srtgo-2.2.2+ktx.18-py3-none-any.whl'
  $want='<publish 때 출력된 sha256>'
  $url='https://raw.githubusercontent.com/shbshb99/srtgo/claude/srtgo-ktx-compatibility-7gczbc/updates/'+$name
  $whl=Join-Path $env:TEMP $name
  Invoke-WebRequest $url -OutFile $whl -UseBasicParsing
  if((Get-FileHash $whl -Algorithm SHA256).Hash.ToLower() -ne $want){Write-Host 'SHA 불일치';return}
  Stop-ScheduledTask -TaskName 'srtgo-bot' -EA SilentlyContinue
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" | ?{$_.CommandLine -match 'srtgo'} | %{Stop-Process -Id $_.ProcessId -Force}
  python -m pip install --disable-pip-version-check $whl
  python -c "import importlib.metadata as m; print('설치:', m.version('srtgo'))"
  Start-ScheduledTask -TaskName 'srtgo-bot'
}
```

---

## 6. 운영 PC 최초 설정 (새 PC)

1. python.org 에서 3.12 설치(첫 화면 **Add python.exe to PATH** 체크).
2. 휠 설치(위 5-5 방식, 또는 설치 페이지).
3. `srtgo` 실행 → `텔레그램 설정`(봇 토큰·내 chat_id), `로그인 설정`→KTX(코레일),
   필요시 `카드 설정`.
4. 상시 가동 등록(관리자 PowerShell, 비밀번호 저장형):
```powershell
& {
  $py=(python -c "import sys;print(sys.executable)").Trim()
  $pw=Join-Path (Split-Path $py) 'pythonw.exe'; if(-not(Test-Path $pw)){$pw=$py}
  powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
  $u="$env:USERDOMAIN\$env:USERNAME"
  $c=Get-Credential -UserName $u -Message 'PC 로그인 비밀번호'
  $a=New-ScheduledTaskAction -Execute $pw -Argument '-m srtgo.watchdog' -WorkingDirectory $env:USERPROFILE
  $t=New-ScheduledTaskTrigger -AtStartup
  $s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName 'srtgo-bot' -Action $a -Trigger $t -Settings $s -User $u -Password $c.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
  Start-ScheduledTask -TaskName 'srtgo-bot'
}
```

---

## 7. 자주 보는 로그 / 문제 해결

```powershell
Get-Content "$env:USERPROFILE\.srtgo\watchdog.log" -Tail 15 -Encoding UTF8
Get-Content "$env:USERPROFILE\.srtgo\bot.log" -Tail 30 -Encoding UTF8
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | ?{$_.CommandLine -match 'srtgo'} | Select ProcessId,CommandLine | fl
```
- `bot.log` 에 "텔레그램 설정이 없습니다": 태스크가 다른 Windows 계정으로 돎 → srtgo 설정한 계정으로 태스크 재등록.
- "다른 PC에서도 실행 중": 다른 PC의 봇을 꺼라.
- 검색 실패 `-8204`: 코레일 차단(현재 상황). 봇은 대기를 멈춘다.
- 봇이 안 올라옴: 위 프로세스 목록에 `srtgo.bot` 없으면 `Start-ScheduledTask -TaskName 'srtgo-bot'`.

---

## 8. 커밋/PR 서명 (현재 세션 기준)

커밋 메시지 끝:
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012vugxuwAKuKJCHY6TikZiz
```
PR 본문 끝:
```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_012vugxuwAKuKJCHY6TikZiz
```

---

## 9. 선 (중요)

코레일의 자동 예매 차단(`-8204`)을 **우회·무력화하는 수정은 하지 않는다**.
코레일이 일반 승객 보호를 위해 넣은 장치이고, 약관 위반으로 계정이 정지될 수 있다.
버전 번호를 올리는 정도가 아니라 보안 토큰/탐지를 흉내 내어 차단을 뚫는 방향은
이 프로젝트의 범위 밖이다.

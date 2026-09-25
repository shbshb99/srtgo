# SRTgo: K-Train (KTX, SRT) Reservation Assistant
[![Upload Python Package](https://github.com/lapis42/srtgo/actions/workflows/python-publish.yml/badge.svg)](https://github.com/lapis42/srtgo/actions/workflows/python-publish.yml)
[![Downloads](https://static.pepy.tech/badge/srtgo)](https://pepy.tech/project/srtgo)
[![Downloads](https://static.pepy.tech/badge/srtgo/month)](https://pepy.tech/project/srtgo)
[![Python version](https://img.shields.io/pypi/pyversions/srtgo)](https://pypistats.org/packages/srtgo)

> [!NOTE]
> 공정한 예매 문화 조성을 위해 본 프로젝트의 개발 및 지원을 중단하기로 결정했습니다. 양해 부탁드립니다.

> [!WARNING]
> 본 프로그램의 모든 상업적, 영리적 이용을 엄격히 금지합니다. 본 프로그램 사용에 따른 민형사상 책임을 포함한 모든 책임은 사용자에게 있으며, 본 프로그램의 개발자는 민형사상 책임을 포함한 어떠한 책임도 부담하지 않습니다. 본 프로그램을 내려받음으로써 모든 사용자는 위 사항에 이의 없이 동의하는 것으로 간주됩니다.

---
> [!NOTE]
> I have decided to discontinue the development and support for this project. Thank you for your understanding.

> [!WARNING]
> All commercial and profit-making use of this program is strictly prohibited. Use of this program is at your own risk, and the developers of this program shall not be liable for any liability, including civil or criminal liability. By downloading this program, all users are deemed to agree to the above terms without any objection.

## 텔레그램 봇 (이 포크에서 추가)
터미널 없이 텔레그램 버튼으로 검색·대기·예매·결제·취소를 합니다. 운영PC에 한 번 설정해 두고 항상 켜 두는 용도입니다.

- 준비: `srtgo` 를 실행해 `텔레그램 설정`(봇 토큰, 내 chat_id)을 먼저 합니다. 오너 계정은 `로그인 설정`, 자동 결제를 쓰려면 `카드 설정`도 합니다.
- 실행: `srtgo-bot` (또는 `srtgo` 메뉴의 `텔레그램 봇 시작`). 항상 켜 두려면 `srtgo-watchdog` 으로 띄웁니다. 창 없이 돌리려면 `pythonw -m srtgo.watchdog`.
  워치독은 봇이 죽거나 멈추면(하트비트 끊김) 다시 띄웁니다. 봇은 재시작돼도 진행 중이던 대기를 이어갑니다.
- 여러 사람: 모르는 사람이 말을 걸면 오너에게 승인 요청이 옵니다. 승인된 사람은 `⚙️ 설정 → 🔑 계정 연결`에서 자기 코레일/SRT 계정을 연결해 자기 예매만 합니다. 계정 메시지는 받자마자 지웁니다.
- 결제: 카드는 오너 것만 PC에 있으므로, 카드 결제(자동 결제 포함)는 오너만 할 수 있습니다. 다른 사람은 코레일톡/SRT 앱에서 결제합니다.
- 설정은 사람마다 따로: 자주 쓰는 역(⭐), 역 직접 추가, 승객 유형(어린이·경로·장애인), KTX만 검색.
- 기록: `~/.srtgo/` (`SRTGO_HOME` 으로 변경) 에 `bot.log`, `watchdog.log`, `bot.console.log`, 상태 파일 `bot_state.json`(비밀번호는 없음, 계정은 OS 자격증명 저장소에만).
- 같은 봇 토큰으로 두 곳에서 동시에 돌리면 안 됩니다 (버튼이 번갈아 먹통이 됨).

## Acknowledgments
- This project includes code from [SRT](https://github.com/ryanking13/SRT) by ryanking13, licensed under the MIT License, and [korail2](https://github.com/carpedm20/korail2) by carpedm20, licensed under the BSD License.
- `srtgo/dynapath.py` is adapted from [korail-mobile-api](https://github.com/yakisoba0728/korail-mobile-api) by yakisoba0728, licensed under the Apache License 2.0. See `NOTICE`.

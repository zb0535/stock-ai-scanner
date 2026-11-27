# 브로커 연동 가이드 (모의투자 → 실계좌)

지금은 **모의투자만** 돈다. 이 문서는 증권사 Open API 키를 발급받았을 때
**어디를 고치면 되는지**를 순서대로 정리한 것이다. 목표는 "키만 넣으면 붙는다"이고,
그 전까지는 어떤 경로로도 실주문이 나가지 않게 막아 두었다.

- 코드: `broker.py` (어댑터·모델·안전장치) / `paper_trading.py` (전략·후보·현금 한도)
- 상태 확인: `GET /` 의 `broker` 블록 (모드·실계좌 여부·킬 스위치·한도·어댑터별 키 설정 여부)
- 테스트: `tests/test_broker_guard.py` (`python -m unittest tests.test_broker_guard -v`)

---

## 1. 주문이 흐르는 길

```
paper_trading.run(market)                 전략·후보·현금/비중 한도 판단
  └─ _execute_buy / _execute_sell         주문 1건을 만든다(OrderRequest)
       └─ broker.get_adapter(market)      BROKER_MODE가 고른 어댑터 + 안전장치
            └─ GuardedBroker             OrderGuard 검사 → 드라이런/차단/실행
                 └─ PaperBroker          모의투자: paper_trading._fill_buy / _fill_sell
                      └─ paper_store     포지션·현금·체결 기록(한 트랜잭션)
                 └─ KisBroker 등         실계좌: 증권사 API 호출 (아직 자리만 있음)
```

- **판단과 체결을 갈라 놓았다.** 전략·후보·현금 한도는 `paper_trading`에 그대로 있고,
  증권사가 바뀌어도 손대지 않는다. 증권사별 차이는 `BrokerAdapter` 구현 하나에만 있다.
- `PaperBroker`는 회계를 새로 쓰지 않고 기존 체결 엔진(`_fill_buy`/`_fill_sell`)을 **연결만**
  한다. 그래서 모의투자 수치(수수료·증권거래세·환차·수량 단위·현금 방어선)는 예전과 100% 같다.

### 인터페이스

| 요소 | 내용 |
| --- | --- |
| `OrderRequest` | `side`(BUY/SELL), `ticker`, `market`, `shares`, `price`, `currency`, `amount_krw`, `fx_rate`, `reason`, `run_id`, `account_id`, `extra` |
| `OrderResult` | `ok`, `status`(`filled`·`dry_run`·`blocked`·`broker_not_configured`·`rejected`), `trade`, `order_id`, `reason`, `guard` |
| `BrokerAdapter` | `name`, `is_live`, `configured`, `place_buy`, `place_sell`, `get_balance`, `health` |
| `BrokerAccountInfo` | `cash_krw`, `positions`, `status` — 모의투자는 원가 기준, 실계좌는 증권사 잔고 |
| `BrokerHealth` | 키 설정 여부(`missing_env`)·구현 여부(`implemented`)·킬 스위치·드라이런 |

---

## 2. 환경변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `BROKER_MODE` | `paper` | `paper` / `kis` / `toss` / `mirae`. 모르는 값이면 `paper`로 내려간다(로그 경고) |
| `REAL_TRADING_ENABLED` | `false` | **킬 스위치.** `0/false/off`면 실계좌 주문은 전부 차단 |
| `REAL_DRY_RUN` | `true` | 실주문 드라이런. 켜져 있으면 주문을 보내지 않고 "보냈다면 어떻게 됐을지"만 반환 |
| `REAL_ORDER_MAX_KRW` | `100000` | 1회 **매수** 주문 상한(원) |
| `REAL_DAILY_MAX_KRW` | `1000000` | 1일 누적 **매수** 주문 상한(원) |
| `BROKER_GUARD_STATE` | (SQLite 파일 옆 `broker_guard.json`) | 가드 상태(1일 누적·중복 키) 파일 경로. 빈 값이면 메모리만 |
| `BROKER_CRED_KEY` | — | **권장.** 화면에서 등록한 증권사 키를 암호화할 재료(문장 아무거나). 없으면 `DATABASE_URL` 등 서버 비밀에서 유도하고, 그것도 없으면 등록을 거부한다(평문 저장 없음) |
| `BROKER_CRED_TOKEN` | — | **권장.** 등록/삭제 요청에 `X-Broker-Token` 헤더를 요구한다. 없으면 누구나 등록·삭제 요청을 보낼 수 있다(키 원문은 어느 쪽도 못 읽음) |
| `KIS_APP_KEY` / `KIS_APP_SECRET` / `KIS_ACCOUNT_NO` | — | 한국투자증권 어댑터가 요구하는 키(자리) |
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` / `TOSS_ACCOUNT_NO` | — | 토스증권 어댑터가 요구하는 키(자리) |
| `MIRAE_API_KEY` / `MIRAE_ACCOUNT_NO` | — | 미래에셋증권 어댑터가 요구하는 키(자리) |

증권사 키 이름은 **자리를 잡아 둔 값**이다. 실제 발급 시 증권사 문서의 이름에 맞춰
`broker.py`의 해당 클래스 `api_key_envs`만 바꾸면 된다(문서·`/` 요약이 자동으로 따라온다).

실주문이 나가려면 **세 조건이 모두** 필요하다:
`BROKER_MODE=kis|toss|mirae` + `REAL_TRADING_ENABLED=1` + `REAL_DRY_RUN=0` + (증권사 키 전부 설정).

---

## 3. 안전장치 `OrderGuard`

`broker.get_adapter()`가 돌려주는 어댑터는 항상 `GuardedBroker`다. 즉 **어떤 경로로 주문해도**
가드를 지난다. 검사 순서(먼저 걸리는 것이 답이 된다):

| 순서 | 검사 | 차단 코드 | 대상 |
| --- | --- | --- | --- |
| 1 | 주문 내용(종목·가격·금액·수량) | `invalid_order` | 모든 주문 |
| 2 | 증권사 키·구현 여부 | `broker_not_configured` | 실계좌 |
| 3 | 킬 스위치 `REAL_TRADING_ENABLED` | `real_trading_disabled` | 실계좌 |
| 4 | 1회 매수 상한 | `order_limit_exceeded` | 실계좌 **매수** |
| 5 | 1일 누적 매수 상한 | `daily_limit_exceeded` | 실계좌 **매수** |
| 6 | 중복 주문(같은 `run_id`·시장·방향·종목) | `duplicate_order` | 모든 주문 |

- **한도는 매수에만 건다.** 매도까지 막으면 손절 출구를 막는 셈이라 더 위험하다.
  1일 누적도 "새로 쓰는 돈"(매수)만 센다.
- **중복 주문 키**는 `run_id|시장:방향:종목`이다. `run_id`가 비면 날짜를 써서 그날 한 번만 나간다.
  어댑터는 같은 주문을 재시도할 때 반드시 `order.client_order_id()`를 증권사 API의 멱등 키로
  넘겨야 한다(네트워크 타임아웃으로 결과가 모호할 때 자동 재시도는 금지 — 중복 체결 위험).
- **영속**: 1일 누적과 중복 키는 `BROKER_GUARD_STATE` JSON 파일에 남는다. 서버가 재기동돼도
  하루 한도를 새로 채울 수 없다. 파일을 못 쓰면 경고만 남기고 메모리로 계속한다(매매를 막지 않음).
  다만 Render 무료 인스턴스의 디스크는 **재배포하면 초기화**된다(오늘 배포 전 1일 누적이 사라짐).
  실계좌로 전환할 때는 이 원장을 PostgreSQL 테이블(`DATABASE_URL`)로 옮기는 것을 권장한다 —
  옮길 자리는 `broker.OrderLedger`의 `_load`/`_save` 두 메서드뿐이다.
- **드라이런**: 주문을 보내지 않고 `status=dry_run`을 돌려준다. 한도·중복에 걸릴 주문이면
  `guard.blocked_by`에 사유 코드가 담긴다. 드라이런은 1일 누적을 먹지 않는다.

---

## 4. 새 증권사 붙이기 (5단계)

### 1) 어댑터 클래스 작성 — `broker.py`

```python
class KisBroker(BrokerAdapter):          # 이미 자리(스텁)가 있다 — 그 클래스를 고친다
    name = "kis"
    label = MODE_LABELS["kis"]
    is_live = True
    implemented = True                   # ← 구현했다고 표시
    api_key_envs = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO")

    def _client(self):
        # 토큰 발급·캐시는 프로세스 하나에 한 번(만료 24시간). 스레드 락으로 감싼다.
        ...

    def place_buy(self, order: OrderRequest) -> OrderResult:
        body = self._client().order(
            account=os.environ["KIS_ACCOUNT_NO"],
            side="BUY",
            ticker=order.ticker,
            qty=order.shares,                # 매수도 수량을 정해서 보낸다
            price=order.price,
            client_order_id=order.client_order_id(),   # ← 멱등 키(중복 주문 방지)
        )
        return OrderResult(ok=True, status=STATUS_FILLED, adapter=self.name, live=True,
                           order_id=str(body.get("ODNO")), raw=body)

    def place_sell(self, order: OrderRequest) -> OrderResult:
        ...  # place_buy와 같은 모양, side만 SELL

    def get_balance(self, db_path=None, account_id=None) -> BrokerAccountInfo:
        ...  # 예수금·보유 종목 → BrokerAccountInfo(cash_krw=..., positions=[...])
```

지켜야 할 것:
- **예외를 삼키지 않는다.** 실패는 `OrderResult(ok=False, status=STATUS_REJECTED, reason=...)`로
  돌려주고, 애매한 실패(타임아웃)는 `ok=False`로 두어 가드가 중복 키를 남기지 않게 한다.
- 금액은 **원화**로 돌려준다(`BrokerAccountInfo.cash_krw`). 현지 통화면 `fx_rate`로 환산.
- 키 본문은 로그·응답에 남기지 않는다(`missing_env`처럼 이름만).

### 2) 등록

`broker.py` 아래쪽 `ADAPTERS`에 이미 들어 있다:
`{"paper": PaperBroker, "kis": KisBroker, "toss": TossBroker, "mirae": MiraeBroker}`.
새 이름을 추가하면 `BROKER_MODE`와 `GET /` 요약이 자동으로 따라온다.

### 3) 실행기 연결 — `paper_trading._run_locked`

실계좌 모드에서는 지금 **일부러 실행을 거부**한다(가짜 시드머니로 실주문을 내지 않게):

```python
adapter = broker.get_adapter(market=scope, db_path=db_path, account_id=account_id)
if adapter.is_live:
    return {"ok": True, "skipped": "live_broker_not_wired", ...}
```

실계좌를 붙일 때 이 블록을 지우고 **아래 세 곳**을 어댑터 기준으로 바꾼다.

| 지금 | 실계좌에서 |
| --- | --- |
| `paper_store.list_positions(db_path, account_id)` (보유·매도 판단) | `adapter.get_balance().positions` |
| `paper_store.get_account(...)["cash_krw"]` (매수 여력) | `adapter.get_balance().cash_krw` |
| `paper_store.record_equity(...)` (일별 총자산) | 그대로 두거나(모의 그림자 원장) 별도 원장으로 |

매수 예산(`amount_krw`)은 `paper_trading`이 정하고, **수량 확정은 어댑터**가 한다
(모의투자는 현금·수수료를 보고 계산, 실계좌는 최소 주문 단위·호가 단위에 맞춰 계산).

> 실계좌에서는 `paper_store`가 "가짜 계좌"라는 점을 잊지 말 것. 실주문의 현금·보유를
> `paper_store`에서 읽으면 잔고와 어긋나 과매수/과매도가 난다.

### 4) 수수료·세금

모의투자는 `paper_trading.FEE_POLICY`(한국 매도 0.195% = 위탁 0.015% + 거래세 0.18%,
미국 0.15%)를 쓴다. 실계좌에서는 증권사가 실제로 떼므로 **어댑터가 돌려준 체결 금액·수수료를
그대로 기록**하는 쪽이 맞다(체결 응답의 `fee_krw`, `settle_krw`). `FEE_POLICY`는 모의투자
전용으로 남겨 둔다.

### 5) 테스트

`tests/test_broker_guard.py`의 `FakeLiveBroker`를 본떠 **네트워크 없이** 검증한다.

```python
broker.ADAPTERS["fake"] = FakeLiveBroker      # 테스트에서만 등록
with patch.dict(os.environ, {"BROKER_MODE": "fake", "REAL_TRADING_ENABLED": "0"}):
    result = broker.get_adapter(market="US").place_buy(order)
assert result.status == "blocked" and result.reason == "real_trading_disabled"
```

---

## 5. 실계좌 전환 체크리스트

1. **드라이런** — `BROKER_MODE=kis`, `REAL_TRADING_ENABLED=1`, `REAL_DRY_RUN=1`(기본),
   `REAL_ORDER_MAX_KRW=10000`. `GET /`의 `broker.adapters[].configured`가 `true`인지 확인.
   `POST /api/paper/run` 대신 어댑터를 직접 불러 `status=dry_run`과 `guard.blocked_by`를 본다.
2. **한도 확인** — 1회 1만원·1일 3만원처럼 작게 잡고, 한도를 넘는 주문이 `order_limit_exceeded`로
   막히는지 확인한다.
3. **첫 실주문** — `REAL_DRY_RUN=0`으로 바꾸고 1회만. 체결 내역(주문번호·수량·금액)을
   증권사 앱과 대조한다.
4. **중복 방지 확인** — 같은 `run_id`로 두 번 호출해 두 번째가 `duplicate_order`인지 확인한다.
5. **한도 상향** — 문제가 없으면 `REAL_ORDER_MAX_KRW`/`REAL_DAILY_MAX_KRW`를 올린다.
6. **감시** — 체결 즉시 FCM 푸시는 `paper_store` 기준이므로, 실계좌 전환 시 증권사 체결 통보
   (웹소켓/알림)로 바꾸는 것을 권장한다.

---

## 6. 화면에서 키 등록하기 (앱·웹, v2.10)

환경변수를 직접 만지지 않고 **앱·웹의 `🔌 실제 계좌 (테스트)` 카드 → `실제 계좌로 해보기`**에서
증권사 키를 등록할 수 있다. 키는 서버에서만 살고, 화면에는 끝 4자리만 돌아온다.

| 메서드 | 경로 | 하는 일 |
| --- | --- | --- |
| GET | `/api/broker/status` | 모드·킬 스위치·한도·어댑터별 키 등록 여부(마스킹 힌트)·증권사별 등록 안내 |
| POST | `/api/broker/credentials` | `{adapter, app_key, app_secret, account_no, confirm_risk:true}` → 암호화 저장 |
| DELETE | `/api/broker/credentials/{adapter}` | 등록 삭제(연동 해제) |

- **저장 방식**(`broker_credentials.py`): `cryptography.fernet`(AES-128-CBC + HMAC-SHA256) 암호문만
  `broker_credentials` 테이블(`cipher` 컬럼)에 넣는다. 화면·로그·에러 메시지 어디에도 원문이 없고,
  상태 응답은 `hint_label`(`App Key ••••3f9a · App Secret •••• · 계좌번호 ••••-01`)만 준다.
  상태 API는 인증 없이 읽히므로 **App Secret은 끝자리도 남기지 않는다**(통째로 `••••`).
- **암호화 재료**: `BROKER_CRED_KEY`(권장) → 없으면 `DATABASE_URL` → `FIREBASE_SERVICE_ACCOUNT_JSON`
  → `GOOGLE_API_KEY`/`GEMINI_API_KEY` → `FINN_API_KEY`에서 PBKDF2-HMAC-SHA256(20만 회)로 유도.
  재료가 바뀌면 기존 암호문은 못 읽는다(상태에 "다시 등록" 안내, 원문은 절대 안 나옴).
- **등록 잠금**: `BROKER_CRED_TOKEN`을 넣으면 등록·삭제에 `X-Broker-Token` 헤더가 필요하다.
  비워 두면 `credentials.auth.mode="open"`이며, 상태 응답이 그 사실을 경고로 알려 준다.
- **미래에셋은 등록 불가**: 개인용 Open API가 아직 없다(2026-10 개발 착수 보도, 시기 미정).
  화면은 안내만 보여 주고 `broker_not_registerable`로 거절한다.
- **등록해도 주문은 안 나간다**: 어댑터 `implemented=false`(주문 코드 미연결) + 킬 스위치 기본
  꺼짐 + 드라이런 기본 켜짐. 세 겹을 모두 통과해야 실주문이 된다.

## 7. 지금 상태 요약

| 모드 | 실주문 | 상태 |
| --- | --- | --- |
| `paper` (기본) | ✗ | 완전 동작. 기존 AI 모의투자와 100% 동일 |
| `kis` / `toss` / `mirae` | ✗ | 자리만 있음. 키가 없거나 구현 전이면 `broker_not_configured` |
- 실계좌 모드에서 `paper_trading.run()`은 `skipped: live_broker_not_wired`로 **아무 주문도 만들지
  않고 오늘 몫도 소모하지 않는다.** (모의투자를 계속 돌리려면 `BROKER_MODE=paper`)
- `GET /`의 `broker` 블록은 DB·시세를 건드리지 않는다(헬스 체크가 10분마다 부른다).

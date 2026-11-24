# 📈 한국·미국 주식 차트 · 재무 AI 스크리너

yfinance 실시간 주가·재무 데이터와 Gemini AI를 결합한 종목 스크리닝 웹앱입니다.
한국 종목(`.KS`/`.KQ`)은 네이버 금융으로 이름·재무를 보완하고 가격을 원화(₩)로 표시합니다.

## 주요 기능
- S&P 500(500개) · 코스피 200(약 200개) 목록 및 야후 파이낸스 실시간 종목 검색
- **한국 주식 지원**(`naver_finance.py`): `.KS`/`.KQ` 티커에 대해 ① 한국어 이름·6자리 코드를
  야후 티커로 해석(`GET /api/resolve-ticker`) ② Yahoo `t.info`가 비거나 막혀도 PER·추정PER·
  영업이익률·부채비율·매출증가율·PBR을 네이버 금융에서 보완 ③ 한국어 뉴스(구글 뉴스 '{종목명} 주가'를
  먼저, 모자라면 네이버 종목 뉴스로 보충) ④ 응답마다 `currency`(`KRW`/`USD`)와 `name`(예: `삼성전자`)을 담는다.
  - `GET /api/resolve-ticker?q=삼성전자` → `{"ticker":"005930.KS","name":"삼성전자",
    "exchange":"KOSPI","currency":"KRW",...}`. `에코프로` → `086520.KQ`, `000660` → `000660.KS`,
    `NAVER` → `035420.KS`, `AAPL`/`NVDA`는 그대로 통과한다. 네이버가 막히면 내장 코스피 200
    목록으로 주요 이름을 오프라인 해석한다. 찾지 못하면 404 `not_found`.
  - 네이버 재무는 **국내 공시 단위**를 따른다: 영업이익률은 %를 소수(0.1307)로, 부채비율은
    % 그대로, 매출증가율은 **확정 연도끼리**(추정 컨센서스 제외) 계산한다. 한국 종목은 이 값을
    Yahoo보다 우선한다(Yahoo의 한국 재무는 기간이 어긋나 영업이익률이 46%처럼 튀는 경우가 있다).
    `financials_source`가 `naver`면 그 경로로 채운 행이다(한국 종목은 Yahoo `t.info`를 부르지 않는다).
  - 네이버 호출은 캐시한다(해석 24시간 · 재무 6시간 · 구성종목 6시간 · 실패 15분).
  - 기존 키는 그대로라 미국 종목·기존 클라이언트는 수정 없이 동작한다.
- **코스피 200 자동 발굴 모드**(`kospi200_scanner.py`): `POST /api/screen`에
  `"mode": "kospi200"`을 보내면 코스피 200 약 200종목을 S&P 500과 **같은 1차 스캔**(6개월 일봉,
  100종목 청크)으로 훑어 조건에 맞는 상위 15개를 뽑고, 그 15개에만 재무·뉴스·Gemini를 적용한다.
  구성종목은 네이버 `index/KPI200/enrollStocks`(6시간 캐시) → 직전 성공값 → **내장 201종목
  폴백** 순으로 항상 채워진다. 스냅샷은 70분 캐시, 응답은 `mode`·`universe_scanned`를 포함한다.
- 20일·60일 이동평균선, RSI(14), 재무지표(PER·매출성장률·영업이익률·부채비율) 기반 AI 추천
- **고급 기술적 지표**(6개월 일봉으로만 계산, 추가 API 호출 없음): MACD(12,26,9)·볼린저밴드(20,2σ)·
  거래량 배수(20일 평균 대비)·6개월 고점 대비 낙폭·구간 위치. `GET /api/screen`의 종목마다
  `macd`·`macd_signal`·`macd_hist`·`macd_cross`·`macd_state`·`bb_upper`·`bb_lower`·`bb_pct_b`·
  `volume_ratio`·`drawdown_from_high_pct`·`range_position_pct`로 실린다. 데이터가 짧거나 비면 `null`/`"none"`.
- **정량 스코어링**(`scoring.py`): 검색 조건에서 전략 의도(`value`/`oversold`/`momentum`/`balanced`)를
  읽어 종목마다 0~100 `quant_score`와 근거 `signals`(예: `["저PER(14.8)", "볼린저하단", "거래량1.9x"]`)를
  계산하고, 점수 내림차순으로 정렬한 뒤 Gemini에 넘긴다(결정적·재현 가능, 네트워크 호출 없음).
  `POST /api/screen` 응답 최상위에 사용한 `strategy`가 추가되고, `all_stocks`·`top_picks` 각 항목에
  `quant_score`·`signals`가 붙는다(기존 키는 그대로라 웹/안드로이드 클라이언트는 수정 없이 동작).
- **S&P 500 자동 발굴 모드**(`sp500_scanner.py`): `POST /api/screen`에 `"mode": "sp500"`을 보내면
  관심 종목 대신 **S&P 500 약 500종목을 기술적으로 먼저 훑어** 조건(전략)에 맞는 상위 15개를 자동
  발굴한다. 6개월 일봉을 100종목씩 `yf.download`로 배치 수신해 이평선·RSI·MACD·볼린저·거래량·고점
  대비 낙폭만으로 점수를 내고(프리 스캔에서는 `t.info`·SEC·Finnhub·뉴스를 부르지 않는다), 선정된
  15개에만 기존 3단 재무 파이프라인 + 뉴스 + Gemini를 적용한다. 스캔 스냅샷은 메모리에 70분
  캐시(스레드 안전)되어 반복 조회는 즉시 응답한다. 응답은 기존과 같은 `analysis`·`all_stocks`·
  `strategy`에 `mode`(`watchlist`|"sp500")·`universe_scanned`(스캔한 종목 수)가 더해진다.
  기본값은 `"watchlist"`라 기존 클라이언트는 그대로 동작한다.
  - **백그라운드 예열(50분 주기)**: 서버가 뜨면 8초 뒤 첫 스캔을 백그라운드 데몬 스레드로 돌리고
    이후 50분마다 갱신한다(TTL 70분보다 짧아 캐시가 만료된 채 사용자 요청을 맞지 않는다).
    사용자는 보통 2분이 아니라 몇 초 만에 응답을 받는다. 예열은 사용자 스캔과 같은 `_SCAN_LOCK`을
    써서 겹쳐도 500종목을 두 번 받지 않고, 실패하면 경고만 남기고 직전 스냅샷을 그대로 서빙한다.
    `SP500_PREWARM=0`으로 끈다(단위 테스트·로컬 실행). 상태는 `GET /`의 `sp500_cache`
    (`warm`·`fresh`·`tickers`·`age_sec`·`ttl_sec`·`prewarm_enabled`·`prewarm_running`·
    `prewarm_last_ok_sec`)로 확인한다 — 스캔 중에도 즉시 응답한다.
  종목 목록은 Wikipedia(8초 타임아웃) → 실패 시 내장 503종목 폴백으로 항상 채워진다.
- **코스피 200 백그라운드 예열(50분 주기)**: S&P 500과 나란히, 기동 60초 뒤 첫 스캔을 돌리고
  이후 50분마다 갱신한다(S&P 500은 8초 뒤 시작 — 첫 스캔이 겹치지 않게 시차를 둔다). S&P 500
  스캔이 도는 중이면 최대 180초 기다렸다가 시작해 Render 512MB에서 메모리 피크가 겹치지
  않는다. `KOSPI200_PREWARM=0`으로 이 시장만 끄고, `PREWARM_ENABLED=0`은 예열 전체를,
  `SP500_PREWARM=0`(기존 테스트·로컬 스위치)은 한국 예열까지 함께 끈다.
  상태는 `GET /`의 `kospi200_cache`(`warm`·`fresh`·`tickers`·`age_sec`·`ttl_sec`·
  `universe_source`·`prewarm_enabled`·`prewarm_running`·`prewarm_last_ok_sec`)로 확인한다.
  구성종목 목록은 `GET /api/kospi200`(`{"results","source","count"}`)으로도 볼 수 있다.
- Recharts 인터랙티브 차트 및 최근 뉴스 요약
- **웹 UI**: 모드 토글 📋 내 관심 종목 / 🌐 S&P 500 자동 발굴 / 🇰🇷 코스피 200 발굴.
  종목 검색·선택 모달에도 같은 시장 토글(**🌐 S&P 500** / **🇰🇷 코스피 200**)이 있어
  `/api/sp500`·`/api/kospi200` 목록을 바로 넘겨보며 클릭으로 담는다(빠른 선택: 인기·반도체·빅테크·한국).
  관심 종목 입력창에 `삼성전자`·`카카오`·`005930`처럼 이름·코드로 바로 추가할 수 있고,
  가격은 통화에 따라 `₩65,400` / `$352.45`로 표시되며 한국 종목에는 한국어 이름 배지가 붙는다
  (TOP 카드·상세 모달·비교표·성적표 모두 동일).
- **AI 추천 성적표**(`history_store.py`): 사용자가 **직접 고른 픽만** 추적해 현재가와 비교하는
  적중률·평균 수익률 표다. `/api/screen`은 이제 이력을 남기지 않는다(분석만 돌려도 성적표가
  오염되던 자동 기록 제거).
  - **`POST /api/performance/track`** — 픽 하나를 추적에 추가한다. 본문
    `{"ticker": "NVDA", "entry_price": 150.96, "strategy": "oversold", "mode": "watchlist",
    "quant_score": 92.0, "criteria": "..."}`. `entry_price`는 선택이며, 없거나 0 이하면 서버가
    **실시간 현재가(네이버·Finnhub·Yahoo 시간외)를 먼저** 받아 채우고, 실시간가를 못 받을 때만 최근 일봉 종가로
    폴백한다(둘 다 실패하면 400 `invalid_ticker`). 응답은
    `{"ok": true, "created": bool, "pick": {id, ticker, ...}, "entry_price_source": str|null}`
    (같은 날 같은 티커·전략은 기존 행 반환). `entry_price_source`는 서버가 진입가를 조회했을 때의
    출처(`naver_realtime`/`finnhub_quote`/`naver_prepost`/`yahoo_prepost`/`daily_close`)이고, 클라이언트가 값을 보냈거나 기존 행을
    돌려준 경우 `null`이다(기존 클라이언트는 무시해도 된다).
  - **`DELETE /api/performance/{id}`** — 그 `id` 행 **하나만** 삭제한다.
    응답 `{"ok": true, "deleted_id": id}`, 없으면 404. 저장소 장애는 503.
  - `GET /api/performance`의 `picks` 각 항목은 `id`(정수 PK)를 포함한다 — 웹 성적표의 행별 🗑️가 이 값으로 지운다.
  - 저장: `ticker`·`recommended_date`(KST YYYY-MM-DD)·`recommended_at`(ISO)·`entry_price`·`strategy`·
    `mode`·`quant_score`·`criteria`. 진입가는 Gemini가 적은 값이 아니라 서버가 계산한 현재가
    (**실시간 시세 우선**, 없으면 최근 일봉 종가)를 쓴다.
  - 같은 날 같은 `(date, ticker, strategy)`는 한 행만 남긴다(첫 진입가·첫 시각 유지, 점수·mode·criteria만 갱신).
  - DB 경로: `HISTORY_DB_PATH` 환경변수 > `data/recommendations.db`(부모 폴더 자동 생성) >
    만들 수 없으면 `/tmp/stock_scanner_history.db`. DB 쓰기가 실패해도 `/api/screen`은 정상 응답한다
    (분석과 저장이 분리됐다 — 저장 실패는 `/api/performance/track`의 503으로만 드러난다).
  - **외부 PostgreSQL(선택)**: `DATABASE_URL`(Neon·Supabase·Render Postgres, `postgres://`/`postgresql://`)이
    설정돼 있으면 SQLite 대신 PostgreSQL에 기록한다. 테이블(`recommendations`)과 인덱스는 첫 사용 시
    자동 생성되며, `(recommended_date, ticker, strategy)` 충돌 시 `ON CONFLICT ... DO UPDATE`로
    점수·mode·criteria만 갱신해 최초 진입가·추천 시각을 유지한다. `postgres://`는 `postgresql://`로
    바꾸고, sslmode가 없고 원격 호스트면 `sslmode=require`를 붙인다(로컬 주소는 그대로).
    `DATABASE_URL`이 없으면 기존 SQLite 그대로다. 저장소가 어디인지는 `GET /`와
    `GET /api/performance`의 `storage_backend`(`postgres`|`sqlite`)로 확인한다(자격 증명은 노출하지 않음).
    ⚠️ Render 무료 플랜은 영구 디스크가 없어 재배포 시 SQLite 이력이 초기화되므로,
    오래 쌓으려면 무료 PostgreSQL(Neon 등)을 붙여 `DATABASE_URL`만 넣으면 된다.
  - 현재가·진입가는 **실시간 시세(네이버·Finnhub·Yahoo 시간외)를 먼저** 쓰고, 실시간가를 못 받은 종목만 일봉 종가
    (`yf.download`, 최대 50종목 한 번·5분 캐시)로 채운다 — 장중 새로고침이 Yahoo 왕복을 기다리지
    않는다. 두 경로는 `history_store.resolve_current_prices` 하나로 모아 성적표 현재가와 추적
    진입가가 같은 우선순위를 쓴다(장중에 전일 종가로 진입가가 잡혀 수익률이 어긋나던 문제).
    픽마다 `price_source`가 `naver_realtime`/`finnhub_quote`/`naver_prepost`/`yahoo_prepost`/`naver_quote`/`yahoo_quote`/`daily_close`다.
  - 응답: `{"summary": {total_picks, evaluated_picks, win_count, win_rate_pct, avg_return_pct,
    best_pick, worst_pick}, "picks": [...], "as_of", "limit"}`. 이력이 비면 개수 0·비율 `null`.
    픽별로 `entry_price`·`current_price`·`return_pct`·`is_win`(return > 0)·`days_held`가 붙는다.
  - 웹 UI 상단에 **📊 AI 추천 성적표** 패널(요약 3개 지표 + 추천 이력 표 + 행별 삭제 + 새로고침)이 있다.
    추천 카드(TOP 1~3)와 상세 모달에는 **📌 추적 추가 / ✅ 추적 중** 버튼이 있고, 같은 카드에서
    **+ 관심 종목 / ✓ 관심 중**으로 발굴된 종목을 관심 종목 풀(localStorage)에 바로 담을 수 있다.
    `전체 N개 종목 원본 지표 비교표`의 각 행에도 `+ 관심` 버튼이 있다.
  - ⚠️ Render 무료 플랜은 영구 디스크가 없어 재배포·재시작 시 이력 파일이 초기화된다.
    위 `DATABASE_URL`(Neon 등 무료 PostgreSQL)을 넣으면 재배포에도 이력이 남는다.
    굳이 파일로 유지하려면 Render Persistent Disk를 붙이고 `HISTORY_DB_PATH=/var/data/recommendations.db`처럼 지정한다.
- **🤖 AI 모의투자(페이퍼 트레이딩)**(`paper_store.py` · `paper_trading.py`): 스크리너 결과로 AI가
  **원화 통합 계좌**에서 한국·미국 주식을 가상으로 사고팔아 수익률을 추적한다. 실제 주문은 나가지 않는다.
  - 계좌: 시드머니 **기본 10,000,000원**(10만~10억, 언제든 설정/리셋). 현금·총자산·수익률은 모두 `₩`,
    미국 종목은 달러로 체결하고 **체결 시점의 USD/KRW 환율**을 포지션에 저장해 원화 원가를 고정한다.
    평가는 현재 환율로 다시 계산하므로 **환차손익도 수익률에 들어간다**. 환율은 네이버 금융(하나은행 고시)
    → Yahoo `KRW=X` → 고정 1,350원 순으로 폴백하고, 응답의 `fx_source`에 어느 경로였는지 남긴다.
  - 수량: **미국은 소수점 4자리**(토스증권 소수점 거래 방식), **한국은 1주 정수**.
  - 투자 시장: `ALL`(한국+미국, 기본) / `US` / `KR`. 전략: `balanced`(기본)·`momentum`·`value`·`oversold`.
  - 매도: 손절 **-8%** · 익절 **+25%** 규칙과 Gemini 판단(전량/부분 `fraction`)으로 현금을 만든다.
  - 매수: 대상 시장의 스크리너 상위 후보(S&P 500 · 코스피 200, 정량 점수·`signals`·재무·뉴스)를 Gemini에
    넘겨 종목·원화 금액을 받는다. **남은 현금을 넘겨 살 수 없고**(요청 금액을 현금으로 자르고 체결 후
    잔고를 다시 검증), 한 종목은 총자산의 **30%**, 최대 **8종목**, 최소 주문 **1만 원**이다.
    Gemini가 죽거나 429·해석 실패면 **정량 점수 상위 3종목 균등 배분 폴백**으로 매매를 계속한다.
  - 자동 실행: 서버 백그라운드 루프가 **한국 장 마감 후 15:40 KST**·**미국 장 마감 후 06:20 KST**에
    시장별 **하루 1회**(`last_run_kr_date`·`last_run_us_date`) 실행한다. 서버가 그 시각에 꺼져 있었다면
    다시 켜진 뒤 1회 따라잡고, `PAPER_AUTO=0`으로 끌 수 있으며, `force` 없이 같은 날 다시 부르면
    `skipped: "already_ran_today"`로 건너뛴다.
  - API: `GET /api/paper/status`(계좌 요약·보유 포지션 평가손익·체결 내역·일별 수익률 추이·자동 실행 일정),
    `POST /api/paper/configure`(`initial_cash_krw`·`markets`·`strategy`·`auto_enabled`·`reset`),
    `POST /api/paper/run`(`force`·`markets`, 실행 결과 + 갱신된 status). 실행은 한 번에 하나만
    돌고(`_RUN_LOCK`), 자동 실행과 수동 실행이 겹치면 두 번째는 `skipped: "already_running"`을 받는다.
    현금은 `cash_krw + ?`로 원자 증감해 잔고보다 많이 차감하려 하면 체결 전체가 취소된다
    (포지션·체결이 반쪽만 남지 않는다).
  - 저장: 기존 `recommendations`(성적표) 테이블은 그대로 두고 `paper_account`·`paper_positions`·
    `paper_trades`·`paper_equity_history` 네 테이블을 새로 만든다(`DATABASE_URL`이 있으면 PostgreSQL,
    없으면 같은 SQLite 파일).
  - 웹 UI 상단에 **🤖 AI 모의투자** 탭: 총 자산·총 수익률·시드머니 카드, 시드머니/시장/전략/자동 실행 설정과
    계좌 초기화, `▶ 지금 AI 매매 1회 실행`, 보유 종목 표(수량·평단가·현재가·평가손익·AI 사유),
    최근 매매 내역, 수익률 추이 차트(Recharts).
- Yahoo Finance `t.info`가 서버 IP에서 막히면(429/401) **SEC EDGAR 공시(XBRL)** 에서
  PER·매출성장률·영업이익률·부채비율을 계산해 채운다(API 키 불필요).
- SEC 공시에도 값이 없으면(예: us-gaap 태그가 없는 외국 ADR `TSM`) **Finnhub**
  `stock/metric`에서 마지막으로 보완한다(`FINNHUB_API_KEY`, `FINN_API_KEY` 중 하나 필요).
  우선순위는 **Yahoo → SEC EDGAR → Finnhub**이며, 앞 단계가 이미 채운 지표는
  다시 조회하지 않는다. Finnhub 응답은 6시간 캐시(실패 15분)로 무료 쿼터를 아낀다.
  응답의 `financials_source`가 `sec_edgar`, `finnhub`, `sec_edgar+finnhub` 등이면
  그 경로로 보완된 행이다.
  `SEC_EDGAR_FALLBACK=0`, `FINNHUB_FALLBACK=0` 환경변수로 각각 끌 수 있다.
- **응답 지연 방어(2026-11 최적화)**: `/api/screen`이 30~108초 걸리던 원인은 ① Render 같은
  데이터센터 IP에서 Yahoo `t.info`가 429/401로 막히는데도 종목마다 30초씩 기다린 것, ② 이미
  받아 둔 6개월 일봉을 15종목이 다시 받은 것이었다. 지금은 이렇게 막는다.
  - **한국 종목은 Yahoo `t.info`를 아예 부르지 않는다**(`.KS`/`.KQ` → 네이버 금융 직행).
  - **전역 회로 차단기**: `t.info`가 429/401/crumb/타임아웃으로 **2회 연속** 실패하면 종목과
    무관하게 **30분 동안 전역으로** `t.info`를 건너뛰고 곧바로 SEC EDGAR → Finnhub로 간다
    (빈 응답은 그 종목 문제일 수 있어 3회까지 본다). 뉴스도 같은 규칙으로 우회한다.
  - **벽시계 타임아웃**: `t.info` 6초 · 뉴스 5초 · 6개월 일봉 8초. yfinance는 요청마다만
    타임아웃이 있어 한 호출이 수십 초를 잡아먹을 수 있었다.
  - **프리 스캔 일봉 재사용**: `yf.download`로 500/201종목을 받을 때 그 일봉을 히스토리 캐시에
    넣어 두고(`prime_history`, 스냅샷 수명 70분), 발굴된 15종목의 지표·차트가 **재다운로드 0회**로
    끝난다. 종목 병렬도는 6 → 8(`MAX_FETCH_WORKERS`).
  - **Gemini**: `gemini-3.5-flash-lite`(빠른 lite 모델) 그대로, 시도당 25초 상한
    (`GEMINI_TIMEOUT_MS`, 기본 30000ms) + 최대 3회(503 과부하만 짧게 재시도). 사고(thinking)는 꺼 둔다(`GEMINI_THINKING_BUDGET=0`) —
    켜져 있으면 15종목 배치가 25초를 넘겨 504로 폴백되던 것을 확인했다. 타임아웃은 재시도하지
    않고 바로 정량 점수 폴백으로 간다(대기가 두 배가 되지 않게).
  - 응답의 `analysis_model`에 실제로 해설을 만든 모델이 담긴다(폴백이면 null).
  - 응답의 `timings`(ms)에 단계별 소요 시간이 담긴다 — `discovery_ms`(발굴·프리 스캔) ·
    `rows_ms`(지표+실시간 병렬) · `realtime_ms`(실시간 시세 구간) · `overlay_ms`(가격 적용) ·
    `news_ms`(상위 뉴스) · `gemini_ms` · `charts_ms` · `total_ms`.
  - **실시간 현재가**(`realtime_quotes.py`): 지표·재무는 30분 캐시 그대로 두고 `price`만 덮어쓴다.
    한국 종목은 네이버 금융 `polling.finance.naver.com`(여러 코드를 한 요청으로), 미국 종목은
    **정규장이면 Finnhub `/quote`**(병렬 8·분당 50회 자체 상한)를 쓰고, **프리·애프터마켓·주말에는
    네이버 해외주식 `overMarketPriceInfo.overPrice` → Yahoo v8 chart 메타의 시간외 마지막 체결가**
    (`extended_quotes.py`)를 우선한다 — Finnhub는 정규장 종가만 주므로 한국 낮에는 토스증권 같은
    시간외 시세와 어긋났다(예: SKHY 103.57 vs 106.30, MNST 43.69 vs 43.27). 두 소스는 동시에
    조회하고, 각 소스 장애는 그 소스만 비우며 소스별로 3회 연속 실패 시 10분 쉰다.
    최종 15종목·성적표·상세 화면에서만 호출하며 프리 스캔(500/201종목)에는 쓰지 않는다.
    캐시는 10~30초 — 같은 요청의 중복 호출만 막고 새 조회에는 최신 체결가가 반영된다.
    실패는 조용히 일봉 종가로 폴백하고 응답에 `price_source`
    (`naver_realtime` | `finnhub_quote` | `naver_prepost` | `yahoo_prepost` | `naver_quote` | `yahoo_quote` | `daily_close`)를 남긴다.
    `REALTIME_PRICE=0`(전체)·`EXTENDED_QUOTES=0`(시간외 계층만)으로 끌 수 있다.
    시간외 계층은 소스별로 3회 연속 실패하면 10분 쉬고, 헬스 `extended_quotes`에 그 상태가 나온다.
  - **AI 분석 10분 캐시**: 같은 `(mode, 전략, 조건, 종목 묶음)` 요청이 10분 안에 다시 오면
    Gemini를 부르지 않고 분석(`market_note`·`top_picks` 이유)을 재사용한다(`analysis_cached: true`,
    `timings.gemini_ms = 0`). 재사용해도 `price`·`price_source`는 **매번 새로 조회**해 덮어쓰므로
    화면의 현재가는 항상 최신이다. Gemini 실패 폴백은 60초만 캐시해 다음 호출에서 정상 분석을
    다시 시도한다(`ANALYSIS_CACHE_TTL`·`ANALYSIS_FALLBACK_TTL`).
  - **뉴스는 상위 5종목만**: 정량 점수 상위 `NEWS_TOP_N`(기본 5) 종목만 뉴스를 조회해 프롬프트에
    넣고 나머지는 `news: []`로 둔다. 프롬프트도 필요한 지표 필드만 담도록 간결화했다(출력 JSON
    스키마는 그대로).
  - `GET /`의 `yahoo_guard`에 `t.info`/뉴스 우회 상태(`blocked`·`remaining_sec`·`fail_streak`)와
    프리 적재된 일봉 캐시 개수가 나온다(병목이 다시 생기면 여기서 바로 보인다).
- `GET /` 헬스 응답에 배포 커밋(`build`)과 Finnhub 키 설정·마지막 오류(`finnhub`),
  저장소 종류(`storage_backend`), 실시간 시세 스위치(`realtime`), AI 분석 캐시 상태
  (`analysis_cache`: 개수·TTL)를 노출한다. 키 본문은 나오지 않고 설정 여부·이름·길이만 보인다.
  `errors_recent`에는 최근 처리되지 않은 예외 10건(`error_id`·`path`·`type`·`message`)이 남는다.
- **NaN 안전(2026-11 사고 수정)**: 야후가 한국 종목 일봉의 마지막 행을 OHLC 전부 NaN인
  자리표시자 행으로 내려줄 때가 있다. 그대로 쓰면 현재가·이평선·RSI가 NaN이 되고, JSON은
  NaN을 표현하지 못해 `/api/screen`이 평문 500이 됐다. 지금은 ① `get_history`가 종가 없는
  행을 걸러 내고 ② 응답 경계(`SafeJSONResponse`)가 NaN/inf를 `null`로 정화하며 ③ 처리되지
  않은 예외는 `{"detail":"internal_error","error_id":"..."}` JSON 500으로 돌려준다.
- **AI 폴백**: Gemini 호출·응답 파싱이 2회 모두 실패해도 500이 아니라 200으로,
  서버 정량 점수(`quant_score`·`signals`)만으로 만든 요약을 돌려준다. 이때 응답의
  `analysis_source`가 `fallback`, `analysis_note`에 사유가 담긴다(정상은 `gemini`·빈 문자열).

## 실행 방법
1. 백엔드: `pip install -r requirements.txt` 후 `GEMINI_API_KEY` 설정 → `python -m uvicorn api:app --reload --port 8000`
2. 프론트: `cd frontend && npm install && npm run dev`
3. 테스트: `python -m unittest discover -s tests` (지표·스코어링·캐시/폴백·추천 성적표·저장 백엔드·API 계약·한국 주식/코스피 200·NaN 안전/폴백·지연 가드·AI 모의투자 검증, 521개)

## 환경변수
| 이름 | 설명 |
| --- | --- |
| `GEMINI_API_KEY` | Gemini 호출 키(필수) |
| `DATABASE_URL` | 외부 PostgreSQL 접속 문자열(선택). 없으면 SQLite(`data/recommendations.db`) |
| `HISTORY_DB_PATH` | SQLite 파일 경로(선택, `DATABASE_URL`이 없을 때만) |
| `FINNHUB_API_KEY` / `FINN_API_KEY` | Finnhub 재무 보완(선택) |
| `SP500_PREWARM` | S&P 500 스냅샷 백그라운드 예열(기본 켜짐). `0`이면 끔(단위 테스트·로컬 실행). 켜져 있지 않으면 코스피 200 예열도 함께 꺼진다 |
| `KOSPI200_PREWARM` | 코스피 200 스냅샷 백그라운드 예열(기본 켜짐). `0`이면 이 시장만 끔 |
| `PREWARM_ENABLED` | 백그라운드 예열 전체 스위치(기본 켜짐). `0`이면 S&P 500·코스피 200 모두 예열하지 않음 |
| `GEMINI_TIMEOUT_MS` | 주 모델 호출 상한(기본 30000ms) |
| `GEMINI_FALLBACK_TIMEOUT_MS` | 2순위 모델 호출 상한(기본 15000ms) |
| `GEMINI_TOTAL_BUDGET_MS` | AI 분석 단계 전체 예산(기본 45000ms). 넘으면 정량 점수 폴백으로 응답 |
| `GEMINI_FALLBACK_MODEL` | 주 모델이 과부하·타임아웃일 때 2순위로 부를 모델(기본 `gemini-3.8-flash`) |
| `GEMINI_THINKING_BUDGET` | Gemini 사고 토큰 예산(기본 `0`=끔, 빠름). `-1`이면 모델 자동 |
| `PAPER_AUTO` | AI 모의투자 자동 실행(기본 켜짐). `0`이면 끔 |
| `PAPER_AUTO_INTERVAL` | 자동 실행 확인 주기(초, 기본 300) |
| `PAPER_STOP_LOSS_PCT` / `PAPER_TAKE_PROFIT_PCT` | 모의투자 손절·익절 기준(기본 -8 / +25) |
| `PAPER_MAX_POSITION_WEIGHT` / `PAPER_MAX_POSITIONS` | 한 종목 비중 상한(기본 0.30) · 최대 보유 종목 수(기본 8) |
| `PAPER_MIN_ORDER_KRW` | 모의투자 최소 주문 금액(기본 10000원) |
| `PAPER_CANDIDATES` | 시장별 매수 후보 수(기본 10종목) |
| `PAPER_USDKRW` | 환율 조회가 모두 실패할 때 쓰는 고정 USD/KRW(기본 1350) |
| `SEC_EDGAR_FALLBACK` / `FINNHUB_FALLBACK` | Yahoo `t.info` 대체 경로 스위치(기본 켜짐). `0`이면 끔 |

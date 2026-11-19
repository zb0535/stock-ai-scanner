# 📈 미국 주식 차트 · 재무 AI 스크리너

yfinance 실시간 주가·재무 데이터와 Gemini AI를 결합한 종목 스크리닝 웹앱입니다.

## 주요 기능
- S&P 500(500개) 목록 및 야후 파이낸스 실시간 종목 검색
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
  15개에만 기존 3단 재무 파이프라인 + 뉴스 + Gemini를 적용한다. 스캔 스냅샷은 메모리에 1시간
  캐시(스레드 안전)되어 반복 조회는 즉시 응답한다. 응답은 기존과 같은 `analysis`·`all_stocks`·
  `strategy`에 `mode`(`watchlist`|"sp500")·`universe_scanned`(스캔한 종목 수)가 더해진다.
  기본값은 `"watchlist"`라 기존 클라이언트는 그대로 동작한다. 첫 스캔은 수십 초, 이후 1시간은 캐시.
  종목 목록은 Wikipedia(8초 타임아웃) → 실패 시 내장 503종목 폴백으로 항상 채워진다.
- Recharts 인터랙티브 차트 및 최근 뉴스 요약
- **AI 추천 성적표**(`history_store.py`): 사용자가 **직접 고른 픽만** 추적해 현재가와 비교하는
  적중률·평균 수익률 표다. `/api/screen`은 이제 이력을 남기지 않는다(분석만 돌려도 성적표가
  오염되던 자동 기록 제거).
  - **`POST /api/performance/track`** — 픽 하나를 추적에 추가한다. 본문
    `{"ticker": "NVDA", "entry_price": 150.96, "strategy": "oversold", "mode": "watchlist",
    "quant_score": 92.0, "criteria": "..."}`. `entry_price`는 선택이며, 없거나 0 이하면 서버가
    최신 종가를 채운다(가격을 못 받으면 400 `invalid_ticker`). 응답은
    `{"ok": true, "created": bool, "pick": {id, ticker, ...}}`(같은 날 같은 티커·전략은 기존 행 반환).
  - **`DELETE /api/performance/{id}`** — 그 `id` 행 **하나만** 삭제한다.
    응답 `{"ok": true, "deleted_id": id}`, 없으면 404. 저장소 장애는 503.
  - `GET /api/performance`의 `picks` 각 항목은 `id`(정수 PK)를 포함한다 — 웹 성적표의 행별 🗑️가 이 값으로 지운다.
  - 저장: `ticker`·`recommended_date`(KST YYYY-MM-DD)·`recommended_at`(ISO)·`entry_price`·`strategy`·
    `mode`·`quant_score`·`criteria`. 진입가는 Gemini가 적은 값이 아니라 서버가 계산한 최신 종가를 쓴다.
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
  - 현재가는 `yf.download`로 **최대 50종목을 한 번에** 받아 5분 캐시한다(`t.info` 호출 없음).
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
- Yahoo Finance `t.info`가 서버 IP에서 막히면(429/401) **SEC EDGAR 공시(XBRL)** 에서
  PER·매출성장률·영업이익률·부채비율을 계산해 채운다(API 키 불필요).
- SEC 공시에도 값이 없으면(예: us-gaap 태그가 없는 외국 ADR `TSM`) **Finnhub**
  `stock/metric`에서 마지막으로 보완한다(`FINNHUB_API_KEY`, `FINN_API_KEY` 중 하나 필요).
  우선순위는 **Yahoo → SEC EDGAR → Finnhub**이며, 앞 단계가 이미 채운 지표는
  다시 조회하지 않는다. Finnhub 응답은 6시간 캐시(실패 15분)로 무료 쿼터를 아낀다.
  응답의 `financials_source`가 `sec_edgar`, `finnhub`, `sec_edgar+finnhub` 등이면
  그 경로로 보완된 행이다.
  `SEC_EDGAR_FALLBACK=0`, `FINNHUB_FALLBACK=0` 환경변수로 각각 끌 수 있다.
- `GET /` 헬스 응답에 배포 커밋(`build`)과 Finnhub 키 설정·마지막 오류(`finnhub`),
  저장소 종류(`storage_backend`)를 노출한다. 키 본문은 나오지 않고 설정 여부·이름·길이만 보인다.

## 실행 방법
1. 백엔드: `pip install -r requirements.txt` 후 `GEMINI_API_KEY` 설정 → `python -m uvicorn api:app --reload --port 8000`
2. 프론트: `cd frontend && npm install && npm run dev`
3. 테스트: `python -m unittest discover -s tests` (지표·스코어링·캐시/폴백·추천 성적표·저장 백엔드·API 계약 검증)

## 환경변수
| 이름 | 설명 |
| --- | --- |
| `GEMINI_API_KEY` | Gemini 호출 키(필수) |
| `DATABASE_URL` | 외부 PostgreSQL 접속 문자열(선택). 없으면 SQLite(`data/recommendations.db`) |
| `HISTORY_DB_PATH` | SQLite 파일 경로(선택, `DATABASE_URL`이 없을 때만) |
| `FINNHUB_API_KEY` / `FINN_API_KEY` | Finnhub 재무 보완(선택) |

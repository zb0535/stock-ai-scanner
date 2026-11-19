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
- Yahoo Finance `t.info`가 서버 IP에서 막히면(429/401) **SEC EDGAR 공시(XBRL)** 에서
  PER·매출성장률·영업이익률·부채비율을 계산해 채운다(API 키 불필요).
- SEC 공시에도 값이 없으면(예: us-gaap 태그가 없는 외국 ADR `TSM`) **Finnhub**
  `stock/metric`에서 마지막으로 보완한다(`FINNHUB_API_KEY`, `FINN_API_KEY` 중 하나 필요).
  우선순위는 **Yahoo → SEC EDGAR → Finnhub**이며, 앞 단계가 이미 채운 지표는
  다시 조회하지 않는다. Finnhub 응답은 6시간 캐시(실패 15분)로 무료 쿼터를 아낀다.
  응답의 `financials_source`가 `sec_edgar`, `finnhub`, `sec_edgar+finnhub` 등이면
  그 경로로 보완된 행이다.
  `SEC_EDGAR_FALLBACK=0`, `FINNHUB_FALLBACK=0` 환경변수로 각각 끌 수 있다.
- `GET /` 헬스 응답에 배포 커밋(`build`)과 Finnhub 키 설정·마지막 오류(`finnhub`)를
  노출한다. 키 본문은 나오지 않고 설정 여부·이름·길이만 보인다.

## 실행 방법
1. 백엔드: `pip install -r requirements.txt` 후 `GEMINI_API_KEY` 설정 → `python -m uvicorn api:app --reload --port 8000`
2. 프론트: `cd frontend && npm install && npm run dev`
3. 테스트: `python -m unittest discover -s tests` (지표·스코어링·캐시/폴백·API 계약 검증)

# 📈 미국 주식 차트 · 재무 AI 스크리너

yfinance 실시간 주가·재무 데이터와 Gemini AI를 결합한 종목 스크리닝 웹앱입니다.

## 주요 기능
- S&P 500(500개) 목록 및 야후 파이낸스 실시간 종목 검색
- 20일·60일 이동평균선, RSI(14), 재무지표(PER·매출성장률·영업이익률·부채비율) 기반 AI 추천
- Recharts 인터랙티브 차트 및 최근 뉴스 요약
- Yahoo Finance `t.info`가 서버 IP에서 막히면(429/401) **SEC EDGAR 공시(XBRL)** 에서
  PER·매출성장률·영업이익률·부채비율을 계산해 채운다(API 키 불필요).
  응답의 `financials_source`가 `sec_edgar`면 그 경로로 보완된 행이다.
  `SEC_EDGAR_FALLBACK=0` 환경변수로 끌 수 있다.

## 실행 방법
1. 백엔드: `pip install -r requirements.txt` 후 `GEMINI_API_KEY` 설정 → `python -m uvicorn api:app --reload --port 8000`
2. 프론트: `cd frontend && npm install && npm run dev`

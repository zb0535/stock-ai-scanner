import json
import re
import pandas as pd
import streamlit as st
import yfinance as yf
from google import genai
from google.genai import types
import stock_tools

MODEL_ID = "gemini-3.5-flash-lite"

SYSTEM_PROMPT = """너는 미국 주식 차트·재무제표 스크리닝 보조 봇이다.
제공된 데이터에 없는 가격·지표·뉴스를 절대 지어내지 마라.
선정한 종목마다 차트 추세(이평선·RSI)와 재무 지표(PER·매출성장률·영업이익률·부채비율)를 실제 숫자 그대로 인용하고, 리스크도 함께 짚어라.
반드시 아래 JSON 형식으로만 출력하라:
{
  "market_note": "후보군 전반 요약 1~2문장",
  "top_picks": [
    {
      "ticker": "티커",
      "price": 0.0,
      "reason": "차트+재무 기준 추천 근거 (실제 수치 포함)",
      "risk": "주의할 점 또는 단점"
    }
  ]
}"""

st.set_page_config(page_title="미국 주식 AI 스크리너", page_icon="📈", layout="wide")
st.title("📈 미국 주식 차트·재무 AI 스크리너")

criteria = st.text_input(
    "스크리닝 조건",
    value="상승 추세(정배열)이면서 RSI 70 미만으로 과열되지 않았고, 매출 성장률과 영업이익률이 탄탄한 상위 3개 종목을 추천해 줘.",
)

if st.button("분석 시작", type="primary"):
    tickers = stock_tools.get_watchlist()
    bar = st.progress(0, text="종목 데이터 조회 중...")
    rows = []
    for i, t in enumerate(tickers):
        bar.progress((i + 1) / len(tickers), text=f"{t} 조회 중... ({i + 1}/{len(tickers)})")
        rows.append(stock_tools.get_stock_metrics(t))
    bar.empty()

    with st.spinner("Gemini가 조건에 맞는 종목을 고르는 중..."):
        client = genai.Client()
        res = client.models.generate_content(
            model=MODEL_ID,
            contents=f"조건: {criteria}\n\n종목 데이터:\n{json.dumps(rows, ensure_ascii=False)}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0,
            ),
        )

    raw = re.sub(r"^```json\s*|\s*```$", "", (res.text or "").strip())
    data = json.loads(raw)

    st.info(data.get("market_note", ""))
    st.subheader("AI 추천 TOP 3")
    cols = st.columns(3)
    for col, pick in zip(cols, data.get("top_picks", [])):
        with col:
            st.metric(pick["ticker"], f"${pick['price']}")
            st.write("**추천 근거:**", pick["reason"])
            st.caption(f"⚠️ 리스크: {pick['risk']}")
            hist = yf.Ticker(pick["ticker"]).history(period="6mo")["Close"]
            st.line_chart(hist, height=180)

    with st.expander("전체 15개 종목 원본 수치 보기"):
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
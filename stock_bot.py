import sys
from google import genai
from google.genai import types
import stock_tools

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL_ID = "gemini-3.5-flash-lite"

SYSTEM_PROMPT = """너는 미국 주식 차트·재무제표 스크리닝 보조 봇이다.
반드시 screen_all_stocks 도구를 호출해 실제 수치를 확인한 뒤에만 답하라.
도구 결과에 없는 가격·지표·뉴스를 절대 지어내지 마라.
선정한 종목마다 차트 추세(이평선·RSI)와 재무 지표(PER·매출성장률·영업이익률·부채비율)를 실제 숫자 그대로 인용하고, 리스크도 함께 짚어라.
최종 답변은 반드시 아래 JSON 형식으로 출력하라:
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


def run_stock_bot(criteria: str):
    def screen_all_stocks() -> list[dict]:
        """후보군 미국 주식 전체의 차트 지표와 재무 지표를 한 번에 조회합니다."""
        tickers = stock_tools.get_watchlist()
        results = []
        for t in tickers:
            print(f"    - {t} 조회 중...", flush=True)
            results.append(stock_tools.get_stock_metrics(t))
        return results

    client = genai.Client()
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[screen_all_stocks],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0,
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=criteria)])]

    for turn in range(1, 5):
        print(f"\n[턴 {turn}] Gemini 판단 중...", flush=True)
        res = client.models.generate_content(model=MODEL_ID, contents=contents, config=config)

        if not res.function_calls:
            print("\n=== 종목 스크리닝 결과 ===")
            print(res.text)
            return

        contents.append(res.candidates[0].content)
        tool_parts = []
        for fc in res.function_calls:
            print(f"  -> [도구 호출] {fc.name}()", flush=True)
            result = screen_all_stocks()
            tool_parts.append(
                types.Part.from_function_response(name=fc.name, response={"result": result})
            )
        contents.append(types.Content(role="user", parts=tool_parts))

    print("최대 턴 도달로 종료합니다.")


if __name__ == "__main__":
    default_q = "상승 추세(정배열)이면서 RSI 70 미만으로 과열되지 않았고, 매출 성장률과 영업이익률이 탄탄한 상위 3개 종목을 추천해 줘."
    question = sys.argv[1] if len(sys.argv) > 1 else default_q
    run_stock_bot(question)
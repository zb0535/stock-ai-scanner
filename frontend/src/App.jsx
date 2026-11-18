import { useEffect, useState } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
} from "recharts";
import "./App.css";

const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

const TABS = {
  "인기": [
    { symbol: "NVDA", name: "NVIDIA Corp.", exchange: "NASDAQ" },
    { symbol: "AAPL", name: "Apple Inc.", exchange: "NASDAQ" },
    { symbol: "MSFT", name: "Microsoft Corp.", exchange: "NASDAQ" },
    { symbol: "TSLA", name: "Tesla, Inc.", exchange: "NASDAQ" },
    { symbol: "AMZN", name: "Amazon.com, Inc.", exchange: "NASDAQ" },
    { symbol: "LLY", name: "Eli Lilly and Co.", exchange: "NYSE" },
  ],
  "반도체·AI": [
    { symbol: "NVDA", name: "NVIDIA Corp.", exchange: "NASDAQ" },
    { symbol: "AVGO", name: "Broadcom Inc.", exchange: "NASDAQ" },
    { symbol: "AMD", name: "Advanced Micro Devices", exchange: "NASDAQ" },
    { symbol: "TSM", name: "Taiwan Semiconductor", exchange: "NYSE" },
    { symbol: "ASML", name: "ASML Holding", exchange: "NASDAQ" },
    { symbol: "PLTR", name: "Palantir Technologies", exchange: "NYSE" },
    { symbol: "IONQ", name: "IonQ, Inc.", exchange: "NYSE" },
  ],
  "빅테크·성장": [
    { symbol: "GOOGL", name: "Alphabet Inc.", exchange: "NASDAQ" },
    { symbol: "META", name: "Meta Platforms", exchange: "NASDAQ" },
    { symbol: "NFLX", name: "Netflix, Inc.", exchange: "NASDAQ" },
    { symbol: "COST", name: "Costco Wholesale", exchange: "NASDAQ" },
    { symbol: "JPM", name: "JPMorgan Chase", exchange: "NYSE" },
    { symbol: "V", name: "Visa Inc.", exchange: "NYSE" },
  ],
  "한국": [
    { symbol: "005930.KS", name: "삼성전자", exchange: "KOSPI" },
    { symbol: "000660.KS", name: "SK하이닉스", exchange: "KOSPI" },
    { symbol: "035420.KS", name: "NAVER", exchange: "KOSPI" },
    { symbol: "035720.KS", name: "카카오", exchange: "KOSPI" },
    { symbol: "005380.KS", name: "현대차", exchange: "KOSPI" },
  ],
};

const PRESETS = [
  {
    label: "🔥 상승 추세 · 저과열",
    text: "상승 추세(정배열)이면서 RSI 70 미만으로 과열되지 않았고, 매출 성장률과 영업이익률이 탄탄한 상위 3개 종목을 추천해 줘.",
  },
  {
    label: "💎 저PER 가치주",
    text: "Trailing PER 또는 Forward PER이 상대적으로 낮고 영업이익률과 부채비율이 안정적인 가치주 상위 3개를 추천해 줘.",
  },
  {
    label: "🚀 고성장 테크주",
    text: "매출 성장률(revenue_growth)이 가장 높고 20일선 위에서 모멘텀이 살아있는 상위 3개 종목을 추천해 줘.",
  },
];


export default function App() {
  const [tickers, setTickers] = useState([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState("인기");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [criteria, setCriteria] = useState(PRESETS[0].text);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const [sp500, setSp500] = useState([]);

  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const openDetail = async (pick) => {
    setDetail({ ...pick, levels: null, quarters: [] });
    setDetailLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/stock/${pick.ticker}`);
      const data = await res.json();
      setDetail({ ...pick, ...data });
    } finally {
      setDetailLoading(false);
    }
  };
    
    const loadDefault = () => {
    fetch(`${API_BASE}/api/watchlist`)
      .then((r) => r.json())
      .then((d) => setTickers(d.tickers || []))
      .catch(() => setTickers(["AAPL", "MSFT", "NVDA", "AMZN", "LLY"]));
  };

  useEffect(() => {
    const saved = localStorage.getItem("stock_watchlist");
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.length > 0) {
          setTickers(parsed);
          return;
        }
      } catch {}
    }
    loadDefault();
  }, []);

  useEffect(() => {
    if (tickers.length > 0) {
      localStorage.setItem("stock_watchlist", JSON.stringify(tickers));
    }
  }, [tickers]);

  useEffect(() => {
  fetch(`${API_BASE}/api/sp500`)
    .then((r) => r.json())
    .then((d) => setSp500(d.results || []))
    .catch(() => {});
}, []);
  
  useEffect(() => {
    if (!query.trim()) {
      setSearchResults([]);
      return;
    }
    const timer = setTimeout(async () => {
      setSearching(true);
      try {
        const res = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}`);
        const data = await res.json();
        setSearchResults(data.results || []);
      } catch {
        setSearchResults([]);
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const toggleTicker = (sym) => {
    setTickers((prev) =>
      prev.includes(sym) ? prev.filter((t) => t !== sym) : [...prev, sym]
    );
  };

  const handleAnalyze = async (e) => {
    e.preventDefault();
    if (tickers.length === 0) return setError("종목을 1개 이상 선택해 주세요.");
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/screen`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ criteria, tickers }),
      });
      if (!res.ok) throw new Error(`서버 오류 (${res.status})`);
      setResult(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const listItems = query.trim()
  ? searchResults
  : tab === "S&P 500 (500개)"
    ? sp500
    : TABS[tab];

  return (
    <div className="shell">
      <div className="page">
        <header className="header">
          <div className="badge">AI QUANT SCREENER</div>
          <h1>미국 주식 차트 · 재무 AI 스크리너</h1>
          <p>이동평균선(20·60일) · RSI · 재무제표 · 최신 뉴스를 종합해 선별합니다</p>
        </header>

        <section className="panel">
          <div className="panel-head">
            <span>관심 종목 풀 ({tickers.length}개 선택됨)</span>
            <button type="button" className="picker-btn" onClick={() => setModalOpen(true)}>
              🔍 종목 검색 · 선택
            </button>
          </div>
          <div className="chips">
            {tickers.map((t) => (
              <span className="chip" key={t}>
                {t}
                <button type="button" onClick={() => toggleTicker(t)}>×</button>
              </span>
            ))}
          </div>

          <div className="presets">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                className={`preset-btn ${criteria === p.text ? "active" : ""}`}
                onClick={() => setCriteria(p.text)}
              >
                {p.label}
              </button>
            ))}
          </div>

          <form className="search-box" onSubmit={handleAnalyze}>
            <input
              value={criteria}
              onChange={(e) => setCriteria(e.target.value)}
              placeholder="스크리닝 조건을 입력하세요..."
            />
            <button type="submit" disabled={loading}>
              {loading ? "분석 중..." : "✨ AI 분석 실행"}
            </button>
          </form>
        </section>

        {error && <div className="error">⚠️ {error}</div>}

        {loading && (
          <section className="grid">
            {[1, 2, 3].map((n) => (
              <div className="card skeleton" key={n}>
                <div className="sk-line w40" />
                <div className="sk-box" />
                <div className="sk-line w90" />
                <div className="sk-line w70" />
              </div>
            ))}
          </section>
        )}

        {result && !loading && (
          <>
            <div className="summary">
              <span className="summary-tag">MARKET INSIGHT</span>
              <p>{result.analysis.market_note}</p>
            </div>

            <section className="grid">
              {result.analysis.top_picks.map((pick, i) => (
                <article className="card clickable" key={pick.ticker} onClick={() => openDetail(pick)}>
                  <div className="card-top">
                    <div>
                      <span className="rank">TOP {i + 1}</span>
                      <div className="ticker">{pick.ticker}</div>
                    </div>
                    <div className="price">${pick.price.toLocaleString()}</div>
                  </div>

                  <div className="chart-wrap">
                    <ResponsiveContainer width="100%" height={190}>
                      <LineChart data={pick.chart} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#ffffff10" />
                        <XAxis dataKey="date" tick={{ fill: "#6e7681", fontSize: 10 }} minTickGap={20} />
                        <YAxis domain={["auto", "auto"]} tick={{ fill: "#6e7681", fontSize: 10 }} />
                        <Tooltip
                          contentStyle={{
                            background: "#0f172a",
                            border: "1px solid #334155",
                            borderRadius: "10px",
                            fontSize: "12px",
                          }}
                        />
                        <Legend wrapperStyle={{ fontSize: "11px" }} />
                        <Line type="monotone" dataKey="price" name="종가" stroke="#38bdf8" dot={false} strokeWidth={2.2} />
                        <Line type="monotone" dataKey="ma20" name="20일선" stroke="#fbbf24" dot={false} strokeWidth={1.5} />
                        <Line type="monotone" dataKey="ma60" name="60일선" stroke="#a78bfa" dot={false} strokeWidth={1.5} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>

                  <div className="insight good">
                    <strong>추천 근거</strong>
                    <p>{pick.reason}</p>
                  </div>
                  <div className="insight warn">
                    <strong>리스크</strong>
                    <p>{pick.risk}</p>
                  </div>

                  {pick.news?.length > 0 && (
                    <div className="news">
                      <strong>최근 뉴스</strong>
                      <ul>
                        {pick.news.map((n, idx) => <li key={idx}>{n}</li>)}
                      </ul>
                    </div>
                  )}
                </article>
              ))}
            </section>

            <details className="table-box">
              <summary>전체 {result.all_stocks.length}개 종목 원본 지표 비교표</summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>티커</th><th>현재가</th><th>20일선</th><th>60일선</th><th>RSI(14)</th><th>추세</th><th>PER</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.all_stocks.map((s) => (
                      <tr key={s.ticker}>
                        <td><strong>{s.ticker}</strong></td>
                        <td>${s.price}</td>
                        <td>${s.ma20 ?? "-"}</td>
                        <td>${s.ma60 ?? "-"}</td>
                        <td>{s.rsi14 ?? "-"}</td>
                        <td>{s.trend}</td>
                        <td>{s.trailing_pe ? s.trailing_pe.toFixed(1) : "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </>
        )}
      </div>

      {modalOpen && (
        <div className="modal-backdrop" onClick={() => setModalOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>종목 검색 · 선택</h2>
              <button type="button" onClick={() => setModalOpen(false)}>✕</button>
            </div>

            <div className="modal-search">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="티커 또는 영문 회사명 검색 (예: Tesla, IONQ, Samsung)"
                autoFocus
              />
            </div>
            {!query.trim() && (
              <div className="tabs">
                {["S&P 500 (500개)", ...Object.keys(TABS)].map((t) => (
                  <button
                    key={t}
                    type="button"
                    className={`tab-btn ${tab === t ? "active" : ""}`}
                    onClick={() => setTab(t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
            )}

            <div className="modal-list">
              {searching && <div className="list-empty">야후 파이낸스 검색 중...</div>}
              {!searching && listItems.length === 0 && (
                <div className="list-empty">검색 결과가 없습니다.</div>
              )}
              {!searching && listItems.map((item) => {
                const picked = tickers.includes(item.symbol);
                return (
                  <div
                    key={item.symbol}
                    className={`list-row ${picked ? "picked" : ""}`}
                    onClick={() => toggleTicker(item.symbol)}
                  >
                    <div className="row-info">
                      <div className="row-top">
                        <span className="row-sym">{item.symbol}</span>
                        {item.exchange && <span className="row-ex">{item.exchange}</span>}
                      </div>
                      <div className="row-name">{item.name}</div>
                    </div>
                    <div className={`row-check ${picked ? "on" : ""}`}>
                      {picked ? "✓" : "+"}
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="modal-foot">
              <div className="foot-left">
                <button type="button" className="ghost" onClick={() => setTickers([])}>비우기</button>
                <button type="button" className="ghost" onClick={loadDefault}>기본 15개</button>
              </div>
              <button type="button" className="done" onClick={() => setModalOpen(false)}>
                {tickers.length}개 선택 완료
              </button>
            </div>
          </div>
        </div>
      )}
      
      {detail && (
        <div className="modal-backdrop" onClick={() => setDetail(null)}>
          <div className="modal detail-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>{detail.ticker} 상세 분석 · 참고선</h2>
              <button type="button" onClick={() => setDetail(null)}>✕</button>
            </div>

            <div className="modal-list">
              {detailLoading && <div className="list-empty">분기 실적 · 참고선 불러오는 중...</div>}

              {detail.levels?.current && (
                <div className="level-grid">
                  <div className="lvl"><span>현재가</span><strong>${detail.levels.current}</strong></div>
                  <div className="lvl red"><span>저항(60일 고점)</span><strong>${detail.levels.resistance_60d}</strong></div>
                  <div className="lvl yellow"><span>20일선</span><strong>${detail.levels.ma20}</strong></div>
                  <div className="lvl green"><span>지지(60일 저점)</span><strong>${detail.levels.support_60d}</strong></div>
                </div>
              )}

              <div className="chart-wrap">
                <ResponsiveContainer width="100%" height={220}>
                  <LineChart data={detail.chart} margin={{ top: 10, right: 10, left: -15, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#ffffff10" />
                    <XAxis dataKey="date" tick={{ fill: "#6e7681", fontSize: 10 }} minTickGap={15} />
                    <YAxis domain={["auto", "auto"]} tick={{ fill: "#6e7681", fontSize: 10 }} />
                    <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155", borderRadius: "10px" }} />
                    {detail.levels?.resistance_60d && (
                      <ReferenceLine y={detail.levels.resistance_60d} stroke="#f87171" strokeDasharray="4 4" />
                    )}
                    {detail.levels?.support_60d && (
                      <ReferenceLine y={detail.levels.support_60d} stroke="#34d399" strokeDasharray="4 4" />
                    )}
                    <Line type="monotone" dataKey="price" name="종가" stroke="#38bdf8" dot={false} strokeWidth={2.2} />
                    <Line type="monotone" dataKey="ma20" name="20일선" stroke="#fbbf24" dot={false} strokeWidth={1.5} />
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {detail.quarters?.length > 0 && (
                <div className="q-table">
                  <strong>📊 최근 4분기 실적 (단위: 백만 달러 $M)</strong>
                  <table>
                    <thead>
                      <tr><th>분기</th><th>매출</th><th>영업이익</th><th>순이익</th></tr>
                    </thead>
                    <tbody>
                      {detail.quarters.map((q) => (
                        <tr key={q.quarter}>
                          <td>{q.quarter}</td>
                          <td>{q.revenue_m?.toLocaleString() ?? "-"}</td>
                          <td>{q.operating_m?.toLocaleString() ?? "-"}</td>
                          <td>{q.net_m?.toLocaleString() ?? "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
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
import PaperTradingView from "./PaperTrading";

const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

// 종목 선택 모달의 시장 토글 값 (목록 출처: /api/sp500, /api/kospi200)
const MARKET_US = "sp500";
const MARKET_KR = "kospi200";

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

const STRATEGY_LABELS = {
  value: "가치",
  oversold: "과매도",
  momentum: "모멘텀",
  balanced: "균형",
};

const fmtPrice = (v, currency = "USD") => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  if (currency === "KRW") return `₩${Math.round(Number(v)).toLocaleString("ko-KR")}`;
  return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
};

// 통화: 서버가 주는 currency를 우선하고, 예전 응답은 티커 접미사로 추론한다.
const currencyOf = (row) => {
  const cur = String(row?.currency || "").toUpperCase();
  if (cur === "KRW" || cur === "USD") return cur;
  const sym = upper(row?.ticker);
  return sym.endsWith(".KS") || sym.endsWith(".KQ") ? "KRW" : "USD";
};

// 한국 종목의 한국어 이름 배지(삼성전자). 미국 종목의 영문명은 표시하지 않는다.
function KrName({ row }) {
  const name = row?.name;
  if (!name || name === row?.ticker) return null;
  if (currencyOf(row) !== "KRW") return null;
  return <span className="kr-name">{name}</span>;
}

const fmtPct = (v) =>
  v === null || v === undefined ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

const retClass = (v) =>
  v === null || v === undefined ? "flat" : v > 0 ? "up" : v < 0 ? "down" : "flat";

// 서버가 기록하는 추천일은 KST 기준 YYYY-MM-DD다. 브라우저 시간대와 무관하게 같은 날짜를 만든다.
const kstToday = () =>
  new Date(Date.now() + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);

const upper = (v) => String(v || "").trim().toUpperCase();

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
  // 최상위 화면: AI 스크리너 / 🤖 AI 모의투자
  const [view, setView] = useState("screener");
  const [tickers, setTickers] = useState([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState(null);   // 빠른 선택(인기·반도체·빅테크·한국), null이면 시장 전체 목록
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [criteria, setCriteria] = useState(PRESETS[0].text);
  const [mode, setMode] = useState("watchlist");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const [sp500, setSp500] = useState([]);
  // 종목 선택 모달의 시장 토글: S&P 500 목록 / 코스피 200 목록
  const [market, setMarket] = useState(MARKET_US);
  const [kospi, setKospi] = useState([]);
  const [kospiLoading, setKospiLoading] = useState(false);
  const [kospiError, setKospiError] = useState("");
  const [sp500Error, setSp500Error] = useState("");

  // 관심 종목에 이름·코드로 바로 추가(삼성전자·카카오·005930 → /api/resolve-ticker)
  const [addQuery, setAddQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [addMsg, setAddMsg] = useState("");

  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [perf, setPerf] = useState(null);
  const [perfLoading, setPerfLoading] = useState(false);
  const [perfError, setPerfError] = useState("");
  const [tracking, setTracking] = useState("");   // 추적 요청 중인 티커
  const [deleting, setDeleting] = useState(null); // 삭제 요청 중인 pick id

  const loadPerformance = async () => {
    setPerfLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/performance`);
      if (!res.ok) throw new Error(`성적표를 불러오지 못했습니다 (${res.status})`);
      setPerf(await res.json());
      setPerfError("");
    } catch (err) {
      setPerfError(err.message);
    } finally {
      setPerfLoading(false);
    }
  };

  // 사용자가 고른 픽만 성적표에 기록한다(분석 실행만으로는 기록되지 않는다).
  const trackPick = async (pick) => {
    const ticker = upper(pick?.ticker);
    if (!ticker || tracking) return;
    setTracking(ticker);
    setPerfError("");
    try {
      // 진입가는 서버가 계산한 현재가(all_stocks, 실시간 시세 반영)를 우선 보낸다.
      // 값이 없으면 서버가 실시간 시세 → 일봉 종가 순으로 채운다.
      const serverPrice = (result?.all_stocks || [])
        .find((s) => upper(s.ticker) === ticker)?.price;
      const entry = serverPrice ?? pick?.price ?? null;
      const res = await fetch(`${API_BASE}/api/performance/track`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker,
          entry_price: typeof entry === "number" && entry > 0 ? entry : null,
          strategy: result?.strategy || "",
          mode: result?.mode || mode,
          quant_score: pick?.quant_score ?? null,
          criteria,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail === "invalid_ticker"
            ? "가격을 확인할 수 없는 종목이라 추적할 수 없습니다."
            : `추적 추가 실패 (${res.status})`
        );
      }
      await loadPerformance();
    } catch (err) {
      setPerfError(err.message);
    } finally {
      setTracking("");
    }
  };

  // 성적표 행 하나만 지운다.
  const deletePick = async (id) => {
    if (id === null || id === undefined || deleting !== null) return;
    setDeleting(id);
    setPerfError("");
    try {
      const res = await fetch(`${API_BASE}/api/performance/${id}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail === "pick_not_found"
            ? "이미 삭제된 항목입니다."
            : `삭제 실패 (${res.status})`
        );
      }
      await loadPerformance();
    } catch (err) {
      setPerfError(err.message);
    } finally {
      setDeleting(null);
    }
  };

  // 발굴·추천된 종목을 내 관심 종목 풀에 바로 넣는다(localStorage에 함께 저장된다).
  const addToWatchlist = (ticker) => {
    const sym = upper(ticker);
    if (!sym) return;
    setTickers((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
  };

  const addByQuery = async (e) => {
    e.preventDefault();
    const q = addQuery.trim();
    if (!q || adding) return;
    setAdding(true);
    setAddMsg("");
    try {
      const res = await fetch(`${API_BASE}/api/resolve-ticker?q=${encodeURIComponent(q)}`);
      if (!res.ok) {
        setAddMsg(`'${q}' 종목을 찾지 못했습니다.`);
        return;
      }
      const data = await res.json();
      addToWatchlist(data.ticker);
      setAddQuery("");
      setAddMsg(`${data.name || data.ticker} → ${data.ticker} 추가됨`);
    } catch {
      setAddMsg("종목을 찾지 못했습니다. 잠시 후 다시 시도해 주세요.");
    } finally {
      setAdding(false);
    }
  };

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
    loadPerformance();
  }, []);

  useEffect(() => {
  fetch(`${API_BASE}/api/sp500`)
    .then((r) => r.json())
    .then((d) => setSp500(d.results || []))
    .catch(() => setSp500Error("S&P 500 목록을 불러오지 못했습니다."));
}, []);

  // 코스피 200 목록 — 모달에서 🇰🇷 코스피 200 토글을 처음 열 때 한 번만 불러온다.
  const loadKospi200 = async () => {
    if (kospi.length || kospiLoading) return;
    setKospiLoading(true);
    setKospiError("");
    try {
      const res = await fetch(`${API_BASE}/api/kospi200`);
      if (!res.ok) throw new Error(`코스피 200 목록을 불러오지 못했습니다 (${res.status})`);
      const data = await res.json();
      const rows = data.results || [];
      setKospi(rows);
      if (!rows.length) setKospiError("코스피 200 목록이 비어 있습니다.");
    } catch (err) {
      setKospiError(err.message);
    } finally {
      setKospiLoading(false);
    }
  };

  useEffect(() => {
    if (modalOpen && !tab && market === MARKET_KR) loadKospi200();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modalOpen, tab, market]);
  
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
    if (mode === "watchlist" && tickers.length === 0) return setError("종목을 1개 이상 선택해 주세요.");
    setLoading(true);
    setError("");
    try {
      const body = { criteria, mode };
      if (mode === "watchlist") body.tickers = tickers;
      const res = await fetch(`${API_BASE}/api/screen`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`서버 오류 (${res.status})`);
      setResult(await res.json());
      // 분석은 이력을 남기지 않는다. 카드의 "📌 추적 추가"를 누를 때만 성적표에 쌓인다.
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const listItems = query.trim()
  ? searchResults
  : tab
    ? (TABS[tab] || [])
    : market === MARKET_KR
      ? kospi
      : sp500;

  // 목록을 불러오는 중/실패 상태 — "결과 없음" 문구와 겹치지 않게 구분한다.
  const browsePending = !query.trim() && !tab && (
    (market === MARKET_KR && (kospiLoading || Boolean(kospiError) || kospi.length === 0))
    || (market === MARKET_US && (Boolean(sp500Error) || sp500.length === 0))
  );

  const perfSummary = perf?.summary || {
    total_picks: 0,
    evaluated_picks: 0,
    win_count: 0,
    win_rate_pct: null,
    avg_return_pct: null,
  };
  const perfPicks = perf?.picks || [];
  const lossCount = perfSummary.evaluated_picks - perfSummary.win_count;

  // 오늘(KST)·같은 전략으로 이미 추적 중인지 성적표 응답으로 판단한다.
  const today = kstToday();
  const trackedSet = new Set(
    perfPicks.map((p) => `${p.recommended_date}|${upper(p.ticker)}|${p.strategy || ""}`)
  );
  const isTracked = (ticker) =>
    trackedSet.has(`${today}|${upper(ticker)}|${result?.strategy || ""}`);
  const inWatchlist = (ticker) => tickers.includes(upper(ticker));

  return (
    <div className="shell">
      <div className="page">
        <nav className="app-tabs" role="tablist" aria-label="화면 선택">
          <button
            type="button"
            role="tab"
            aria-selected={view === "screener"}
            className={`app-tab ${view === "screener" ? "active" : ""}`}
            onClick={() => setView("screener")}
          >
            📊 AI 스크리너
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "paper"}
            className={`app-tab ${view === "paper" ? "active" : ""}`}
            onClick={() => setView("paper")}
          >
            🤖 AI 모의투자
          </button>
        </nav>

        {view === "paper" ? (
          <PaperTradingView />
        ) : (
          <>
        <header className="header">
          <div className="badge">AI QUANT SCREENER</div>
          <h1>한국·미국 주식 차트 · 재무 AI 스크리너</h1>
          <p>이동평균선(20·60일) · RSI · 재무제표 · 최신 뉴스를 종합해 선별합니다</p>
        </header>

        <section className="panel">
          <div className="mode-switch" role="tablist" aria-label="스크리닝 모드">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "watchlist"}
              className={`mode-btn ${mode === "watchlist" ? "active" : ""}`}
              onClick={() => { setMode("watchlist"); setError(""); }}
            >
              📋 내 관심 종목
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "sp500"}
              className={`mode-btn ${mode === "sp500" ? "active" : ""}`}
              onClick={() => { setMode("sp500"); setError(""); }}
            >
              🌐 S&P 500 자동 발굴
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "kospi200"}
              className={`mode-btn ${mode === "kospi200" ? "active" : ""}`}
              onClick={() => { setMode("kospi200"); setError(""); }}
            >
              🇰🇷 코스피 200 발굴
            </button>
          </div>

          {mode === "sp500" ? (
            <div className="mode-note">
              S&P 500 약 500종목을 6개월 차트(이평선·RSI·MACD·볼린저·거래량)로 먼저 훑어 조건에
              맞는 상위 15개만 뽑고, 그 종목에만 재무·뉴스를 붙여 AI가 최종 선정합니다.
              서버가 50분마다 미리 계산해 두기 때문에 보통 몇 초 만에 응답합니다.
              (서버가 막 켜져 캐시가 아직 비었을 때만 첫 계산에 1~2분 걸립니다.)
            </div>
          ) : mode === "kospi200" ? (
            <div className="mode-note">
              코스피 200 약 200종목을 같은 방식으로 먼저 훑어 상위 15개만 뽑고, 그 종목에만
              재무·뉴스를 붙여 AI가 최종 선정합니다. 재무 지표는 네이버 금융(연간 재무제표)에서
              보완하고 가격은 원화(₩)로 표시합니다. 서버가 50분마다 미리 계산해 둡니다.
            </div>
          ) : (
            <>
              <div className="panel-head">
                <span>관심 종목 풀 ({tickers.length}개 선택됨)</span>
                <button type="button" className="picker-btn" onClick={() => setModalOpen(true)}>
                  🔍 종목 검색 · 선택
                </button>
              </div>
              <form className="kr-add" onSubmit={addByQuery}>
                <input
                  value={addQuery}
                  onChange={(e) => { setAddQuery(e.target.value); setAddMsg(""); }}
                  placeholder="종목명·코드로 추가 (삼성전자, 카카오, 005930, AAPL)"
                  aria-label="종목명 또는 코드로 관심 종목 추가"
                />
                <button type="submit" disabled={adding || !addQuery.trim()}>
                  {adding ? "찾는 중..." : "+ 추가"}
                </button>
              </form>
              {addMsg && <div className="kr-add-msg">{addMsg}</div>}
              <div className="chips">
                {tickers.map((t) => (
                  <span className="chip" key={t}>
                    {t}
                    <button type="button" onClick={() => toggleTicker(t)}>×</button>
                  </span>
                ))}
              </div>
            </>
          )}

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
              {loading
                ? mode === "kospi200"
                  ? "코스피 200 발굴 중..."
                  : mode === "sp500"
                    ? "S&P 500 발굴 중..."
                    : "분석 중..."
                : "✨ AI 분석 실행"}
            </button>
          </form>
        </section>

        {error && <div className="error">⚠️ {error}</div>}

        <section className="panel scoreboard" aria-label="AI 추천 성적표">
          <div className="score-head">
            <div>
              <h2 className="score-title">📊 AI 추천 성적표</h2>
              <p className="score-sub">
                직접 추적에 추가한 종목만 진입가 대비 현재가로 평가합니다 · 최근 {perfSummary.total_picks}건
                {perf?.as_of && ` · 기준 ${perf.as_of.slice(5, 10)} ${perf.as_of.slice(11, 16)}`}
              </p>
            </div>
            <button type="button" className="refresh-btn" onClick={loadPerformance} disabled={perfLoading}>
              {perfLoading ? "불러오는 중..." : "🔄 새로고침"}
            </button>
          </div>

          {perfError && <div className="score-note">⚠️ {perfError}</div>}

          <div className="score-metrics">
            <div className="metric">
              <span>총 추적 종목</span>
              <strong>{perfSummary.total_picks}</strong>
              <em>평가 {perfSummary.evaluated_picks}건</em>
            </div>
            <div className="metric">
              <span>적중률</span>
              <strong className={retClass(perfSummary.win_rate_pct)}>
                {perfSummary.win_rate_pct === null ? "-" : `${perfSummary.win_rate_pct.toFixed(1)}%`}
              </strong>
              <em>승 {perfSummary.win_count} · 패 {lossCount}</em>
            </div>
            <div className="metric">
              <span>평균 수익률</span>
              <strong className={retClass(perfSummary.avg_return_pct)}>
                {fmtPct(perfSummary.avg_return_pct)}
              </strong>
              <em>
                {perfSummary.best_pick
                  ? `최고 ${perfSummary.best_pick.ticker} ${fmtPct(perfSummary.best_pick.return_pct)}`
                  : "아직 평가 전"}
              </em>
            </div>
          </div>

          {perfPicks.length > 0 ? (
            <div className="table-scroll score-table">
              <table>
                <thead>
                  <tr>
                    <th>추천일</th><th>티커</th><th>전략</th><th>진입가</th><th>현재가</th><th>수익률</th><th>보유</th><th aria-label="삭제" />
                  </tr>
                </thead>
                <tbody>
                  {perfPicks.map((p) => (
                    <tr key={p.id ?? `${p.recommended_date}-${p.ticker}-${p.strategy}`}>
                      <td>{p.recommended_date?.slice(5)}</td>
                      <td>
                        <strong>{p.ticker}</strong>
                        <KrName row={p} />
                        {p.mode === "sp500" && <span className="mini-tag">S&amp;P</span>}
                        {p.mode === "kospi200" && <span className="mini-tag kr">코스피200</span>}
                      </td>
                      <td>{STRATEGY_LABELS[p.strategy] || p.strategy || "-"}</td>
                      <td>{fmtPrice(p.entry_price, currencyOf(p))}</td>
                      <td>{fmtPrice(p.current_price, currencyOf(p))}</td>
                      <td>
                        <span className={`ret ${retClass(p.return_pct)}`}>
                          {p.return_pct === null || p.return_pct === undefined
                            ? "평가 대기"
                            : fmtPct(p.return_pct)}
                        </span>
                      </td>
                      <td>{p.days_held === null || p.days_held === undefined ? "-" : `${p.days_held}일`}</td>
                      <td>
                        <button
                          type="button"
                          className="del-btn"
                          title={`${p.ticker} 추적 삭제`}
                          aria-label={`${p.ticker} 추적 삭제`}
                          disabled={deleting === p.id || p.id === null || p.id === undefined}
                          onClick={() => deletePick(p.id)}
                        >
                          🗑️
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="score-empty">
              {perfLoading
                ? "성적표를 불러오는 중..."
                : "아직 추적 중인 종목이 없습니다. 위 추천 카드의 📌 추적 추가를 누르면 이곳에 쌓입니다."}
            </div>
          )}
        </section>

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
            {result.mode === "sp500" && (
              <div className="scan-badge">
                🌐 S&P 500 {result.universe_scanned}종목 자동 발굴 → 상위 {result.all_stocks.length}종목 분석
              </div>
            )}
            {result.mode === "kospi200" && (
              <div className="scan-badge kr">
                🇰🇷 코스피 200 {result.universe_scanned}종목 자동 발굴 → 상위 {result.all_stocks.length}종목 분석
                · 가격은 원화(₩)
              </div>
            )}
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
                      <div className="ticker">
                        {pick.ticker} <KrName row={pick} />
                      </div>
                    </div>
                    <div className="price">{fmtPrice(pick.price, currencyOf(pick))}</div>
                  </div>

                  <div className="card-actions">
                    <button
                      type="button"
                      className={`track-btn ${isTracked(pick.ticker) ? "on" : ""}`}
                      disabled={isTracked(pick.ticker) || tracking === upper(pick.ticker)}
                      title="이 종목을 AI 추천 성적표에 기록"
                      onClick={(e) => { e.stopPropagation(); trackPick(pick); }}
                    >
                      {isTracked(pick.ticker)
                        ? "✅ 추적 중"
                        : tracking === upper(pick.ticker) ? "추적 중..." : "📌 추적 추가"}
                    </button>
                    <button
                      type="button"
                      className={`watch-btn ${inWatchlist(pick.ticker) ? "on" : ""}`}
                      disabled={inWatchlist(pick.ticker)}
                      title="내 관심 종목 풀에 추가"
                      onClick={(e) => { e.stopPropagation(); addToWatchlist(pick.ticker); }}
                    >
                      {inWatchlist(pick.ticker) ? "✓ 관심 중" : "+ 관심 종목"}
                    </button>
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
                      <th>티커</th><th>현재가</th><th>20일선</th><th>60일선</th><th>RSI(14)</th><th>추세</th><th>PER</th><th aria-label="관심 종목">관심</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.all_stocks.map((s) => (
                      <tr key={s.ticker}>
                        <td><strong>{s.ticker}</strong> <KrName row={s} /></td>
                        <td>{fmtPrice(s.price, currencyOf(s))}</td>
                        <td>{fmtPrice(s.ma20, currencyOf(s))}</td>
                        <td>{fmtPrice(s.ma60, currencyOf(s))}</td>
                        <td>{s.rsi14 ?? "-"}</td>
                        <td>{s.trend}</td>
                        <td>{s.trailing_pe ? s.trailing_pe.toFixed(1) : "-"}</td>
                        <td>
                          <button
                            type="button"
                            className={`watch-btn mini ${inWatchlist(s.ticker) ? "on" : ""}`}
                            disabled={inWatchlist(s.ticker)}
                            title={`${s.ticker} 관심 종목에 추가`}
                            onClick={() => addToWatchlist(s.ticker)}
                          >
                            {inWatchlist(s.ticker) ? "✓ 관심 중" : "+ 관심"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </>
        )}
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
                placeholder="티커·회사명 검색 (예: Tesla, IONQ, 삼성전자, 005930)"
                autoFocus
              />
            </div>
            {!query.trim() && (
              <div className="modal-market">
                <div className="mode-switch market-switch" role="tablist" aria-label="종목 목록 시장">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={market === MARKET_US && !tab}
                    className={`mode-btn ${market === MARKET_US && !tab ? "active" : ""}`}
                    onClick={() => { setMarket(MARKET_US); setTab(null); }}
                  >
                    🌐 S&amp;P 500
                    <span className="market-count">{sp500.length || 500}</span>
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={market === MARKET_KR && !tab}
                    className={`mode-btn ${market === MARKET_KR && !tab ? "active" : ""}`}
                    onClick={() => { setMarket(MARKET_KR); setTab(null); loadKospi200(); }}
                  >
                    🇰🇷 코스피 200
                    <span className="market-count">{kospi.length || 200}</span>
                  </button>
                </div>
              </div>
            )}
            {!query.trim() && (
              <div className="tabs tabs-quick">
                <span className="tabs-label">빠른 선택</span>
                {Object.keys(TABS).map((t) => (
                  <button
                    key={t}
                    type="button"
                    className={`tab-btn ${tab === t ? "active" : ""}`}
                    onClick={() => setTab(tab === t ? null : t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
            )}

            <div className="modal-list">
              {searching && <div className="list-empty">야후 파이낸스 검색 중...</div>}
              {!searching && !query.trim() && !tab && market === MARKET_US && sp500.length === 0 && (
                <div className="list-empty">
                  {sp500Error || "S&P 500 목록을 불러오는 중..."}
                </div>
              )}
              {!searching && !query.trim() && !tab && market === MARKET_KR && kospi.length === 0 && (
                <div className="list-empty">
                  {kospiLoading ? "코스피 200 목록을 불러오는 중..." : (kospiError || "코스피 200 목록이 비어 있습니다.")}
                </div>
              )}
              {!searching && listItems.length === 0 && !browsePending && (
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
              <h2>
                {detail.name ? `${detail.name} (${detail.ticker})` : detail.ticker} 상세 분석 · 참고선
              </h2>
              <button type="button" onClick={() => setDetail(null)}>✕</button>
            </div>

            <div className="detail-actions">
              <button
                type="button"
                className={`track-btn ${isTracked(detail.ticker) ? "on" : ""}`}
                disabled={isTracked(detail.ticker) || tracking === upper(detail.ticker)}
                onClick={() => trackPick(detail)}
              >
                {isTracked(detail.ticker)
                  ? "✅ 추적 중"
                  : tracking === upper(detail.ticker) ? "추적 중..." : "📌 추적 추가"}
              </button>
              <button
                type="button"
                className={`watch-btn ${inWatchlist(detail.ticker) ? "on" : ""}`}
                disabled={inWatchlist(detail.ticker)}
                onClick={() => addToWatchlist(detail.ticker)}
              >
                {inWatchlist(detail.ticker) ? "✓ 관심 중" : "+ 관심 종목"}
              </button>
            </div>

            <div className="modal-list">
              {detailLoading && <div className="list-empty">분기 실적 · 참고선 불러오는 중...</div>}

              {detail.levels?.current && (
                <div className="level-grid">
                  <div className="lvl"><span>현재가</span><strong>{fmtPrice(detail.levels.current, currencyOf(detail))}</strong></div>
                  <div className="lvl red"><span>저항(60일 고점)</span><strong>{fmtPrice(detail.levels.resistance_60d, currencyOf(detail))}</strong></div>
                  <div className="lvl yellow"><span>20일선</span><strong>{fmtPrice(detail.levels.ma20, currencyOf(detail))}</strong></div>
                  <div className="lvl green"><span>지지(60일 저점)</span><strong>{fmtPrice(detail.levels.support_60d, currencyOf(detail))}</strong></div>
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
                  <strong>
                    📊 최근 4분기 실적 (단위: {currencyOf(detail) === "KRW" ? "백만 원 ₩M" : "백만 달러 $M"})
                  </strong>
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
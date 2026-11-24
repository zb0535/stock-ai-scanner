import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
} from "recharts";

const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

const MARKETS = [
  { value: "ALL", label: "🇰🇷+🇺🇸 한국+미국" },
  { value: "US", label: "🇺🇸 미국만" },
  { value: "KR", label: "🇰🇷 한국만" },
];

const STRATEGIES = [
  { value: "balanced", label: "균형" },
  { value: "momentum", label: "모멘텀(상승 추세)" },
  { value: "value", label: "가치(저PER)" },
  { value: "oversold", label: "과매도 반등" },
];

const won = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Math.round(Number(value));
  return `${n < 0 ? "-" : ""}₩${Math.abs(n).toLocaleString("ko-KR")}`;
};

const pct = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
};

const retClass = (value) => {
  if (value === null || value === undefined) return "flat";
  return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "flat";
};

const nativePrice = (value, currency) => {
  if (value === null || value === undefined) return "-";
  if (currency === "KRW") return `₩${Math.round(Number(value)).toLocaleString("ko-KR")}`;
  return `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
};

const sharesLabel = (row) =>
  row.currency === "KRW"
    ? `${Number(row.shares).toLocaleString("ko-KR")}주`
    : `${Number(row.shares).toLocaleString(undefined, { maximumFractionDigits: 4 })}주`;

// 서버 시각(ISO)을 KST "11-24 18:57"로 — 브라우저 시간대와 무관하게 같은 표기를 쓴다.
const kstDateTime = (iso) => {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const kst = new Date(date.getTime() + 9 * 60 * 60 * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(kst.getUTCMonth() + 1)}-${pad(kst.getUTCDate())} ${pad(kst.getUTCHours())}:${pad(kst.getUTCMinutes())}`;
};

export default function PaperTradingView() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [seed, setSeed] = useState("");
  const [markets, setMarkets] = useState("ALL");
  const [strategy, setStrategy] = useState("balanced");
  const [auto, setAuto] = useState(true);
  const [runResult, setRunResult] = useState(null);

  const apply = useCallback((body) => {
    setData(body);
    setSeed(String(Math.round(body?.account?.initial_cash_krw ?? 0)));
    setMarkets(body?.account?.markets ?? "ALL");
    setStrategy(body?.account?.strategy ?? "balanced");
    setAuto(Boolean(body?.account?.auto_enabled));
  }, []);

  const load = useCallback(
    async (silent = false) => {
      if (!silent) setLoading(true);
      try {
        const res = await fetch(`${API_BASE}/api/paper/status`);
        if (!res.ok) throw new Error(`계좌를 불러오지 못했습니다 (${res.status})`);
        apply(await res.json());
        setError("");
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    },
    [apply]
  );

  useEffect(() => {
    load();
  }, [load]);

  const saveSettings = async (reset = false) => {
    const amount = Number(String(seed).replace(/[^0-9.]/g, ""));
    if (!amount) {
      setError("시드머니를 입력해 주세요.");
      return;
    }
    if (reset && !window.confirm(`${won(amount)}으로 계좌를 초기화할까요? 보유 종목·체결 내역이 모두 지워집니다.`)) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/paper/configure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          initial_cash_krw: amount,
          markets,
          strategy,
          auto_enabled: auto,
          reset,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          body.detail === "cash_below_zero"
            ? "보유 현금보다 크게 줄일 수 없습니다. 계좌 초기화를 사용해 주세요."
            : body.detail === "initial_cash_krw_out_of_range"
              ? "시드머니는 10만 원 ~ 10억 원 사이로 설정해 주세요."
              : `설정 저장 실패 (${res.status})`
        );
      }
      apply(body.status);
      setRunResult(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const runNow = async () => {
    setRunning(true);
    setError("");
    setRunResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/paper/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(`AI 매매 실행 실패 (${res.status})`);
      setRunResult(body.result);
      apply(body.status);
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  };

  const account = data?.account;
  const positions = data?.positions || [];
  const trades = data?.trades || [];
  const schedule = data?.auto_schedule || [];

  const chart = useMemo(
    () =>
      (data?.equity_history || []).map((row) => ({
        date: String(row.date || "").slice(5),
        total: Math.round(row.total_value_krw || 0),
        ret: row.return_pct,
      })),
    [data]
  );

  if (loading && !data) {
    return <div className="list-empty">🤖 AI 모의투자 계좌 불러오는 중...</div>;
  }

  return (
    <div className="paper">
      <section className="panel paper-hero">
        <div className="score-head">
          <div>
            <h2 className="score-title">🤖 AI 모의투자</h2>
            <p className="score-sub">
              원화 통합 계좌 · {account?.markets === "KR" ? "한국" : account?.markets === "US" ? "미국" : "한국+미국"} ·{" "}
              {account?.strategy_label || "-"} · 1 USD = {won(account?.fx_rate)}{" "}
              <span className="mini-tag">{account?.fx_source || "-"}</span>
            </p>
          </div>
          <div className="paper-actions">
            <button type="button" className="refresh-btn" onClick={() => load()} disabled={loading}>
              🔄 새로고침
            </button>
            <button type="button" className="run-btn" onClick={runNow} disabled={running}>
              {running ? "AI 판단 중... (최대 1분)" : "▶ 지금 AI 매매 1회 실행"}
            </button>
          </div>
        </div>

        <div className="score-metrics paper-metrics">
          <div className="metric">
            <span>총 자산</span>
            <strong>{won(account?.total_value_krw)}</strong>
            <em>현금 {won(account?.cash_krw)} + 주식 {won(account?.stock_value_krw)}</em>
          </div>
          <div className="metric">
            <span>총 수익률</span>
            <strong className={retClass(account?.total_return_pct)}>
              {pct(account?.total_return_pct)}
            </strong>
            <em className={retClass(account?.total_pnl_krw)}>
              평가손익 {won(account?.total_pnl_krw)}
            </em>
          </div>
          <div className="metric">
            <span>시드머니(투자 원금)</span>
            <strong>{won(account?.initial_cash_krw)}</strong>
            <em>실현 손익 {won(account?.realized_pnl_krw)}</em>
          </div>
        </div>

        <div className="paper-schedule">
          {schedule.map((item) => (
            <span key={item.market} className={`sched ${item.enabled ? "" : "off"}`}>
              {item.market === "KR" ? "🇰🇷 한국" : "🇺🇸 미국"} {item.at} 이후 1회 ·{" "}
              {item.done_today ? "오늘 완료 ✅" : "오늘 대기"}
            </span>
          ))}
          <span className={`sched ${account?.auto_enabled ? "on" : "off"}`}>
            자동 실행 {account?.auto_enabled ? "켜짐" : "꺼짐"}
          </span>
          {data?.next_auto?.label && <span className="sched">다음: {data.next_auto.label}</span>}
        </div>
      </section>

      {error && <div className="error">⚠️ {error}</div>}

      {runResult && (
        <section className="panel paper-run-result">
          <div className="score-head">
            <div>
              <h3 className="score-title">
                {runResult.skipped === "already_ran_today" ? "이미 오늘 실행됨" : "매매 실행 결과"}
              </h3>
              <p className="score-sub">
                {runResult.ai_source === "gemini" ? "Gemini AI 판단" : "정량 점수 폴백(AI 미응답)"} ·{" "}
                {(runResult.markets || []).join("+")} · {kstDateTime(runResult.ran_at)}
              </p>
            </div>
            <button type="button" className="ghost" onClick={() => setRunResult(null)}>
              닫기
            </button>
          </div>
          {runResult.message && <p className="score-sub">{runResult.message}</p>}
          {(runResult.notes || []).filter(Boolean).map((note, index) => (
            <p className="paper-note" key={index}>
              💬 {note}
            </p>
          ))}
          <div className="paper-run-grid">
            <div>
              <strong>매수 {runResult.buys?.length || 0}건</strong>
              {(runResult.buys || []).map((trade) => (
                <div className="paper-run-row" key={`b-${trade.id}-${trade.ticker}`}>
                  <span className="side buy">BUY</span>
                  <b>{trade.ticker}</b>
                  <span>{sharesLabel(trade)}</span>
                  <span>{nativePrice(trade.price, trade.currency)}</span>
                  <span>{won(trade.amount_krw)}</span>
                </div>
              ))}
            </div>
            <div>
              <strong>매도 {runResult.sells?.length || 0}건</strong>
              {(runResult.sells || []).map((trade) => (
                <div className="paper-run-row" key={`s-${trade.id}-${trade.ticker}`}>
                  <span className="side sell">SELL</span>
                  <b>{trade.ticker}</b>
                  <span>{sharesLabel(trade)}</span>
                  <span>{nativePrice(trade.price, trade.currency)}</span>
                  <span className={retClass(trade.realized_pnl_krw)}>
                    {won(trade.realized_pnl_krw)}
                  </span>
                </div>
              ))}
            </div>
          </div>
          {(runResult.skipped || []).length > 0 && (
            <details className="paper-skipped">
              <summary>건너뛴 요청 {runResult.skipped.length}건</summary>
              <ul>
                {runResult.skipped.map((item, index) => (
                  <li key={index}>
                    {item.ticker || "-"} · {item.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      <section className="panel paper-settings">
        <div className="score-head">
          <div>
            <h3 className="score-title">⚙️ 계좌 설정</h3>
            <p className="score-sub">시드머니는 언제든 바꿀 수 있고, 초기화하면 새 시드로 다시 시작합니다.</p>
          </div>
        </div>
        <div className="paper-form">
          <label>
            시드머니 (원)
            <input
              inputMode="numeric"
              value={seed}
              onChange={(event) => setSeed(event.target.value.replace(/[^0-9]/g, ""))}
              placeholder="10000000"
            />
            <em>{won(seed)}</em>
          </label>
          <label>
            투자 시장
            <div className="seg">
              {MARKETS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className={markets === item.value ? "active" : ""}
                  onClick={() => setMarkets(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </label>
          <label>
            AI 전략
            <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              {STRATEGIES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="inline">
            <input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />
            매일 1회 자동 매매 (한국 15:40 · 미국 06:20 KST)
          </label>
        </div>
        <div className="paper-form-actions">
          <button type="button" className="run-btn" onClick={() => saveSettings(false)} disabled={saving}>
            {saving ? "저장 중..." : "설정 저장"}
          </button>
          <button type="button" className="ghost danger" onClick={() => saveSettings(true)} disabled={saving}>
            ♻️ 계좌 초기화
          </button>
        </div>
      </section>

      <section className="panel scoreboard paper-chart">
        <div className="score-head">
          <div>
            <h3 className="score-title">📈 수익률 추이</h3>
            <p className="score-sub">매매가 실행된 날의 총 자산(원화)입니다. 점선은 시드머니입니다.</p>
          </div>
        </div>
        {chart.length < 2 ? (
          <div className="list-empty">아직 기록이 하루치뿐입니다. AI 매매가 하루 더 돌면 선이 그려집니다.</div>
        ) : (
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={chart} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#ffffff10" />
                <XAxis dataKey="date" tick={{ fill: "#6e7681", fontSize: 10 }} minTickGap={20} />
                <YAxis
                  domain={["auto", "auto"]}
                  tick={{ fill: "#6e7681", fontSize: 10 }}
                  tickFormatter={(value) => `${Math.round(value / 10000).toLocaleString()}만`}
                />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155", borderRadius: "10px" }}
                  formatter={(value) => [won(value), "총 자산"]}
                />
                <ReferenceLine y={account?.initial_cash_krw} stroke="#94a3b8" strokeDasharray="4 4" />
                <Line type="monotone" dataKey="total" stroke="#38bdf8" dot={false} strokeWidth={2.2} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>

      <section className="panel scoreboard">
        <div className="score-head">
          <div>
            <h3 className="score-title">📦 보유 종목 ({positions.length})</h3>
            <p className="score-sub">평가액·손익은 현재 환율로 원화 환산한 값입니다(환차 포함).</p>
          </div>
        </div>
        {positions.length === 0 ? (
          <div className="list-empty">보유 종목이 없습니다. 위의 ‘▶ 지금 AI 매매 1회 실행’을 눌러 보세요.</div>
        ) : (
          <div className="table-scroll">
            <table className="paper-table">
              <thead>
                <tr>
                  <th>종목</th>
                  <th>수량</th>
                  <th>평단가</th>
                  <th>현재가</th>
                  <th>평가액</th>
                  <th>평가손익</th>
                  <th>AI 매수 사유</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((row) => (
                  <tr key={row.ticker}>
                    <td>
                      <b>{row.ticker}</b>
                      {row.name && row.name !== row.ticker && (
                        <span className={`mini-tag ${row.market === "KR" ? "kr" : ""}`}>{row.name}</span>
                      )}
                    </td>
                    <td>{sharesLabel(row)}</td>
                    <td>
                      {nativePrice(row.avg_price, row.currency)}
                      {row.currency === "USD" && (
                        <em className="sub"> ({won(row.avg_price_krw)})</em>
                      )}
                    </td>
                    <td>
                      {nativePrice(row.current_price, row.currency)}
                      <em className="sub"> {row.price_source}</em>
                    </td>
                    <td>{won(row.market_value_krw)}</td>
                    <td>
                      <span className={`ret ${retClass(row.pnl_pct)}`}>{pct(row.pnl_pct)}</span>
                      <em className={`sub ${retClass(row.pnl_krw)}`}>{won(row.pnl_krw)}</em>
                    </td>
                    <td className="reason" title={row.reason}>
                      {row.reason || "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel scoreboard">
        <div className="score-head">
          <div>
            <h3 className="score-title">🧾 최근 매매 ({trades.length})</h3>
            <p className="score-sub">AI가 체결한 매수·매도 내역입니다(최근 40건).</p>
          </div>
        </div>
        {trades.length === 0 ? (
          <div className="list-empty">아직 체결 내역이 없습니다.</div>
        ) : (
          <div className="table-scroll">
            <table className="paper-table">
              <thead>
                <tr>
                  <th>시각</th>
                  <th>구분</th>
                  <th>종목</th>
                  <th>수량</th>
                  <th>체결가</th>
                  <th>금액</th>
                  <th>실현 손익</th>
                  <th>근거</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((trade) => (
                  <tr key={trade.id}>
                    <td>{kstDateTime(trade.traded_at)}</td>
                    <td>
                      <span className={`side ${trade.side === "BUY" ? "buy" : "sell"}`}>{trade.side}</span>
                    </td>
                    <td>
                      <b>{trade.ticker}</b>
                      {trade.name && trade.name !== trade.ticker && (
                        <span className={`mini-tag ${trade.market === "KR" ? "kr" : ""}`}>{trade.name}</span>
                      )}
                    </td>
                    <td>{sharesLabel(trade)}</td>
                    <td>{nativePrice(trade.price, trade.currency)}</td>
                    <td>{won(trade.amount_krw)}</td>
                    <td>
                      {trade.realized_pnl_krw === null || trade.realized_pnl_krw === undefined ? (
                        "-"
                      ) : (
                        <span className={`ret ${retClass(trade.realized_pnl_krw)}`}>
                          {won(trade.realized_pnl_krw)}
                        </span>
                      )}
                    </td>
                    <td className="reason" title={trade.reason}>
                      {trade.reason || "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className="paper-disclaimer">
        ※ 모의투자입니다. 실제 주문이 나가지 않으며, 체결은 서버가 조회한 현재가로 기록됩니다. 과거 수익률이
        미래 수익을 보장하지 않습니다.
      </p>
    </div>
  );
}

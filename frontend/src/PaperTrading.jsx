import { useCallback, useEffect, useMemo, useState } from "react";
import { RealAccountCard, RealAccountModal } from "./RealAccount";
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

// 닉네임 + 4자리 PIN 세션 — 토큰은 localStorage에만 두고, 새로고침·재접속에도 유지한다.
const SESSION_KEY = "paper:session:v1";

const readSession = () => {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed?.token) return null;
    return parsed;
  } catch {
    return null;
  }
};

const authHeaders = (session) =>
  session?.token ? { "X-Paper-User-Token": session.token } : {};

const AUTH_ERROR = (status, detail) => {
  if (detail?.message) return detail.message;
  if (status === 401) return "PIN이 맞지 않습니다.";
  if (status === 429) return "PIN을 여러 번 틀렸습니다. 10분 뒤 다시 시도해 주세요.";
  if (status === 400) return "닉네임과 4자리 PIN을 확인해 주세요.";
  return `로그인하지 못했습니다 (${status})`;
};

const MARKETS = [
  { value: "US", label: "🇺🇸 미국 계좌" },
  { value: "KR", label: "🇰🇷 한국 계좌" },
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

// 서버가 주는 수수료·세금 기준(`fee_policy`)을 사람이 읽는 문장으로.
const feeRow = (policy, market, side) => {
  const row = policy?.[market]?.[side];
  if (!row) return null;
  const parts = [`수수료 ${row.commission_pct}%`];
  if (Number(row.tax_pct) > 0) parts.push(`증권거래세 ${row.tax_pct}%`);
  if (Number(row.fx_spread_pct) > 0) parts.push(`환전 스프레드 ${row.fx_spread_pct}%`);
  return { total: row.total_pct, detail: parts.join(" + ") };
};

function FeePolicyNote({ policy }) {
  if (!policy?.KR || !policy?.US) return null;
  const krBuy = feeRow(policy, "KR", "buy");
  const krSell = feeRow(policy, "KR", "sell");
  const usBuy = feeRow(policy, "US", "buy");
  const usSell = feeRow(policy, "US", "sell");
  return (
    <div className="paper-fee-note">
      <strong>거래 비용 반영</strong>
      {krBuy && (
        <span>
          🇰🇷 매수 <b>{krBuy.total}%</b> · 매도 <b>{krSell?.total}%</b>
          <em> ({krSell?.detail})</em>
        </span>
      )}
      {usBuy && (
        <span>
          🇺🇸 매수 <b>{usBuy.total}%</b> · 매도 <b>{usSell?.total}%</b>
          <em> ({usBuy.detail})</em>
        </span>
      )}
      <em className="paper-fee-rule">
        매도 판단(손절/익절)은 주가 등락이 아니라 수수료·세금·환차를 모두 뺀 순수익률 기준입니다.
      </em>
    </div>
  );
}

// 서버 시각(ISO)을 KST "11-24 18:57"로 — 브라우저 시간대와 무관하게 같은 표기를 쓴다.
const kstDateTime = (iso) => {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const kst = new Date(date.getTime() + 9 * 60 * 60 * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(kst.getUTCMonth() + 1)}-${pad(kst.getUTCDate())} ${pad(kst.getUTCHours())}:${pad(kst.getUTCMinutes())}`;
};

// 🏆 지인 수익률 랭킹 — 순위·닉네임·합산/미장/국장 수익률. PIN·토큰은 서버가 보내지 않는다.
function LeaderboardCard({ rows, me }) {
  if (!rows?.length) return null;
  return (
    <section className="panel paper-rank">
      <div className="score-head">
        <div>
          <h3 className="score-title">🏆 지인 수익률 랭킹</h3>
          <p className="score-sub">미국·한국 두 계좌를 합친 수익률 순위입니다(평가 시점 환율 기준).</p>
        </div>
      </div>
      <div className="table-scroll">
        <table className="paper-table paper-rank-table">
          <thead>
            <tr>
              <th>순위</th>
              <th>닉네임</th>
              <th>합산</th>
              <th>🇺🇸 미국</th>
              <th>🇰🇷 한국</th>
              <th>총자산</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.user_id} className={row.user_id === me ? "me" : ""}>
                <td className="rank-cell">
                  {row.rank === 1 ? "🥇" : row.rank === 2 ? "🥈" : row.rank === 3 ? "🥉" : row.rank}
                </td>
                <td>
                  <b>{row.nickname}</b>
                  {row.is_admin && <span className="mini-tag">관리자</span>}
                  {row.user_id === me && <span className="mini-tag">나</span>}
                </td>
                <td>
                  <span className={`ret ${retClass(row.total_return_pct)}`}>
                    {pct(row.total_return_pct)}
                  </span>
                </td>
                <td>
                  <span className={`ret ${retClass(row.us?.total_return_pct)}`}>
                    {pct(row.us?.total_return_pct)}
                  </span>
                </td>
                <td>
                  <span className={`ret ${retClass(row.kr?.total_return_pct)}`}>
                    {pct(row.kr?.total_return_pct)}
                  </span>
                </td>
                <td>{won(row.total_value_krw)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// 닉네임 + 4자리 PIN 입력 시트(처음 입장·계정 전환·로그아웃).
function AuthSheet({ session, onClose, onSubmit, busy, error, nickname, pin, setNickname,
                     setPin, onLogout }) {
  return (
    <div className="paper-auth-backdrop" role="dialog" aria-modal="true">
      <form className="panel paper-auth-sheet" onSubmit={onSubmit}>
        <div className="score-head">
          <div>
            <h3 className="score-title">👥 계정 전환 · 로그인</h3>
            <p className="score-sub">
              {session
                ? `지금 ${session.nickname}님으로 접속 중입니다. 다른 닉네임은 PIN으로 들어오세요.`
                : "닉네임과 4자리 PIN으로 입장하세요. 처음 보는 닉네임이면 계좌가 새로 만들어집니다."}
            </p>
          </div>
          {session && (
            <button type="button" className="ghost" onClick={onClose}>
              닫기
            </button>
          )}
        </div>
        <div className="paper-form paper-auth-form">
          <label>
            닉네임
            <input
              value={nickname}
              onChange={(event) => setNickname(event.target.value.slice(0, 20))}
              placeholder="예: 민수"
              autoFocus
            />
          </label>
          <label>
            4자리 PIN
            <input
              type="password"
              inputMode="numeric"
              maxLength={4}
              value={pin}
              onChange={(event) => setPin(event.target.value.replace(/[^0-9]/g, "").slice(0, 4))}
              placeholder="0000"
            />
          </label>
        </div>
        {error && <div className="error">⚠️ {error}</div>}
        <div className="paper-form-actions">
          <button type="submit" className="run-btn" disabled={busy}>
            {busy ? "확인 중..." : "입장 / 로그인"}
          </button>
          {session && (
            <button type="button" className="ghost danger" onClick={onLogout}>
              로그아웃
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

export default function PaperTradingView() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [seed, setSeed] = useState("");
  const [market, setMarket] = useState("US");
  const [strategy, setStrategy] = useState("balanced");
  const [auto, setAuto] = useState(true);
  const [runResult, setRunResult] = useState(null);
  const [realOpen, setRealOpen] = useState(false);
  const [broker, setBroker] = useState(null);
  const [session, setSession] = useState(readSession);
  const [authOpen, setAuthOpen] = useState(() => !readSession());
  const [authNick, setAuthNick] = useState("");
  const [authPin, setAuthPin] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [authError, setAuthError] = useState("");
  const [ranking, setRanking] = useState(null);

  const apply = useCallback((body) => {
    setData(body);
    setSeed(String(Math.round(body?.account?.initial_cash_krw ?? 0)));
    setStrategy(body?.account?.strategy ?? "balanced");
    setAuto(Boolean(body?.account?.auto_enabled));
    if (body?.broker) setBroker(body.broker);
  }, []);

  const load = useCallback(
    async (silent = false, scope = market, active = session) => {
      if (!active?.token) return;
      if (!silent) setLoading(true);
      try {
        const res = await fetch(`${API_BASE}/api/paper/status?market=${scope}`, {
          headers: authHeaders(active),
        });
        if (res.status === 401) {
          window.localStorage.removeItem(SESSION_KEY);
          setSession(null);
          setData(null);
          setAuthOpen(true);
          throw new Error("로그인이 만료됐어요. 닉네임과 PIN으로 다시 로그인해 주세요.");
        }
        if (!res.ok) throw new Error(`계좌를 불러오지 못했습니다 (${res.status})`);
        apply(await res.json());
        setError("");
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    },
    [apply, market, session]
  );

  const loadRanking = useCallback(async (active = session) => {
    try {
      const res = await fetch(`${API_BASE}/api/paper/leaderboard`, {
        headers: authHeaders(active),
      });
      if (!res.ok) return;
      setRanking(await res.json());
    } catch {
      // 랭킹은 부가 정보 — 실패해도 계좌 화면은 그대로 보여 준다.
    }
  }, [session]);

  useEffect(() => {
    if (session?.token) {
      load(false, market);
      loadRanking();
    } else {
      setLoading(false);
    }
    setRunResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, session]);

  const submitAuth = async (event) => {
    event?.preventDefault?.();
    const nickname = authNick.trim();
    if (!nickname || !/^\d{4}$/.test(authPin)) {
      setAuthError("닉네임과 4자리 PIN을 입력해 주세요.");
      return;
    }
    setAuthBusy(true);
    setAuthError("");
    try {
      const res = await fetch(`${API_BASE}/api/paper/auth`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname, pin: authPin }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(AUTH_ERROR(res.status, body.detail));
      const next = {
        token: body.token,
        userId: body.user?.id,
        nickname: body.user?.nickname || nickname,
        isAdmin: Boolean(body.user?.is_admin),
      };
      window.localStorage.setItem(SESSION_KEY, JSON.stringify(next));
      setSession(next);
      setAuthOpen(false);
      setAuthPin("");
      setData(null);
      setRanking(null);
      if (body.created) setError("");
    } catch (err) {
      setAuthError(err.message);
    } finally {
      setAuthBusy(false);
    }
  };

  const logout = () => {
    window.localStorage.removeItem(SESSION_KEY);
    setSession(null);
    setData(null);
    setRanking(null);
    setBroker(null);
    setError("");
    setAuthNick("");
    setAuthPin("");
    setAuthError("");
    setAuthOpen(true);
  };

  const saveSettings = async (reset = false) => {
    const amount = Number(String(seed).replace(/[^0-9.]/g, ""));
    if (!amount) {
      setError("시드머니를 입력해 주세요.");
      return;
    }
    if (reset && !window.confirm(`${won(amount)}으로 ${market === "KR" ? "한국" : "미국"} 계좌를 초기화할까요? 보유 종목·체결 내역이 모두 지워집니다.`)) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/paper/configure`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(session) },
        body: JSON.stringify({
          market,
          initial_cash_krw: amount,
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
      loadRanking();
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
        headers: { "Content-Type": "application/json", ...authHeaders(session) },
        body: JSON.stringify({ market, force: true }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(`AI 매매 실행 실패 (${res.status})`);
      setRunResult(body.result);
      apply(body.status);
      loadRanking();
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
  const feePolicy = data?.fee_policy || null;
  const comparison = data?.comparison || [];

  const chart = useMemo(
    () =>
      (data?.equity_history || []).map((row) => ({
        date: String(row.date || "").slice(5),
        total: Math.round(row.total_value_krw || 0),
        ret: row.return_pct,
      })),
    [data]
  );

  if (!session?.token) {
    return (
      <div className="paper">
        <section className="panel paper-auth-hero">
          <div className="score-head">
            <div>
              <h2 className="score-title">👥 지인 모의투자 · 닉네임 + 4자리 PIN</h2>
              <p className="score-sub">
                각자 닉네임과 PIN으로 자기 계좌(미국·한국 각 100만 원)를 따로 굴리고, 수익률을
                나란히 겨룹니다. 처음 보는 닉네임이면 계좌가 새로 만들어집니다.
              </p>
            </div>
          </div>
          <form className="paper-form paper-auth-form" onSubmit={submitAuth}>
            <label>
              닉네임
              <input
                value={authNick}
                onChange={(event) => setAuthNick(event.target.value.slice(0, 20))}
                placeholder="예: 민수"
                autoFocus
              />
            </label>
            <label>
              4자리 PIN
              <input
                type="password"
                inputMode="numeric"
                maxLength={4}
                value={authPin}
                onChange={(event) => setAuthPin(event.target.value.replace(/[^0-9]/g, "").slice(0, 4))}
                placeholder="0000"
              />
            </label>
          </form>
          {authError && <div className="error">⚠️ {authError}</div>}
          <div className="paper-form-actions">
            <button type="button" className="run-btn" onClick={submitAuth} disabled={authBusy}>
              {authBusy ? "확인 중..." : "입장 / 로그인"}
            </button>
          </div>
          <p className="paper-disclaimer">
            ※ 닉네임과 PIN은 지인끼리만 정해 쓰세요. 관리자(현우)는 기존 계좌를 그대로 이어받습니다 —
            처음 한 번 PIN을 정하면 이후 그 PIN으로 들어옵니다.
          </p>
        </section>
      </div>
    );
  }

  if (loading && !data) {
    return <div className="list-empty">🤖 AI 모의투자 계좌 불러오는 중...</div>;
  }

  return (
    <div className="paper">
      <div className="paper-userbar">
        <span className="paper-user-chip">
          👤 <b>{session.nickname}</b>
          {session.isAdmin && <em>관리자</em>}
        </span>
        <button type="button" className="ghost" onClick={() => setAuthOpen(true)}>
          계정 전환 / 로그인
        </button>
      </div>

      <section className={`paper-compare ${session.isAdmin ? "paper-compare-three" : ""}`}>
        {comparison.map((row) => (
            <button
              key={row.market}
              type="button"
              className={`paper-compare-card ${row.market === market ? "active" : ""}`}
              onClick={() => setMarket(row.market)}
            >
              <span className="paper-compare-label">
                {row.label || row.market} 계좌
                {row.auto_enabled === false && <em className="sub">자동 꺼짐</em>}
              </span>
              <strong>{won(row.total_value_krw)}</strong>
              <span className={`ret ${retClass(row.total_return_pct)}`}>
                {pct(row.total_return_pct)}
              </span>
              <em className={`sub ${retClass(row.total_pnl_krw)}`}>
                손익 {won(row.total_pnl_krw)}
              </em>
              <em className="sub">
                현금 {won(row.cash_krw)} · 보유 {row.positions_count ?? 0}종목
              </em>
            </button>
          ))}
        {session.isAdmin && (
          <RealAccountCard broker={broker} onOpen={() => setRealOpen(true)} />
        )}
      </section>

      <LeaderboardCard rows={ranking?.rows} me={session.userId} />


      <section className="panel paper-hero">
        <div className="score-head">
          <div>
            <h2 className="score-title">🤖 AI 모의투자 · {market === "KR" ? "🇰🇷 한국" : "🇺🇸 미국"} 계좌</h2>
            <p className="score-sub">
              시드 {won(account?.initial_cash_krw)} · {account?.strategy_label || "-"} · 1 USD = {won(account?.fx_rate)}{" "}
              <span className="mini-tag">{account?.fx_source || "-"}</span>
            </p>
            <div className="seg paper-account-seg">
              {MARKETS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className={market === item.value ? "active" : ""}
                  onClick={() => setMarket(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>
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

        <FeePolicyNote policy={feePolicy} />
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
                  <em className="sub">수수료 {won(trade.fee_krw)}</em>
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
                  <em className="sub">수수료 {won(trade.fee_krw)}</em>
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
            <h3 className="score-title">⚙️ 계좌 설정 · {market === "KR" ? "🇰🇷 한국" : "🇺🇸 미국"}</h3>
            <p className="score-sub">
              계좌마다 시장이 따로입니다(미국 계좌는 미국 종목만). 시드머니는 언제든 바꿀 수 있고,
              초기화하면 이 계좌만 새 시드로 다시 시작합니다.
            </p>
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
        <FeePolicyNote policy={feePolicy} />
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
            <p className="score-sub">
              평가액·손익은 현재 환율로 원화 환산한 값입니다(환차 포함). ‘매도 시 순손익’은 지금 전량 매도할 때
              수수료·세금을 뺀 실제 손익입니다.
            </p>
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
                  <th>매도 시 순손익</th>
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
                      {row.currency === "USD" && row.fx_change_pct !== null && row.fx_change_pct !== undefined && (
                        <em className={`sub ${retClass(row.fx_change_pct)}`}>
                          환율 {pct(row.fx_change_pct)}
                        </em>
                      )}
                    </td>
                    <td>
                      <span className={`ret ${retClass(row.net_pnl_pct)}`}>{pct(row.net_pnl_pct)}</span>
                      <em className={`sub ${retClass(row.net_pnl_krw)}`}>{won(row.net_pnl_krw)}</em>
                      <em className="sub">수수료 {won(row.sell_fee_krw)}</em>
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
                  <th>수수료·세금</th>
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
                    <td className={trade.fee_krw ? "" : "sub"}>
                      {won(trade.fee_krw)}
                      <em className="sub">{trade.side === "BUY" ? "지출" : "입금"} {won(trade.settle_krw)}</em>
                    </td>
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
        ※ 모의투자입니다. 실제 주문이 나가지 않으며, 체결은 서버가 조회한 현재가로 기록됩니다. 매수·매도에는
        수수료·증권거래세·환전 스프레드가 붙어 현금과 손익에 그대로 반영됩니다. 과거 수익률이 미래 수익을
        보장하지 않습니다.
      </p>

      {realOpen && session.isAdmin && (
        <RealAccountModal
          status={broker}
          onClose={() => setRealOpen(false)}
          onChanged={(next) => setBroker(next)}
        />
      )}

      {authOpen && (
        <AuthSheet
          session={session}
          onClose={() => setAuthOpen(false)}
          onSubmit={submitAuth}
          busy={authBusy}
          error={authError}
          nickname={authNick}
          pin={authPin}
          setNickname={setAuthNick}
          setPin={setAuthPin}
          onLogout={logout}
        />
      )}
    </div>
  );
}

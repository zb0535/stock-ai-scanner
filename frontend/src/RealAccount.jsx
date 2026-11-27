import { useCallback, useEffect, useState } from "react";

const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

// 모의투자 세션 토큰 — 브로커 API도 로그인 토큰이 있어야 통과한다(토큰 없으면 서버가 401).
const authHeaders = (session) =>
  session?.token ? { "X-Paper-User-Token": session.token } : {};

const won = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `₩${Math.round(Number(value)).toLocaleString("ko-KR")}`;
};

/** 마스킹 힌트만 보여 준다 — 서버는 끝 4자리 외에는 절대 보내지 않는다. */
const hintText = (row) => row?.credential_hint_label || "";

const stateLine = (status) => {
  if (!status) return "상태 불러오는 중…";
  const registered = (status.adapters || []).filter((row) => row.registered);
  const parts = [];
  parts.push(
    registered.length
      ? `${registered.map((row) => row.label || row.name).join(", ")} 키 등록됨`
      : "키 미등록"
  );
  parts.push(status.real_trading_enabled ? "킬 스위치 켜짐" : "킬 스위치 꺼짐");
  if (status.dry_run) parts.push("드라이런");
  return parts.join(" · ");
};

/**
 * 🔌 실제 계좌 (테스트) 카드 + 경고/안내/키 등록 모달.
 *
 * 키는 **이 브라우저에 저장하지 않는다**(localStorage·쿠키 없음). 입력값은 등록 요청 직후 비우고,
 * 서버는 암호화해 보관한 뒤 마스킹 힌트만 돌려준다. 등록해도 킬 스위치·드라이런·주문 코드가
 * 그대로라 실제 주문은 나가지 않는다.
 */
export function RealAccountCard({ broker, onOpen }) {
  const registered = (broker?.adapters || []).filter((row) => row.registered);
  const wired = (broker?.adapters || []).some((row) => row.registered && row.implemented);
  return (
    <button type="button" className="paper-compare-card real-account-card" onClick={onOpen}>
      <span className="paper-compare-label">
        🔌 실제 계좌 <em className="sub">(테스트)</em>
      </span>
      <strong>{registered.length ? "키 등록됨" : "미연결"}</strong>
      <span className={`ret ${broker?.real_trading_enabled && wired ? "down" : "flat"}`}>
        {broker?.real_trading_enabled ? "킬 스위치 켜짐" : "실주문 차단"}
      </span>
      <em className="sub">{registered.length ? hintText(registered[0]) : "모의투자로만 돌고 있어요"}</em>
      <em className="sub">
        {broker?.dry_run ? "드라이런" : "드라이런 꺼짐"} · 등록 {registered.length}/
        {(broker?.adapters || []).filter((row) => row.name !== "paper").length}
      </em>
      <em className="sub real-account-cta">실제 계좌로 해보기 →</em>
    </button>
  );
}

export function RealAccountModal({ status, session, onClose, onChanged }) {
  const [adapter, setAdapter] = useState("kis");
  const [form, setForm] = useState({ app_key: "", app_secret: "", account_no: "", token: "" });
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [live, setLive] = useState(status || null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/broker/status`, { headers: authHeaders(session) });
      if (!res.ok) throw new Error(`상태를 불러오지 못했습니다 (${res.status})`);
      const body = await res.json();
      setLive(body);
      onChanged?.(body);
    } catch (err) {
      setError(err.message);
    }
  }, [onChanged, session]);

  useEffect(() => {
    load();
  }, [load]);

  const guides = live?.guides || [];
  const guide = guides.find((row) => row.adapter === adapter) || guides[0];
  const registeredRow = live?.credentials?.adapters?.[adapter] || null;
  const credentials = live?.credentials || {};
  const encryption = credentials.encryption || {};
  const auth = credentials.auth || {};
  const safety = live?.safety || {};
  const needsToken = auth.mode === "token";

  const clearForm = () => setForm({ app_key: "", app_secret: "", account_no: "", token: "" });

  const submit = async () => {
    if (!agree) {
      setError("경고를 확인하고 동의에 체크해 주세요.");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const headers = { "Content-Type": "application/json", ...authHeaders(session) };
      if (needsToken && form.token.trim()) headers["X-Broker-Token"] = form.token.trim();
      const res = await fetch(`${API_BASE}/api/broker/credentials`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          adapter,
          app_key: form.app_key,
          app_secret: form.app_secret,
          account_no: form.account_no,
          confirm_risk: true,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = body.detail || {};
        throw new Error(detail.message || `등록 실패 (${res.status})`);
      }
      setLive(body.broker);
      onChanged?.(body.broker);
      setNotice(
        `${guide?.title || adapter} 키를 서버에 암호화해 저장했습니다. ` +
          "실제 주문은 아직 나가지 않아요(킬 스위치 꺼짐·드라이런·주문 코드 미연결)."
      );
      setAgree(false);
    } catch (err) {
      setError(err.message);
    } finally {
      clearForm();
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`${guide?.title || adapter} 키를 삭제할까요? 저장된 암호문도 함께 지웁니다.`)) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const headers = { ...authHeaders(session) };
      if (needsToken && form.token.trim()) headers["X-Broker-Token"] = form.token.trim();
      const res = await fetch(`${API_BASE}/api/broker/credentials/${adapter}`, {
        method: "DELETE",
        headers,
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail?.message || `삭제 실패 (${res.status})`);
      setLive(body.broker);
      onChanged?.(body.broker);
      setNotice("키를 삭제했습니다(연동 해제).");
    } catch (err) {
      setError(err.message);
    } finally {
      clearForm();
      setBusy(false);
    }
  };

  return (
    <div className="sheet-backdrop" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="real-account-sheet" onClick={(event) => event.stopPropagation()}>
        <div className="sheet-head">
          <div>
            <h3 className="score-title">🔌 실제 계좌로 해보기 · 경고와 등록 안내</h3>
            <p className="score-sub">{live?.summary || stateLine(live)}</p>
          </div>
          <button type="button" className="ghost" onClick={onClose}>
            닫기
          </button>
        </div>

        <section className="real-warning">
          <strong>⚠️ 실제 돈이 나갈 수 있는 연동입니다</strong>
          <ul>
            <li>실제 계좌로 주문이 나가면 <b>실제 자금이 손실</b>될 수 있습니다. AI 판단은 틀릴 수 있고, 과거 수익률이 미래를 보장하지 않습니다.</li>
            <li>지금은 <b>키를 등록해도 실제 주문이 나가지 않습니다</b> — 서버의 주문 실행 코드가 증권사 API에 아직 연결되지 않았고(`implemented=false`), 킬 스위치·드라이런이 기본으로 막습니다.</li>
            <li>실주문을 켜려면 Render 환경변수에서 <code>REAL_TRADING_ENABLED=1</code>, <code>REAL_DRY_RUN=0</code>을 직접 바꿔야 합니다.</li>
          </ul>
        </section>

        <section className="real-safety">
          <h4>현재 서버 안전장치</h4>
          <div className="real-safety-grid">
            <div>
              <span>킬 스위치 {safety.kill_switch_env || "REAL_TRADING_ENABLED"}</span>
              <b className={safety.real_trading_enabled ? "down" : "up"}>
                {safety.real_trading_enabled ? "켜짐(주문 가능)" : "꺼짐(전부 차단)"}
              </b>
            </div>
            <div>
              <span>드라이런 {safety.dry_run_env || "REAL_DRY_RUN"}</span>
              <b className={safety.dry_run ? "up" : "down"}>
                {safety.dry_run ? "켜짐(주문 안 보냄)" : "꺼짐(실제로 보냄)"}
              </b>
            </div>
            <div>
              <span>1회 매수 상한</span>
              <b>{won(safety.order_max_krw)}</b>
            </div>
            <div>
              <span>1일 누적 상한</span>
              <b>
                {won(safety.daily_max_krw)} <em className="sub">(오늘 {won(safety.daily_used_krw)})</em>
              </b>
            </div>
            <div>
              <span>중복 주문 차단</span>
              <b className="up">켜짐</b>
            </div>
            <div>
              <span>주문 코드 연결</span>
              <b className="down">미연결</b>
            </div>
          </div>
        </section>

        <section className="real-security">
          <h4>🔐 보안 수칙 — 이건 꼭 지켜 주세요</h4>
          <ul>
            <li>App Key·App Secret·계좌번호는 <b>서버(Render)에만</b> 보관합니다. 이 화면은 입력값을 브라우저에 저장하지 않고, 보내는 즉시 비웁니다.</li>
            <li>서버는 값을 <b>암호화(Fernet)해서</b> DB에 넣고, 화면에는 <b>끝 4자리 마스킹</b>만 돌려줍니다. 다시 보여 주는 기능은 없습니다.</li>
            <li>키를 <b>카톡·메일·GitHub·스크린샷·채팅 AI</b>에 붙여 넣지 마세요. 노출이 의심되면 증권사에서 즉시 재발급하세요.</li>
            <li>주문·출금 권한 중 <b>출금 권한은 켜지 마세요</b>. 조회 → 주문 순서로 조금씩 열어 주세요.</li>
            <li>서버 암호화 키 재료({encryption.key_source || "미설정"})는 DB 밖에 있습니다. 없으면 등록이 거부됩니다(평문 저장 없음).</li>
          </ul>
          {needsToken ? (
            <p className="real-auth-note">
              이 서버는 등록 잠금 토큰이 켜져 있습니다. Render의 <code>{auth.header || "X-Broker-Token"}</code>
              {" "}값을 아래에 입력해야 등록·삭제할 수 있어요(저장하지 않습니다).
            </p>
          ) : (
            <p className="real-auth-note">{auth.message}</p>
          )}
        </section>

        <section className="real-register">
          <div className="seg real-broker-seg">
            {guides.map((row) => (
              <button
                key={row.adapter}
                type="button"
                className={adapter === row.adapter ? "active" : ""}
                onClick={() => {
                  setAdapter(row.adapter);
                  clearForm();
                  setError("");
                  setNotice("");
                  setAgree(false);
                }}
              >
                {row.title}
                {!row.registerable && <em className="sub"> (출시 대기)</em>}
              </button>
            ))}
          </div>

          {guide && (
            <div className="real-guide">
              <p className="real-guide-summary">{guide.summary}</p>
              {guide.url && (
                <p className="score-sub">
                  발급 페이지:{" "}
                  <a href={guide.url} target="_blank" rel="noreferrer">
                    {guide.url}
                  </a>
                </p>
              )}
              <ol>
                {(guide.steps || []).map((step, index) => (
                  <li key={index}>{step}</li>
                ))}
              </ol>
              {(guide.notes || []).length > 0 && (
                <ul className="real-guide-notes">
                  {guide.notes.map((note, index) => (
                    <li key={index}>· {note}</li>
                  ))}
                </ul>
              )}
              {guide.env?.length > 0 && (
                <p className="score-sub">
                  서버 환경변수 이름: <code>{guide.env.join(", ")}</code> — 직접 넣을 필요는 없어요(여기서 등록).
                </p>
              )}
            </div>
          )}

          {!guide?.registerable ? (
            <p className="real-waiting">
              이 증권사는 아직 등록할 수 없습니다. 출시되면 이 화면에서 바로 열어 드릴게요.
            </p>
          ) : registeredRow?.registered ? (
            <div className="real-registered">
              <div>
                <strong>✅ 등록됨</strong>
                <p className="score-sub">
                  {registeredRow.hint_label} · {registeredRow.updated_at || "-"}
                </p>
              </div>
              <button type="button" className="ghost danger" onClick={remove} disabled={busy}>
                🔓 연동 해제(키 삭제)
              </button>
            </div>
          ) : (
            <div className="real-form">
              {(guide?.fields || []).map((field) => (
                <label key={field.field}>
                  {field.label}
                  <input
                    type={field.secret ? "password" : "text"}
                    inputMode={field.field === "account_no" ? "numeric" : undefined}
                    autoComplete="new-password"
                    spellCheck={false}
                    value={form[field.field] || ""}
                    onChange={(event) =>
                      setForm((prev) => ({ ...prev, [field.field]: event.target.value }))
                    }
                    placeholder={field.secret ? "발급받은 값을 붙여넣기" : "예: 50123456-01"}
                  />
                  <em>{field.env}</em>
                </label>
              ))}
              {needsToken && (
                <label>
                  서버 등록 토큰 (선택)
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={form.token}
                    onChange={(event) => setForm((prev) => ({ ...prev, token: event.target.value }))}
                  />
                  <em>저장하지 않습니다</em>
                </label>
              )}
            </div>
          )}

          {guide?.registerable && !registeredRow?.registered && (
            <>
              <label className="real-agree">
                <input type="checkbox" checked={agree} onChange={(event) => setAgree(event.target.checked)} />
                실제 자금 손실 위험과 보안 수칙을 확인했고, 키를 서버에만 암호화 저장하는 데 동의합니다.
              </label>
              <div className="paper-form-actions">
                <button type="button" className="run-btn" onClick={submit} disabled={busy || !agree}>
                  {busy ? "저장 중…" : "🔐 키 등록 (서버 암호화 저장)"}
                </button>
              </div>
            </>
          )}
        </section>

        {notice && <p className="real-notice">✅ {notice}</p>}
        {error && <p className="error">⚠️ {error}</p>}
        <p className="paper-disclaimer">
          ※ 등록은 "보관"일 뿐입니다. 실제 주문은 서버 안전장치(킬 스위치·1회/1일 한도·중복 차단·드라이런)와
          증권사 주문 코드를 모두 통과해야 나갑니다. 지금은 주문 코드가 연결되지 않아 어떤 주문도 나가지 않습니다.
        </p>
      </div>
    </div>
  );
}

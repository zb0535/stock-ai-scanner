"""메모리 보호 계층 — Render 무료 512MB에서 프로세스가 한도에 닿지 않게 조절한다.

왜 필요한가
-----------
Render 무료 플랜은 인스턴스 메모리가 512MB를 넘으면 프로세스를 강제로 재시작한다
(`503 hibernate-wake-error` / `502`). 이 앱은 기동 직후 다음 작업이 겹친다.

- S&P 500(503종목) 프리 스캔 — 8초 뒤 시작
- 코스피 200(201종목) 프리 스캔 — 60초 뒤 시작
- 발굴 후보 재무 예열 — 스캔이 끝날 때마다 백그라운드로 시작(SEC companyfacts 파싱)
- 사용자 스크리닝 — 15종목 재무 파이프라인 + Gemini
- AI 모의투자 상태 스냅샷 — 60초마다

겹치면 순간 피크가 한도를 넘는다. 실제로 측정한 값(2026-11-27, 로컬 3.12/pandas 기준):

| 작업 | 메모리 |
|---|---|
| 라이브러리 임포트 후 기준선(`import api`) | 138MB |
| `yf.download` 100종목(6개월 일봉) | +12MB (피크 120MB) |
| SEC companyfacts 1건 파싱(AAPL 3.9MB JSON) | +17MB |
| SEC companyfacts 3건 동시 파싱(JPM·BAC·XOM) | +123MB (피크 170MB) |
| companyfacts 원본 캐시 3건 상주 | 대략 +60~120MB |

즉 **스캔보다 SEC 원본 JSON이 주범**이다. 이 모듈은 (1) 실제 RSS와 컨테이너 한도를 읽어
압력을 계산하고, (2) 압력이 높을 때 캐시를 정리·gc하고, (3) 무거운 작업(스캔 청크)을
한 번에 하나만 돌게 하며, (4) 프리워밍 단계를 직렬화한다.

설계 원칙
---------
- **한도를 못 읽으면 아무것도 막지 않는다.** 로컬·맥·CI에서는 `MEM_GUARD=auto`가 비활성으로
  판정돼 기존 동작·테스트가 그대로다. Render(512MB cgroup)에서만 켜진다.
- **락이 아니라 순서를 잡는다.** 슬롯·단계 락은 모두 시간 제한(timeout)이 있어, 오래 기다리면
  그냥 진행한다(서비스가 멈추는 것보다 메모리를 조금 더 쓰는 편이 낫다).
- **죽지 않게 건너뛴다.** 여유가 없으면 스캔 청크·재무 예열 종목을 건너뛰고, 이미 채운
  스냅샷·캐시는 그대로 서빙한다(사용자는 조금 오래된 데이터를 받을 뿐 502를 받지 않는다).

환경변수
--------
- `MEM_GUARD`         `auto`(기본, 2GB 이하 cgroup일 때만 켬) / `1`(강제 켬) / `0`(끔)
- `MEM_LIMIT_MB`      컨테이너 한도 수동 지정(cgroup을 못 읽는 환경용)
- `MEM_SOFT_LIMIT_MB` 캐시 정리를 시작하는 선(기본 한도의 75%)
- `MEM_HARD_LIMIT_MB` 새 무거운 작업을 거부하는 선(기본 한도의 88%)
- `MEM_HEAVY_TIMEOUT` 무거운 슬롯을 기다리는 최대 초(기본 20)
- `MEM_PHASE_TIMEOUT` 프리워밍 단계 락을 기다리는 최대 초(기본 300)
"""
import contextlib
import gc
import logging
import os
import resource
import threading
import time

logger = logging.getLogger("memory_guard")

PROC_STATUS = "/proc/self/status"
# (사용량 파일, 한도 파일) — cgroup v2 → v1 순서로 본다.
CGROUP_FILES = (
    ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
    ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
     "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
DEFAULT_LIMIT_MB = 512.0       # 한도를 못 읽고 MEM_GUARD=1로 강제했을 때의 가정
SOFT_RATIO = 0.75
HARD_RATIO = 0.88
AUTO_DETECT_MAX_MB = 2048.0    # 이보다 큰 한도면(=개발 PC·CI) 자동 모드에서는 끈다
_UNLIMITED = 1e7               # cgroup이 'max'/아주 큰 값을 줄 때(바이트 기준 MB로 환산)

_HEAVY_TIMEOUT = float(os.environ.get("MEM_HEAVY_TIMEOUT", "20"))
_PHASE_TIMEOUT = float(os.environ.get("MEM_PHASE_TIMEOUT", "300"))

# name -> {"trim": fn() -> int, "size": fn() -> dict}
_CACHES: dict[str, dict] = {}
_CACHES_LOCK = threading.Lock()

# 무거운 작업 슬롯(기본 1칸)과 프리워밍 단계 락. 둘 다 재진입하지 않는다.
_HEAVY_LOCK = threading.Lock()
_PHASE_LOCK = threading.Lock()
_STATE: dict = {"heavy_owner": "", "phase_owner": ""}
_STATE_LOCK = threading.Lock()


# --- 읽기 도우미 -------------------------------------------------------------

def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:  # 파일이 없는 환경(맥·윈도) — 조용히 넘어간다
        return None


def _to_mb(raw: str | None) -> float | None:
    """cgroup 값(바이트 문자열)을 MB로. `max`·0·비정상 값은 None(한도 없음)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or text == "max":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0:
        return None
    mb = value / (1024.0 * 1024.0)
    if mb >= _UNLIMITED:  # v1은 '한도 없음'을 2^63-1로 준다
        return None
    return mb


def _env_float(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def rss_mb() -> float | None:
    """현재 프로세스 RSS(MB). `/proc`이 없으면 cgroup 사용량, 그것도 없으면 None."""
    text = _read_text(PROC_STATUS)
    if text:
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return float(parts[1]) / 1024.0
                    except ValueError:
                        break
                break
    for usage_path, _limit_path in CGROUP_FILES:
        mb = _to_mb(_read_text(usage_path))
        if mb:
            return mb
    return None


def peak_rss_mb() -> float | None:
    """프로세스 최고 RSS(MB) — 프로세스 수명 동안의 최대값(줄지 않는다)."""
    try:
        return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0
    except (ValueError, OSError):
        return None


def limit_mb() -> float | None:
    """컨테이너 메모리 한도(MB). `MEM_LIMIT_MB` → cgroup v2 → cgroup v1 순서."""
    env = _env_float("MEM_LIMIT_MB")
    if env:
        return env
    for _usage_path, limit_path in CGROUP_FILES:
        mb = _to_mb(_read_text(limit_path))
        if mb:
            return mb
    return None


def enabled() -> bool:
    """메모리 보호를 켤지 — `auto`(기본)는 2GB 이하 한도가 감지될 때만 켠다."""
    flag = (os.environ.get("MEM_GUARD") or "auto").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    limit = limit_mb()
    return bool(limit is not None and limit <= AUTO_DETECT_MAX_MB)


def _effective_limit() -> float:
    limit = limit_mb()
    return limit if limit else DEFAULT_LIMIT_MB


def soft_limit_mb() -> float:
    """캐시 정리를 시작하는 선(기본 한도의 75%)."""
    env = _env_float("MEM_SOFT_LIMIT_MB")
    if env:
        return env
    return round(_effective_limit() * SOFT_RATIO, 1)


def hard_limit_mb() -> float:
    """새 무거운 작업을 거부하는 선(기본 한도의 88%)."""
    env = _env_float("MEM_HARD_LIMIT_MB")
    if env:
        return env
    return round(_effective_limit() * HARD_RATIO, 1)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


_TRIM_FN = None


def trim_heap() -> bool:
    """glibc `malloc_trim(0)` — 파이썬이 돌려준 힙 페이지를 OS에 반납한다.

    `gc.collect()`는 파이썬 객체를 회수하지만, glibc는 한 번 잡은 아레나를 프로세스가
    살아 있는 동안 계속 물고 있는다(RSS에 그대로 남아 512MB 한도를 압박한다). 스캔이
    끝날 때마다 한 번 부르면 RSS가 실제로 내려간다. Linux/glibc가 아니면 조용히 False.
    """
    global _TRIM_FN
    if _TRIM_FN is None:
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            fn = libc.malloc_trim
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_size_t]
            _TRIM_FN = fn
        except (OSError, AttributeError, ImportError):
            _TRIM_FN = False
    if not _TRIM_FN:
        return False
    try:
        return bool(_TRIM_FN(0))
    except Exception:  # 반납 실패는 치명적이지 않다
        return False


# --- 캐시 등록·정리 ----------------------------------------------------------

def register_cache(name: str, trim=None, size=None) -> None:
    """모듈별 캐시 정리기를 등록한다(임포트 시 1회).

    `trim()`은 만료·초과 항목을 지우고 지운 개수를 돌려준다.
    `size()`는 진단용 크기 dict를 돌려준다.
    """
    with _CACHES_LOCK:
        _CACHES[name] = {"trim": trim, "size": size}


def trim_caches() -> dict:
    """등록된 캐시를 모두 정리한다. 반환: {이름: 지운 개수}."""
    with _CACHES_LOCK:
        specs = list(_CACHES.items())
    out: dict[str, int] = {}
    for name, spec in specs:
        trim = spec.get("trim")
        if not callable(trim):
            continue
        try:
            out[name] = int(trim() or 0)
        except Exception as exc:  # 정리 실패가 요청을 막지 않게
            logger.debug("캐시 정리 실패(%s): %s", name, exc)
            out[name] = 0
    return out


def cache_sizes() -> dict:
    """등록된 캐시의 현재 크기(진단용)."""
    with _CACHES_LOCK:
        specs = list(_CACHES.items())
    out: dict[str, dict] = {}
    for name, spec in specs:
        size = spec.get("size")
        if not callable(size):
            continue
        try:
            out[name] = size()
        except Exception:  # 진단이 본체를 방해하지 않게
            continue
    return out


def _over_soft(extra_mb: float = 0.0) -> bool:
    if not enabled():      # 가드가 꺼진 환경(로컬·큰 머신)에서는 캐시를 건드리지 않는다
        return False
    rss = rss_mb()
    if rss is None:
        return False
    return (rss + extra_mb) > soft_limit_mb()


def collect(reason: str = "") -> dict:
    """`gc.collect()` + (압력이 높으면) 캐시 정리 + 힙 반납. 반환: {gc, trimmed, rss_mb}."""
    collected = gc.collect()
    trimmed: dict[str, int] = {}
    if _over_soft():
        trimmed = trim_caches()
        if any(trimmed.values()):
            gc.collect()
    freed = trim_heap()
    if (trimmed and any(trimmed.values())) or reason:
        logger.info("메모리 정리(%s): gc=%d, 캐시=%s, 힙반납=%s, rss≈%sMB",
                    reason or "-", collected, trimmed or "-", freed, _round(rss_mb()))
    return {"gc": collected, "trimmed": trimmed, "heap_trimmed": freed,
            "rss_mb": _round(rss_mb())}


# --- 게이트 -----------------------------------------------------------------

def headroom(need_mb: float = 0.0, *, wait: float = 0.0) -> bool:
    """새 작업에 `need_mb`만큼의 여유가 있는지. 없으면 캐시를 정리하고 `wait`초 기다린다."""
    if not enabled():
        return True
    hard = hard_limit_mb()
    rss = rss_mb()
    if hard is None or rss is None:
        return True
    if rss + need_mb <= hard:
        return True

    trim_caches()
    gc.collect()
    trim_heap()
    rss = rss_mb()
    deadline = time.time() + max(0.0, wait)
    while rss is not None and rss + need_mb > hard and time.time() < deadline:
        time.sleep(min(0.5, max(0.05, deadline - time.time())))
        rss = rss_mb()
    if rss is None:
        return True
    ok = rss + need_mb <= hard
    if not ok:
        logger.warning("메모리 여유 부족(rss≈%.1fMB + 필요 %.0fMB > 한도 %.1fMB) → 이번 작업 건너뜀",
                       rss, need_mb, hard)
    return ok


@contextlib.contextmanager
def heavy_slot(name: str, need_mb: float = 0.0, timeout: float | None = None,
               wait: float = 0.0):
    """무거운 작업 슬롯(기본 1칸) — 스캔 청크가 겹쳐 메모리 피크가 두 배가 되지 않게 한다.

    슬롯을 얻은 **뒤** 메모리 여유를 확인하고, 그 결과를 `yield`한다.
    - `True`: 진행해도 된다
    - `False`: 여유가 없다 → 호출부는 이번 청크/작업을 건너뛰고 다음 주기에 다시 시도한다

    슬롯을 `timeout` 안에 못 얻으면(다른 스캔이 오래 도는 중) 배타성 없이 진행한다 —
    스캔을 영원히 미루는 것보다 낫다(피크는 청크 단위라 중첩이 짧다).
    """
    if not enabled():
        yield True
        return
    limit = _HEAVY_TIMEOUT if timeout is None else max(0.0, float(timeout))
    acquired = _HEAVY_LOCK.acquire(timeout=limit)
    if acquired:
        with _STATE_LOCK:
            _STATE["heavy_owner"] = name
    else:
        logger.info("무거운 슬롯 대기 %.0fs 초과(%s) → 배타성 없이 진행", limit, name)
    try:
        yield headroom(need_mb, wait=wait)
    finally:
        if acquired:
            with _STATE_LOCK:
                _STATE["heavy_owner"] = ""
            _HEAVY_LOCK.release()


@contextlib.contextmanager
def prewarm_phase(name: str, timeout: float | None = None):
    """프리워밍 단계 락 — S&P 500·코스피 200의 스캔/재무 예열이 겹치지 않게 직렬화한다.

    기동 직후 8초(S&P 500)·60초(코스피 200) 시차만으로는 스캔이 끝난 뒤 시작하는
    재무 예열(SEC companyfacts)이 다른 시장 스캔과 겹쳤다. 이 락 하나로 '무거운 예열은
    한 번에 하나'가 된다. `timeout` 안에 못 얻으면 그냥 진행한다(다음 주기에 다시 시도).
    """
    if not enabled():
        yield True
        return
    limit = _PHASE_TIMEOUT if timeout is None else max(0.0, float(timeout))
    acquired = _PHASE_LOCK.acquire(timeout=limit)
    if acquired:
        with _STATE_LOCK:
            _STATE["phase_owner"] = name
    else:
        logger.warning("프리워밍 단계 대기 %.0fs 초과(%s) → 겹쳐서 진행", limit, name)
    started = time.time()
    try:
        yield acquired
    finally:
        if acquired:
            with _STATE_LOCK:
                _STATE["phase_owner"] = ""
            _PHASE_LOCK.release()
            elapsed = time.time() - started
            if elapsed >= 30:
                logger.info("프리워밍 단계 %s %.0fs 소요", name, elapsed)


def status() -> dict:
    """`GET /`의 `memory` — 지금 프로세스가 한도에 얼마나 가까운지."""
    rss = rss_mb()
    limit = limit_mb()
    with _STATE_LOCK:
        heavy_owner = _STATE.get("heavy_owner") or None
        phase_owner = _STATE.get("phase_owner") or None
    return {
        "guard": enabled(),
        "rss_mb": _round(rss),
        "peak_rss_mb": _round(peak_rss_mb()),
        "limit_mb": _round(limit),
        "soft_mb": _round(soft_limit_mb()),
        "hard_mb": _round(hard_limit_mb()),
        "pressure_pct": (round(rss / limit * 100.0, 1) if rss is not None and limit else None),
        "heavy_busy": bool(heavy_owner),
        "heavy_owner": heavy_owner,
        "prewarm_phase": phase_owner,
        "caches": cache_sizes(),
    }


def reset_state() -> None:
    """테스트용 — 소유자 표시만 비운다(락·등록된 캐시 정리기는 그대로)."""
    with _STATE_LOCK:
        _STATE["heavy_owner"] = ""
        _STATE["phase_owner"] = ""

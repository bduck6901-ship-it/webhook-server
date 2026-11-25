"""
BTC/USDT 선물 자동매매 봇 - 웹훅 버전 (최종)
거래소: Bitget (USDT-M 선물)
트리거: TradingView 웹훅 → Flask 서버 → Bitget 주문

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
전략 요약
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[시간봉별 시나리오] 신호가 울린 시간봉의 시나리오로만 진행 (진행 중 다른 신호는 무시)
  - 15분 : 1차 잔고 10% / 2차 잔고 5% / 2차 지정가 $200
  - 1시간: 손절 시 손실 = 잔고의 1% 로 수량 자동 계산 (1차:2차 = 7:5) / 2차 지정가 $500 / 손절가 = 2차 지정가 바깥 0.8%
  - 4시간: 손절 시 손실 = 잔고의 3% 로 수량 자동 계산 (1차:2차 = 5:5) / 2차 지정가 $700 / 손절가 = 2차 지정가 바깥 1.0%
  - 익절(가격 기준): 15분 TP1 +0.3% / TP2 +0.5%, 1시간 TP1 +0.5% / TP2 +0.7% / TP3 +1.0%, 4시간 TP1 +0.7% / TP2 +1.0% / TP3 +1.3%
  - 손절: 15분은 평단 기준 -0.3% (고정 비율 진입), 1시간·4시간은 위의 계산된 손절가
  - 진입 조건: 거래소에 포지션이 전혀 없을 때만 자동 진입 (수동 포지션·조회 실패 시에도 진입 안 함)
  - 청산 쿨다운: 포지션이 완전히 청산되면(익절·손절·본전 손절 모두) 그 시간봉 시나리오만 4시간 동안 신호 무시 (다른 시간봉은 그대로)
  - 상태 저장(D): 진행 상태·쿨다운을 파일(STATE_FILE)에 저장하고, 재시작 시 복구 (영구 디스크가 있어야 재배포 후에도 유지)
  - 모니터링 간격(C): 5초 (환경변수 MONITOR_INTERVAL 로 조정, 거래소 연결은 재사용하고 오류 시에만 재생성)
  - 안전장치 A: 2차 체결 후 손절(SL) 설정 실패 → 즉시 시장가 전량 청산
  - 안전장치 B: 2차 지정가 예약 실패 → 1차 포지션에 보호용 손절 설정(실패 시 즉시 청산)
  - 안전 상한: 자동 계산 수량의 합계 증거금이 잔고의 50%를 넘으면 수량을 줄임
  - 단계 구성: 1차 전량 TP → 2차 체결 후 TP1 / TP2 / TP3(1시간·4시간만) / 나머지는 본전 SL
  - TP1 매도 비율(전체 수량 대비): 15분 50% / 1시간 20% / 4시간 15%  (TP2는 모두 30%)
  - TP3(마지막 목표가): 평단 기준 1시간 +1.0% (전체의 30%) / 4시간 +1.3% (전체의 20%) 매도 (TP2 와 함께 예약)
  - 마지막에 남는 수량: 15분 20% / 1시간 20% / 4시간 35% (본전 SL 로만 보호, 완전 청산 전까지 새 진입 금지)

[1차 진입] 시장가 (레버리지 10배 격리)
  - TP: 진입가 기준 0.3% / 전체 수량 100%
  - SL: 없음
  - 2차 지정가 동시 예약 (잔고 5%)
    · 롱: 캔들 저점 - $200
    · 숏: 캔들 고점 + $200

[경우 A] 1차 TP 100% 체결
  → 전량 익절 → 2차 지정가 취소 → 처음부터 재시작

[경우 B] 2차 지정가 체결
  → 1차 TP 취소
  → 전체 포지션 평균단가 기준
    · TP1: 전체 수량의 일부(시나리오별 비율) / 평단 기준 시나리오별 %
    · SL : 전체 수량 100% / 평단 -0.3%

[TP1 체결 후]
  → SL을 평단가(본전)로 이동 (남은 수량 전체)
  → TP2: 전체 수량의 30% / 평단 기준 시나리오별 %
  → TP3(1시간·4시간만): 전체 수량의 20% / 평단 기준 시나리오별 %

[TP2 체결 후]
  → 남은 수량은 본전 SL로 보호
  → 남은 수량이 완전 청산될 때까지 새 진입 금지
  → 포지션이 0이 되면 자동으로 처음부터 재시작

[수동 개입 감지]
  - 단계별 정상 수량 목록(1차/2차 합산, 부분 익절 등)과 다르면 수동 개입으로 판단
  - 수동 개입 시 모든 주문 취소 + 봇 중단
  - 포지션 완전 청산 확인 후 자동 재시작
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import ccxt
import json
import logging
import os
import time
import threading
from flask import Flask, request, jsonify
from datetime import datetime

# ─────────────────────────────────────────
# 🔑 API 설정
# ─────────────────────────────────────────
API_KEY       = os.environ.get("BITGET_API_KEY", "YOUR_BITGET_API_KEY")
API_SECRET    = os.environ.get("BITGET_API_SECRET", "YOUR_BITGET_API_SECRET")
API_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "YOUR_BITGET_PASSPHRASE")
# Bitget 포지션 모드: 기본은 단방향(one-way). 헤지 모드 계정이면 환경변수 BITGET_HEDGE_MODE=true
HEDGE_MODE    = os.environ.get("BITGET_HEDGE_MODE", "false").lower() == "true"
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "my_secret_token_1234")

# ─────────────────────────────────────────
# ⚙️ 전략 파라미터
# ─────────────────────────────────────────
SYMBOL              = "BTC/USDT:USDT"
LEVERAGE            = 10
MARGIN_MODE         = "isolated"
# 시간봉별 시나리오 (신호가 울린 시간봉의 시나리오로만 진행)
#   first_ratio  : 1차 시장가 진입 비율 (잔고 대비)
#   second_ratio : 2차 지정가 진입 비율 (잔고 대비)
#   second_offset: 2차 지정가 간격 ($) - 롱: 캔들 저점 -, 숏: 캔들 고점 +
#   tp1_pct      : 1차 TP(전량) 및 2차 체결 후 TP1 가격 변동 기준 (평단/진입가 대비)
#   tp2_pct      : TP2 가격 변동 기준 (평단 대비)
#   tp1_close_ratio: 2차 체결 후 TP1 에서 팔 비율 (전체 수량 대비)
#   tp2_close_ratio: TP2 에서 팔 비율 (전체 수량 대비)
#   tp3_pct / tp3_close_ratio: (선택) 마지막 목표가. 없으면 TP3 없이 나머지를 본전 SL 로만 보호
#   sizing       : "fixed" = 잔고 대비 고정 비율로 진입, 손절은 평단 -STOP_LOSS_PCT
#                  "risk"  = 신호마다 수량 자동 계산 (아래 두 값 사용)
#                            · risk_pct     : 손절 시 잃을 금액 (잔고 대비)
#                            · sl_buffer_pct: 손절가 = 2차 지정가 바깥 이 비율만큼
#                            이때 first_ratio : second_ratio 는 1차/2차 분배 비율로만 쓰임
SCENARIOS = {
    "15m": {"name": "15분",  "sizing": "fixed", "first_ratio": 0.10, "second_ratio": 0.05, "second_offset": 200, "tp1_pct": 0.003, "tp2_pct": 0.005,
            "tp1_close_ratio": 0.50, "tp2_close_ratio": 0.50,   # TP2 = 남은 전량 (꼬리 없음)
            "tp_limit": True,    # 익절은 전부 지정가
            "cooldown_only_on_stop": True},   # 쿨다운은 손절·안전장치 청산 때만 (익절로 끝나면 바로 다음 신호 진입 가능)(reduceOnly, 메이커). 손절·안전장치만 시장가
    "1h":  {"name": "1시간", "sizing": "risk",  "first_ratio": 0.07, "second_ratio": 0.05, "second_offset": 500, "tp1_pct": 0.005, "tp2_pct": 0.007,
            "tp1_close_ratio": 0.20, "tp2_close_ratio": 0.30,
            "tp3_pct": 0.010, "tp3_close_ratio": 0.30,
            "risk_pct": 0.01, "sl_buffer_pct": 0.008, "tp_limit": True},
    "4h":  {"name": "4시간", "sizing": "risk",  "first_ratio": 0.05, "second_ratio": 0.05, "second_offset": 700, "tp1_pct": 0.007, "tp2_pct": 0.010,
            "tp1_close_ratio": 0.15, "tp2_close_ratio": 0.30,
            "tp3_pct": 0.013, "tp3_close_ratio": 0.20,
            "risk_pct": 0.03, "sl_buffer_pct": 0.010, "tp_limit": True},
}

# 자동 수량 계산 시 안전 상한: 1·2차 합계 증거금이 잔고의 이 비율을 넘지 않게 수량을 줄임
# (줄어들면 손실도 목표보다 작아지므로 안전한 쪽)
MAX_TOTAL_MARGIN_RATIO = 0.5
# ── 보호 없는 포지션 감시 ──
# ① 봇이 관리 중인데 필요한 보호 주문(TP/SL)이 상태에 없으면 GRACE 초 후 즉시 청산
# ② 미실현 손실이 '계획 손절 손실 × LOSS_CAP_MULT' 이상이면 즉시 청산 (손절이 안 걸렸거나 크게 미끄러진 경우의 최후 방어선)
WATCHDOG_GRACE = float(os.environ.get("WATCHDOG_GRACE", "30"))
LOSS_CAP_MULT  = float(os.environ.get("LOSS_CAP_MULT", "1.2"))

# ── 동시 신호 우선순위 ──
# 1h·4h(·15m) 알림이 같은 순간 겹쳐 오면 가장 큰 시간봉 하나만 진입 (4h > 1h > 15m)
# 4h 는 더 큰 봉이 없으므로 기다리지 않고 즉시 진입, 그 외는 SIGNAL_WINDOW 초 동안 더 큰 봉 신호를 기다림
# ── 2차 지정가 기준 ──
# True : 1차 진입가(평단) 기준으로 second_offset 만큼 불리한 쪽 (롱: 아래, 숏: 위)
# False: 신호 캔들의 저점/고점 기준으로 second_offset 만큼 바깥 (예전 방식)
SECOND_FROM_ENTRY = os.environ.get("SECOND_FROM_ENTRY", "true").lower() == "true"
SIGNAL_WINDOW = float(os.environ.get("SIGNAL_WINDOW", "3"))
TF_PRIORITY   = {"15m": 1, "1h": 2, "4h": 3}
# 레버리지·마진 설정은 매 진입마다 하지 않고 SETUP_TTL 초 동안 재사용 (진입 속도 개선)
SETUP_TTL     = float(os.environ.get("SETUP_TTL", "43200"))

# 웹훅 timeframe 값 → 시나리오 키 (트뷰 {{interval}} 은 15 / 60 / 240 으로 옴)
TIMEFRAME_ALIASES = {
    "15": "15m", "15m": "15m",
    "60": "1h",  "1h": "1h",
    "240": "4h", "4h": "4h",
}
# 손절은 "가격 변동 기준" (레버리지 적용 전) - 모든 시나리오 공통
# 익절(TP1/TP2)은 위 SCENARIOS 에서 시간봉별로 설정
STOP_LOSS_PCT       = 0.003     # 손절 가격 -0.3%
MONITOR_INTERVAL    = float(os.environ.get("MONITOR_INTERVAL", "5"))  # 모니터링 루프 간격 (초, 환경변수로 조정 가능)
# 상태 저장 파일. Render 에서 재배포·재시작 후에도 유지하려면 유료 플랜의 Disk 를 /var/data 에 붙이고
# 환경변수 STATE_FILE=/var/data/bot_state.json 으로 지정하세요. (디스크가 없으면 프로세스 재시작까지만 유지)
STATE_FILE          = os.environ.get("STATE_FILE", "bot_state.json")
COOLDOWN_AFTER_SL   = 4 * 3600  # 포지션이 청산된 시나리오는 이 시간(초) 동안 신호 무시 (익절·손절 공통)

# ─────────────────────────────────────────
# 📋 로깅 설정
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 상태 관리
# ─────────────────────────────────────────
state = {
    "active":           False,   # 봇 전략 진행 중 여부
    "manual_pause":     False,   # 수동 개입으로 일시 중단 여부
    "entry_count":      0,       # 체결된 진입 횟수
    "side":             None,    # "buy" or "sell"
    "tp_order_id":      None,    # 현재 TP1 주문 ID
    "tp2_order_id":     None,    # TP2 주문 ID
    "tp3_order_id":     None,    # TP3 주문 ID (1시간·4시간만)
    "limit_order_id":   None,    # 2차 지정가 주문 ID
    "sl_order_id":      None,    # SL 주문 ID
    "expected_qty":     0.0,     # 봇이 관리하는 총 예상 수량
    "allowed_qtys":     [],      # 현재 단계에서 정상으로 인정하는 포지션 수량 목록
    "avg_price":        0.0,     # 2차 진입 후 평균단가 (본전 SL용)
    "total_qty":        0.0,     # 2차 진입 후 전체 수량 (비율 계산용)
    "timeframe":        None,    # 현재 진행 중인 시나리오 (15m / 1h / 4h)
    "sl_price":         None,    # 신호 시점에 계산한 손절가 (risk 시나리오만, 없으면 평단 기준)
    "planned_loss":     0.0,     # 계획한 손절 시 손실(USDT) - 최대 손실 한도 계산용
    "last_signal":      None,
    "entering":         False,   # 진입 처리 중 (동시에 알림 2개가 와도 중복 진입 방지)
}

state_lock = threading.Lock()
# 시간봉별 쿨다운 종료 시각 (epoch 초). 서버 재시작 시 초기화됨 (안정장치 D 와 함께 저장 예정)
cooldown_until = {}
app = Flask(__name__)


# ─────────────────────────────────────────
# 상태 저장 / 복구 (안정장치 D)
# ─────────────────────────────────────────
_last_saved = None


def save_state():
    """현재 상태를 파일에 저장 (임시 파일에 쓰고 교체해서 쓰다 끊겨도 깨지지 않게). 내용이 같으면 건너뜀"""
    global _last_saved
    try:
        with state_lock:
            payload = {"state": dict(state), "cooldown_until": dict(cooldown_until)}
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if snapshot == _last_saved:
            return
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot)
        os.replace(tmp, STATE_FILE)
        _last_saved = snapshot
    except Exception as e:
        log.warning(f"상태 저장 실패: {e}")


def load_state():
    """시작할 때 저장된 상태를 복구. 거래소와의 불일치는 모니터링 루프가 바로잡음
    (상태는 진행 중인데 포지션이 없으면 초기화, 포지션이 있으면 주문 ID 로 이어서 관리)"""
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        saved = payload.get("state", {})
        with state_lock:
            for k in state:
                if k in saved:
                    state[k] = saved[k]
            state["entering"] = False
            cooldown_until.update({k: float(v) for k, v in payload.get("cooldown_until", {}).items()})
        log.info(f"💾 저장된 상태 복구: active={state['active']} 단계={state['entry_count']} 시간봉={state['timeframe']}")
    except Exception as e:
        log.warning(f"상태 복구 실패 (무시하고 새로 시작): {e}")


# ─────────────────────────────────────────
# 거래소 초기화
# ─────────────────────────────────────────
def get_exchange():
    return ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASSPHRASE,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })


def setup_leverage(exchange):
    """Bitget: 포지션 모드 → 마진 모드(격리) → 레버리지 순서. 각 단계 실패는 경고만 (이미 설정된 경우 등)"""
    steps = [
        ("포지션 모드", lambda: exchange.set_position_mode(HEDGE_MODE, SYMBOL)),
        ("마진 모드",   lambda: exchange.set_margin_mode(MARGIN_MODE, SYMBOL)),
    ]
    if HEDGE_MODE:
        # 헤지 모드에서는 롱/숏 각각 holdSide 를 지정해 레버리지 설정
        steps.append(("레버리지(롱)", lambda: exchange.set_leverage(LEVERAGE, SYMBOL, {"holdSide": "long"})))
        steps.append(("레버리지(숏)", lambda: exchange.set_leverage(LEVERAGE, SYMBOL, {"holdSide": "short"})))
    else:
        steps.append(("레버리지", lambda: exchange.set_leverage(LEVERAGE, SYMBOL)))
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            log.warning(f"{name} 설정 경고: {e}")
    log.info(f"레버리지 {LEVERAGE}배 / {MARGIN_MODE} 설정 시도 완료")


_setup_done_at = 0.0


def ensure_setup(exchange):
    """레버리지/마진 설정을 SETUP_TTL 안에는 다시 하지 않음 (신호→진입 지연 단축)"""
    global _setup_done_at
    if time.time() - _setup_done_at < SETUP_TTL:
        return
    setup_leverage(exchange)
    _setup_done_at = time.time()


def trigger_params(kind, price, extra=None):
    """TP/SL 트리거(조건부) 주문 파라미터 - Bitget 방식 (ccxt 통합 파라미터)
    kind: 'tp' → takeProfitPrice / 'sl' → stopLossPrice"""
    params = {"takeProfitPrice" if kind == "tp" else "stopLossPrice": price, "reduceOnly": True}
    if HEDGE_MODE:
        params["hedged"] = True
    if extra:
        params.update(extra)
    return params


def create_trigger_order(exchange, kind, close_side, qty, price, limit=None):
    # 시나리오가 tp_limit 이면 익절은 트리거 시장가 대신 지정가(reduceOnly)로 건다
    # limit 을 직접 주면 그 값 사용(진입 직후엔 state 의 시간봉이 아직 갱신 전이라)
    if limit is None:
        limit = bool(current_scenario().get("tp_limit"))
    if kind == "tp" and limit:
        params = {"reduceOnly": True}
        if HEDGE_MODE:
            params["hedged"] = True
        return exchange.create_order(SYMBOL, "limit", close_side, qty, price, params)
    return exchange.create_order(SYMBOL, "market", close_side, qty, None, trigger_params(kind, price))


def emergency_close(exchange, side, reason):
    """안전장치: 현재 포지션 전량을 시장가(reduceOnly)로 즉시 청산"""
    log.error(f"🚨 긴급 청산 실행 - 사유: {reason}")
    try:
        cancel_all_open_orders(exchange)
        pos = get_position(exchange)
        if not pos:
            log.info("긴급 청산: 이미 포지션 없음")
            return True
        qty = abs(float(pos.get("contracts") or 0))
        close_side = "sell" if side == "buy" else "buy"
        params = {"reduceOnly": True}
        if HEDGE_MODE:
            params["hedged"] = True
        exchange.create_order(SYMBOL, "market", close_side, qty, None, params)
        log.error(f"🚨 긴급 청산 주문 전송: {close_side} {qty} BTC")
        return True
    except Exception as e:
        log.error(f"❗❗ 긴급 청산 실패! 즉시 수동 확인 필요: {e}")
        return False


# ─────────────────────────────────────────
# 잔고 조회
# ─────────────────────────────────────────
def get_balance(exchange):
    try:
        balance = exchange.fetch_balance({"type": "swap"})
        usdt = balance["USDT"]["free"]
        log.info(f"사용 가능 잔고: {usdt:.2f} USDT")
        return usdt
    except Exception as e:
        log.error(f"잔고 조회 실패: {e}")
        return 0


# ─────────────────────────────────────────
# 포지션 조회
# ─────────────────────────────────────────
def get_position_strict(exchange):
    """조회 실패 시 예외를 그대로 던짐. 모니터링에서 '조회 실패'를 '포지션 없음'으로 오인하지 않기 위해 사용"""
    for pos in exchange.fetch_positions([SYMBOL]):
        if abs(float(pos.get("contracts") or 0)) > 0:
            return pos
    return None


def get_position(exchange):
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if abs(float(pos.get("contracts") or 0)) > 0:
                return pos
        return None
    except Exception as e:
        log.error(f"포지션 조회 실패: {e}")
        return None


# ─────────────────────────────────────────
# 주문 상태 조회
# ─────────────────────────────────────────
def list_open_orders(exchange):
    """일반 미체결 + 트리거(TP/SL) 미체결 주문을 합쳐서 반환. 둘 다 조회 성공해야 리스트 반환, 아니면 None"""
    try:
        normal = exchange.fetch_open_orders(SYMBOL)
        trig   = exchange.fetch_open_orders(SYMBOL, None, None, {"trigger": True})
        seen, merged = set(), []
        for o in list(normal) + list(trig):
            if o["id"] not in seen:
                seen.add(o["id"])
                merged.append(o)
        return merged
    except Exception as e:
        log.warning(f"미체결 주문 목록 조회 실패: {e}")
        return None


def get_order_status(exchange, order_id):
    """상태: 'open' / 'closed' / 'canceled' / None(알 수 없음)
    1) 일반 조회 → 2) 트리거 주문 조회 → 3) 둘 다 실패하면 '미체결 목록에 없음 = 체결됨' 으로 판단"""
    for params in ({}, {"trigger": True}):
        try:
            order = exchange.fetch_order(order_id, SYMBOL, params)
            if order and order.get("status"):
                return order.get("status")
        except Exception:
            pass
    # 방금 낸 주문은 거래소 조회에 늦게 반영될 수 있어 → 한 번 더 확인 후에만 '체결됨'으로 판단
    for attempt in range(2):
        open_list = list_open_orders(exchange)
        if open_list is None:
            return None
        if any(o["id"] == order_id for o in open_list):
            return "open"
        if attempt == 0:
            time.sleep(2)
            try:
                order = exchange.fetch_order(order_id, SYMBOL, {})
                if order and order.get("status"):
                    return order.get("status")
            except Exception:
                pass
    return "closed"


# ─────────────────────────────────────────
# 주문 취소
# ─────────────────────────────────────────
def cancel_order_safe(exchange, order_id):
    if not order_id:
        return False
    last_err = None
    for params in ({}, {"trigger": True}):
        try:
            exchange.cancel_order(order_id, SYMBOL, params)
            log.info(f"  주문 취소: {order_id}")
            return True
        except Exception as e:
            last_err = e
    log.warning(f"  주문 취소 실패 ({order_id}): {last_err}")
    return False


def cancel_all_open_orders(exchange):
    open_orders = list_open_orders(exchange)
    if open_orders is None:
        log.error("열린 주문 전체 취소 실패: 목록 조회 불가")
        return
    for order in open_orders:
        cancel_order_safe(exchange, order["id"])
    log.info(f"열린 주문 {len(open_orders)}개 전체 취소")


# ─────────────────────────────────────────
# 상태 초기화
# ─────────────────────────────────────────
def reset_state(manual_pause=False):
    with state_lock:
        state["active"]         = False
        state["manual_pause"]   = manual_pause
        state["entry_count"]    = 0
        state["side"]           = None
        state["tp_order_id"]    = None
        state["tp2_order_id"]   = None
        state["tp3_order_id"]   = None
        state["limit_order_id"] = None
        state["sl_order_id"]    = None
        state["expected_qty"]   = 0.0
        state["avg_price"]      = 0.0
        state["total_qty"]      = 0.0
        state["allowed_qtys"]   = []
        state["timeframe"]      = None
        state["sl_price"]       = None
        state["planned_loss"]   = 0.0
    save_state()
    if manual_pause:
        log.warning("⏸️  수동 개입 감지 → 봇 일시 중단 (포지션 청산 후 자동 재시작)")
    else:
        log.info("🔄 상태 초기화 완료 → 다음 신호 대기 중")


# ─────────────────────────────────────────
# 청산 쿨다운
# ─────────────────────────────────────────
def start_cooldown(tf_key):
    if not tf_key:
        return
    with state_lock:
        cooldown_until[tf_key] = time.time() + COOLDOWN_AFTER_SL
    save_state()
    name = SCENARIOS.get(tf_key, {}).get("name", tf_key)
    log.warning(f"⏳ [{name}] 포지션 청산 → {COOLDOWN_AFTER_SL // 3600}시간 동안 이 시간봉 신호 무시")


def cooldown_after_profit(tf_key):
    """익절(이익)로 끝났을 때의 쿨다운. cooldown_only_on_stop 시나리오는 쿨다운 없음"""
    if tf_key and SCENARIOS.get(tf_key, {}).get("cooldown_only_on_stop"):
        log.info(f"✅ [{SCENARIOS[tf_key]['name']}] 익절로 종료 → 쿨다운 없음 (손절일 때만 쉼)")
        return
    start_cooldown(tf_key)


def cooldown_remaining(tf_key):
    with state_lock:
        return max(0.0, cooldown_until.get(tf_key, 0.0) - time.time())


# ─────────────────────────────────────────
# 현재 진행 중인 시나리오 설정 조회 (state_lock 밖에서 호출)
# ─────────────────────────────────────────
def current_scenario():
    with state_lock:
        tf = state["timeframe"]
    return SCENARIOS.get(tf) or SCENARIOS["15m"]


# ─────────────────────────────────────────
# 신호별 주문 계획 계산 (수량 / 2차 지정가 / 손절가)
# ─────────────────────────────────────────
def compute_order_plan(scenario, side, price, candle_high, candle_low, balance):
    """
    신호가 올 때마다 1차·2차 수량과 2차 지정가, 손절가를 계산합니다.

    · fixed: 잔고 × 비율 × 레버리지 ÷ 가격 으로 수량 결정 (손절은 평단 기준 → sl_price=None)
    · risk : 손절 시 잃을 금액(잔고 × risk_pct)에 맞춰 총 수량을 역산
             손절가 = 2차 지정가 바깥 sl_buffer_pct
             총 수량 = 목표 손실 ÷ (예상 평단 - 손절가)
             1차/2차는 first_ratio : second_ratio 로 분배

    반환: dict(first_qty, second_qty, limit_price, sl_price, est_avg,
              est_loss_usdt, margin_ratio, capped)
    비정상 입력이면 ValueError
    """
    r1, r2 = scenario["first_ratio"], scenario["second_ratio"]
    w1 = r1 / (r1 + r2)
    w2 = 1.0 - w1
    offset = scenario["second_offset"]

    if SECOND_FROM_ENTRY:
        limit_price = (price - offset) if side == "buy" else (price + offset)
    else:
        limit_price = (candle_low - offset) if side == "buy" else (candle_high + offset)

    # 2차 지정가는 현재가보다 불리한 쪽(롱: 아래, 숏: 위)에 있어야 정상
    if side == "buy" and limit_price >= price:
        raise ValueError(f"2차 지정가({limit_price:.1f})가 현재가({price:.1f})보다 높음 - 캔들 값 확인 필요")
    if side == "sell" and limit_price <= price:
        raise ValueError(f"2차 지정가({limit_price:.1f})가 현재가({price:.1f})보다 낮음 - 캔들 값 확인 필요")

    est_avg = w1 * price + w2 * limit_price
    capped  = False

    if scenario.get("sizing") == "risk":
        buf      = scenario["sl_buffer_pct"]
        sl_price = limit_price * (1 - buf) if side == "buy" else limit_price * (1 + buf)
        loss_per_btc = abs(est_avg - sl_price)
        if loss_per_btc <= 0:
            raise ValueError("손절 거리가 0 이하 - 계산 불가")

        total_qty = balance * scenario["risk_pct"] / loss_per_btc

        # 안전 상한: 합계 증거금이 잔고의 MAX_TOTAL_MARGIN_RATIO 를 넘지 않게
        margin = total_qty * est_avg / LEVERAGE
        cap    = balance * MAX_TOTAL_MARGIN_RATIO
        if margin > cap:
            total_qty *= cap / margin
            capped = True

        first_qty  = total_qty * w1
        second_qty = total_qty * w2
        est_loss   = total_qty * loss_per_btc
    else:
        sl_price   = None
        first_qty  = balance * r1 * LEVERAGE / price
        second_qty = balance * r2 * LEVERAGE / price
        est_loss   = (first_qty + second_qty) * est_avg * STOP_LOSS_PCT

    margin_ratio = (first_qty + second_qty) * est_avg / LEVERAGE / balance if balance > 0 else 0.0

    return {
        "first_qty": first_qty, "second_qty": second_qty,
        "limit_price": limit_price, "sl_price": sl_price,
        "est_avg": est_avg, "est_loss_usdt": est_loss,
        "margin_ratio": margin_ratio, "capped": capped,
    }


# ─────────────────────────────────────────
# 시장가 주문
# ─────────────────────────────────────────
def market_limits(exchange):
    """거래소가 알려주는 최소 주문 수량(BTC)·최소 주문 금액(USDT). 못 읽으면 보수적 기본값"""
    min_amt, min_cost = 0.0001, 5.0
    try:
        lim = (exchange.market(SYMBOL) or {}).get("limits") or {}
        a = (lim.get("amount") or {}).get("min")
        c = (lim.get("cost") or {}).get("min")
        if a:
            min_amt = float(a)
        if c:
            min_cost = float(c)
    except Exception as e:
        log.warning(f"거래소 최소 주문 조건 조회 실패 → 기본값 사용: {e}")
    return min_amt, min_cost


def place_market_order(exchange, side, usdt_amount=None, qty=None):
    """qty 를 직접 주면 그 수량으로, 없으면 usdt_amount 로 계산해서 주문"""
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price  = ticker["last"]
        if qty is None:
            qty = (usdt_amount * LEVERAGE) / price
        qty    = float(exchange.amount_to_precision(SYMBOL, qty))

        order = exchange.create_market_order(SYMBOL, side, qty)
        log.info(f"✅ 시장가 체결: {side.upper()} {qty} BTC @ {price:.2f} USDT")
        return order, price
    except Exception as e:
        log.error(f"시장가 주문 실패: {e}")
        return None, None



def resolve_fill(exchange, order, requested_qty, fallback_price):
    """시장가 주문 직후 실제 체결 수량·평균가를 확인.
    Bitget 은 주문 생성 응답에 filled/average 를 주지 않는 경우가 있어(수량 0 으로 읽히는 문제),
    ① 응답값 → ② fetch_order → ③ 실제 포지션 → ④ 요청 수량 순으로 확인한다."""
    qty, avg = 0.0, None
    try:
        qty = float(order.get("filled") or 0)
        avg = order.get("average") or order.get("price")
    except Exception:
        pass
    if qty <= 0 and order.get("id"):
        for _ in range(3):
            try:
                o = exchange.fetch_order(order["id"], SYMBOL)
                qty = float(o.get("filled") or 0)
                avg = o.get("average") or o.get("price") or avg
                if qty > 0:
                    break
            except Exception as e:
                log.warning(f"체결 조회 재시도: {e}")
            time.sleep(0.4)
    if qty <= 0:
        try:
            pos = get_position_strict(exchange)
            if pos:
                qty = abs(float(pos.get("contracts") or 0))
                avg = pos.get("entryPrice") or avg
        except Exception as e:
            log.warning(f"포지션으로 체결 수량 확인 실패: {e}")
    if qty <= 0:
        qty = float(requested_qty)   # 시장가는 전량 체결이 일반적이므로 요청 수량 사용
        log.warning(f"체결 수량을 확인하지 못해 요청 수량 사용: {qty}")
    try:
        avg = float(avg) if avg else float(fallback_price)
    except Exception:
        avg = float(fallback_price)
    return float(exchange.amount_to_precision(SYMBOL, qty)), avg

# ─────────────────────────────────────────
# 1차 TP 주문 설정 (전체 수량 100%)
# ─────────────────────────────────────────
def set_tp_order_full(exchange, side, ref_price, full_qty, tp_pct, limit=None):
    """
    1차 진입 시 전체 수량 100% TP로 설정합니다.
    TP 도달 시 전량 익절 후 봇 재시작.
    """
    try:
        close_side = "sell" if side == "buy" else "buy"
        tp_price   = ref_price * (1 + tp_pct) if side == "buy" \
                     else ref_price * (1 - tp_pct)
        tp_price   = float(exchange.price_to_precision(SYMBOL, tp_price))

        tp_order = create_trigger_order(exchange, "tp", close_side, full_qty, tp_price, limit)
        log.info(f"  📈 1차 TP 설정: {tp_price:.2f} USDT  수량: {full_qty} BTC (전체 100%)")
        return tp_order.get("id") if tp_order else None
    except Exception as e:
        log.error(f"1차 TP 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# 2차 TP1 주문 설정 (전체 수량의 일부만)
# ─────────────────────────────────────────
def set_tp_order(exchange, side, ref_price, full_qty, tp_pct, close_ratio):
    """
    2차 진입 후 전체 수량의 close_ratio 만큼만 TP로 설정합니다.
    ref_price: 평균단가 기준
    """
    try:
        close_side = "sell" if side == "buy" else "buy"
        tp_price   = ref_price * (1 + tp_pct) if side == "buy" \
                     else ref_price * (1 - tp_pct)
        tp_price   = float(exchange.price_to_precision(SYMBOL, tp_price))
        tp_qty     = float(exchange.amount_to_precision(SYMBOL, full_qty * close_ratio))

        tp_order = create_trigger_order(exchange, "tp", close_side, tp_qty, tp_price)
        log.info(f"  📈 2차 TP 설정: {tp_price:.2f} USDT  수량: {tp_qty} BTC (전체의 {close_ratio:.0%})")
        return tp_order.get("id") if tp_order else None
    except Exception as e:
        log.error(f"2차 TP 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# SL 주문 설정 (전체 수량 100%)
# ─────────────────────────────────────────
def set_sl_order(exchange, side, avg_price, total_qty, sl_price=None):
    """sl_price 를 주면 그 가격에 손절, 없으면 평단 기준 -STOP_LOSS_PCT"""
    try:
        close_side = "sell" if side == "buy" else "buy"
        if sl_price is None:
            sl_price = avg_price * (1 - STOP_LOSS_PCT) if side == "buy" \
                       else avg_price * (1 + STOP_LOSS_PCT)
        sl_price   = float(exchange.price_to_precision(SYMBOL, sl_price))

        sl_order = create_trigger_order(exchange, "sl", close_side, total_qty, sl_price)
        log.info(f"  📉 SL 설정: {sl_price:.2f} USDT  수량: {total_qty} BTC (전체 100%)")
        return sl_order.get("id") if sl_order else None
    except Exception as e:
        log.error(f"SL 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# 본전 SL 설정 (TP1 체결 후 SL을 평단가로 이동)
# ─────────────────────────────────────────
def set_sl_order_breakeven(exchange, side, avg_price, qty):
    try:
        close_side = "sell" if side == "buy" else "buy"
        sl_price   = float(exchange.price_to_precision(SYMBOL, avg_price))

        sl_order = create_trigger_order(exchange, "sl", close_side, qty, sl_price)
        log.info(f"  📉 본전 SL 이동: {sl_price:.2f} USDT (평단가 기준)  수량: {qty} BTC")
        return sl_order.get("id") if sl_order else None
    except Exception as e:
        log.error(f"본전 SL 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# 2차 지정가 주문 예약
# ─────────────────────────────────────────
def place_second_limit_order(exchange, side, candle_high, candle_low, usdt_amount, offset,
                             qty=None, limit_price=None):
    """qty / limit_price 를 직접 주면 그 값으로, 없으면 기존 방식으로 계산"""
    try:
        if limit_price is None:
            limit_price = (candle_low - offset) if side == "buy" \
                          else (candle_high + offset)
        limit_price = float(exchange.price_to_precision(SYMBOL, limit_price))

        if qty is None:
            ticker        = exchange.fetch_ticker(SYMBOL)
            current_price = ticker["last"]
            qty           = (usdt_amount * LEVERAGE) / current_price
        qty           = float(exchange.amount_to_precision(SYMBOL, qty))

        limit_order = exchange.create_limit_order(SYMBOL, side, qty, limit_price)

        if SECOND_FROM_ENTRY:
            label = f"1차 진입가 - ${offset}" if side == "buy" else f"1차 진입가 + ${offset}"
        else:
            label = f"저점 - ${offset}" if side == "buy" else f"고점 + ${offset}"
        log.info(f"  📌 2차 지정가 예약: {limit_price:.2f} USDT ({label})")
        return limit_order.get("id") if limit_order else None, qty
    except Exception as e:
        log.error(f"2차 지정가 주문 실패: {e}")
        return None, 0


# ─────────────────────────────────────────
# 수동 개입 감지
# 봇이 알고 있는 expected_qty 와 실제 포지션 수량 비교
# ─────────────────────────────────────────
def detect_manual_intervention(position):
    """
    실제 포지션 수량이 '현재 단계에서 정상으로 인정하는 수량 목록'
    (allowed_qtys) 중 어느 것과도 맞지 않으면 수동 개입으로 판단합니다.

    예) 1차 단계: [1차 수량, 1차+2차 수량]  → 2차 체결로 2배가 돼도 정상
        2차 단계: [전체, 전체 - TP1 매도분]            → TP1 체결로 줄어도 정상
        3단계   : [TP1 후 잔여, TP2 후 잔여, TP3 후 잔여] → TP2/TP3 체결로 줄어도 정상
        4단계   : [TP2 후 남은 수량, (TP3 대기 중이면) TP3 후 남은 수량]

    허용 오차: max(0.001 BTC, 해당 수량의 1%)
    """
    with state_lock:
        if not state["active"]:
            return False
        allowed = list(state["allowed_qtys"])

    actual = abs(float(position.get("contracts") or 0))

    if not allowed:
        return False

    for a in allowed:
        tol = max(0.001, a * 0.01)
        if abs(actual - a) <= tol:
            return False

    log.warning(f"⚠️  수량 불일치 감지! 인정 수량: {[round(a, 4) for a in allowed]} BTC / 실제: {actual:.4f} BTC")
    return True


# ─────────────────────────────────────────
# 모니터링 스레드
# ─────────────────────────────────────────
def monitor_loop():
    log.info("🔍 모니터링 스레드 시작")
    exchange = None   # 거래소 연결은 재사용 (3초마다 새로 만들면 시장 정보를 매번 다시 받아 느려짐)
    unprotected_since = None   # 보호 주문이 없는 상태가 시작된 시각
    while True:
        try:
            time.sleep(MONITOR_INTERVAL)
            save_state()   # 직전 반복에서 바뀐 상태 저장
            if exchange is None:
                exchange = get_exchange()

            with state_lock:
                is_active      = state["active"]
                is_paused      = state["manual_pause"]
                entry_count    = state["entry_count"]
                side           = state["side"]
                tp_order_id    = state["tp_order_id"]
                limit_order_id = state["limit_order_id"]
                sl_order_id    = state["sl_order_id"]

            # ── 수동 중단 상태: 포지션 완전 청산 대기 후 재시작 ──
            if is_paused:
                position = get_position_strict(exchange)
                if not position:
                    log.info("✅ 포지션 완전 청산 확인 → 봇 자동 재시작 대기 중")
                    reset_state(manual_pause=False)
                else:
                    log.info(f"⏸️  수동 중단 중... 포지션 청산 대기 ({abs(float(position.get('contracts', 0))):.4f} BTC 남음)")
                continue

            # ── 비활성 상태면 스킵 ──
            if not is_active:
                unprotected_since = None
                continue

            # ── 포지션 조회 (실패하면 예외 → 이번 반복 건너뜀. 실패를 '포지션 없음'으로 보면 안 됨) ──
            position = get_position_strict(exchange)

            # ── 포지션 없음: 모두 청산됨 ──
            if not position:
                log.info("📭 포지션 없음 → 미체결 주문 정리 후 초기화")
                # 기본: 익절·손절 어느 쪽으로 끝났든 해당 시간봉 쿨다운 시작
                # (cooldown_only_on_stop 시나리오는 익절로 끝난 게 확인될 때만 쿨다운 생략)
                with state_lock:
                    closed_tf = state["timeframe"]
                profit_exit = False
                if SCENARIOS.get(closed_tf, {}).get("cooldown_only_on_stop"):
                    if entry_count >= 3:        # TP1 이후 단계 = 이미 이익 실현됨 (본전 청산 포함)
                        profit_exit = True
                    elif tp_order_id and get_order_status(exchange, tp_order_id) == "closed":
                        profit_exit = True      # 1차 전량 TP 또는 TP1 체결
                if profit_exit:
                    cooldown_after_profit(closed_tf)
                else:
                    start_cooldown(closed_tf)
                cancel_all_open_orders(exchange)
                reset_state()
                continue

            # ── 보호 없는 포지션 감시 ──
            with state_lock:
                wd_tf     = state["timeframe"]
                wd_loss   = state["planned_loss"]
            # 단계별로 있어야 하는 보호 주문 (1단계: 전량 TP 또는 보호 SL / 2단계 이상: SL)
            if entry_count <= 1:
                protected = bool(tp_order_id or sl_order_id)
            else:
                protected = bool(sl_order_id)
            if protected:
                unprotected_since = None
            else:
                if unprotected_since is None:
                    unprotected_since = time.time()
                    log.warning(f"⚠️ 보호 주문 없는 포지션 감지 → {WATCHDOG_GRACE:.0f}초 안에 복구되지 않으면 청산")
                elif time.time() - unprotected_since >= WATCHDOG_GRACE:
                    emergency_close(exchange, side, "보호 주문(TP/SL) 없는 포지션 감시")
                    start_cooldown(wd_tf)
                    reset_state()
                    unprotected_since = None
                    continue
            # 최대 손실 한도: 미실현 손실이 계획 손절 손실의 LOSS_CAP_MULT 배 이상이면 청산
            try:
                upnl = position.get("unrealizedPnl")
                if upnl is not None and wd_loss and float(upnl) <= -wd_loss * LOSS_CAP_MULT:
                    emergency_close(exchange, side,
                                    f"최대 손실 한도 도달 (미실현 {float(upnl):.2f} USDT ≤ -{wd_loss * LOSS_CAP_MULT:.2f})")
                    start_cooldown(wd_tf)
                    reset_state()
                    unprotected_since = None
                    continue
            except Exception as e:
                log.warning(f"손실 한도 확인 오류: {e}")

            # ── 수동 개입 감지 ──
            if detect_manual_intervention(position):
                log.warning("🚫 수동 개입 감지 → 모든 봇 명령 중단")
                cancel_all_open_orders(exchange)
                reset_state(manual_pause=True)
                continue

            # ════════════════════════════════════
            # 1차 진입 상태 모니터링
            # ════════════════════════════════════
            if entry_count == 1:

                # 1차 TP 전체 체결 확인
                if tp_order_id:
                    tp_status = get_order_status(exchange, tp_order_id)
                    _act = abs(float(position.get("contracts") or 0))
                    with state_lock:
                        _exp = state["expected_qty"]
                    if tp_status == "closed" and _exp > 0 and _act >= _exp * 0.5:
                        # 전량 익절이면 포지션이 사라져야 함 → 포지션이 그대로면 조회 오류로 보고 무시
                        log.warning(f"⚠️ TP '체결' 응답이나 포지션 {_act:.4f}/{_exp:.4f} BTC 그대로 → 조회 오류로 보고 무시")
                    elif tp_status == "closed":
                        log.info("🎉 1차 TP 전체 체결! → 2차 지정가 취소 → 처음부터 재시작")
                        with state_lock:
                            tp_tf = state["timeframe"]
                        cooldown_after_profit(tp_tf)
                        cancel_order_safe(exchange, limit_order_id)
                        cancel_all_open_orders(exchange)
                        reset_state()
                        continue

                # 보호용 SL(안전장치 B) 체결 확인
                if sl_order_id and get_order_status(exchange, sl_order_id) == "closed":
                    log.info("📉 1차 보호용 SL 체결 → 포지션 청산 완료")
                    with state_lock:
                        start_tf = state["timeframe"]
                    start_cooldown(start_tf)
                    cancel_all_open_orders(exchange)
                    reset_state()
                    continue

                # 2차 지정가 체결 확인
                if limit_order_id:
                    limit_status = get_order_status(exchange, limit_order_id)
                    if limit_status == "closed":
                        _act2 = abs(float(position.get("contracts") or 0))
                        with state_lock:
                            _exp2 = state["expected_qty"]
                        if _exp2 > 0 and _act2 <= _exp2 * 1.2:
                            # 2차가 체결됐다면 수량이 늘어야 함 → 그대로면 조회 오류로 보고 무시
                            log.warning(f"⚠️ 2차 '체결' 응답이나 포지션 {_act2:.4f}/{_exp2:.4f} BTC 그대로 → 조회 오류로 보고 무시")
                            limit_status = "open"
                    if limit_status == "closed":
                        log.info("✅ 2차 지정가 체결! → 1차 TP 취소 후 평균단가 기준 TP+SL 재설정")

                        # 1차 TP 취소
                        cancel_order_safe(exchange, tp_order_id)

                        # 평균단가 & 총 수량 조회
                        time.sleep(1)
                        position = get_position(exchange)
                        if not position:
                            log.error("2차 체결 후 포지션 조회 실패")
                            continue

                        avg_price  = float(position.get("entryPrice") or 0)
                        total_qty  = abs(float(position.get("contracts") or 0))
                        log.info(f"  📊 평균단가: {avg_price:.2f}  총 수량: {total_qty:.4f} BTC")

                        # 전체의 일부(시나리오별 비율)만 TP1 / 전체 100% SL
                        sc = current_scenario()
                        new_tp_id = set_tp_order(exchange, side, avg_price, total_qty,
                                                 sc["tp1_pct"], sc["tp1_close_ratio"])
                        with state_lock:
                            planned_sl = state["sl_price"]
                        # 위험 기반 시나리오: 2차 체결 후 '실제 평단·실제 수량' 기준으로 손절가를 다시 계산
                        # → 손절 시 손실이 계획 손실(잔고 대비 risk_pct)에 정확히 맞음
                        with state_lock:
                            _pl = state["planned_loss"]
                        if sc.get("sizing") == "risk" and _pl and total_qty > 0:
                            _d = _pl / total_qty
                            _px = avg_price - _d if side == "buy" else avg_price + _d
                            if _px > 0:
                                planned_sl = _px
                        new_sl_id = set_sl_order(exchange, side, avg_price, total_qty, planned_sl)
                        if not new_sl_id:
                            # 안전장치 A: 손절 설정 실패 → 손절 없는 포지션을 두지 않고 즉시 청산
                            emergency_close(exchange, side, "2차 체결 후 SL 설정 실패")
                            reset_state()
                            continue
                        if planned_sl is not None:
                            exp_loss = abs(avg_price - planned_sl) * total_qty
                            log.info(f"  🎯 계산된 손절가 적용: {planned_sl:.2f}  (손절 시 예상 손실 약 {exp_loss:.2f} USDT)")

                        with state_lock:
                            state["entry_count"]    = 2
                            state["tp_order_id"]    = new_tp_id
                            state["sl_order_id"]    = new_sl_id
                            state["limit_order_id"] = None
                            state["expected_qty"]   = total_qty
                            state["avg_price"]      = avg_price
                            state["total_qty"]      = total_qty
                            state["allowed_qtys"]   = [total_qty, total_qty * (1 - sc["tp1_close_ratio"])]
                        continue

            # ════════════════════════════════════
            # 2차 진입 상태 모니터링
            # ════════════════════════════════════
            elif entry_count == 2:

                with state_lock:
                    tp2_order_id = state["tp2_order_id"]
                    avg_price    = state["avg_price"]
                    total_qty    = state["total_qty"]

                # ── TP1 체결 확인 ──
                if tp_order_id:
                    tp_status = get_order_status(exchange, tp_order_id)
                    if tp_status == "closed":
                        log.info(f"🎉 2차 TP1 {current_scenario()['tp1_close_ratio']:.0%} 체결! → SL 본전으로 이동 + TP2 예약")

                        # 기존 SL 취소
                        cancel_order_safe(exchange, sl_order_id)

                        sc = current_scenario()
                        r1 = sc["tp1_close_ratio"]
                        r2 = sc["tp2_close_ratio"]

                        # 잔여 수량 (TP1 로 판 만큼 뺀 나머지)
                        remaining_qty = float(exchange.amount_to_precision(
                            SYMBOL, total_qty * (1 - r1)
                        ))

                        # TP2: 전체의 r2 / 평단 기준 시나리오별 tp2_pct
                        tp2_qty = float(exchange.amount_to_precision(
                            SYMBOL, total_qty * r2
                        ))
                        if r1 + r2 >= 0.999:      # TP2 가 남은 전량이면 반올림 잔량이 남지 않게 맞춤
                            tp2_qty = remaining_qty
                        close_side = "sell" if side == "buy" else "buy"
                        tp2_pct    = current_scenario()["tp2_pct"]
                        tp2_price  = avg_price * (1 + tp2_pct) if side == "buy" \
                                     else avg_price * (1 - tp2_pct)
                        tp2_price  = float(exchange.price_to_precision(SYMBOL, tp2_price))

                        tp2_order = create_trigger_order(exchange, "tp", close_side, tp2_qty, tp2_price)
                        new_tp2_id = tp2_order.get("id") if tp2_order else None
                        log.info(f"  📈 TP2 설정: {tp2_price:.2f} USDT  수량: {tp2_qty} BTC (전체의 {r2:.0%})")

                        # TP3 (마지막 목표가): 시나리오에 있을 때만, TP2 와 함께 미리 예약
                        new_tp3_id = None
                        r3 = 0.0
                        if sc.get("tp3_pct"):
                            r3        = sc["tp3_close_ratio"]
                            tp3_qty   = float(exchange.amount_to_precision(SYMBOL, total_qty * r3))
                            tp3_price = avg_price * (1 + sc["tp3_pct"]) if side == "buy" \
                                        else avg_price * (1 - sc["tp3_pct"])
                            tp3_price = float(exchange.price_to_precision(SYMBOL, tp3_price))
                            tp3_order = create_trigger_order(exchange, "tp", close_side, tp3_qty, tp3_price)
                            new_tp3_id = tp3_order.get("id") if tp3_order else None
                            log.info(f"  📈 TP3 설정: {tp3_price:.2f} USDT  수량: {tp3_qty} BTC (전체의 {r3:.0%})")

                        # SL 본전(평균단가)으로 이동
                        new_sl_id = set_sl_order_breakeven(exchange, side, avg_price, remaining_qty)

                        with state_lock:
                            state["entry_count"]  = 3
                            state["tp_order_id"]  = None
                            state["tp2_order_id"] = new_tp2_id
                            state["tp3_order_id"] = new_tp3_id
                            state["sl_order_id"]  = new_sl_id
                            state["expected_qty"] = remaining_qty
                            # 정상으로 인정하는 수량: TP1 후 / TP2 후 / (TP3까지 한 번에 지나간 경우) TP3 후
                            state["allowed_qtys"] = [remaining_qty,
                                                     total_qty * (1 - r1 - r2),
                                                     total_qty * (1 - r1 - r2 - r3)]
                        continue

                # SL 체결 확인
                if sl_order_id:
                    sl_status = get_order_status(exchange, sl_order_id)
                    if sl_status == "closed":
                        log.info("📉 2차 SL 체결 → 포지션 청산 완료")
                        with state_lock:
                            start_tf = state["timeframe"]
                        start_cooldown(start_tf)
                        cancel_all_open_orders(exchange)
                        reset_state()
                        continue

            # ════════════════════════════════════
            # 3단계: TP2 + 본전 SL 모니터링
            # (2차 TP1 익절 후 남은 수량 관리)
            # ════════════════════════════════════
            elif entry_count == 3:

                with state_lock:
                    tp2_order_id = state["tp2_order_id"]
                    tp3_order_id = state["tp3_order_id"]
                    sl_order_id2 = state["sl_order_id"]
                    avg_price    = state["avg_price"]
                    total_qty    = state["total_qty"]

                # TP2 체결 확인
                if tp2_order_id:
                    tp2_status = get_order_status(exchange, tp2_order_id)
                    if tp2_status == "closed":
                        log.info("🎉 TP2 체결! → 남은 수량 본전 SL로 보호 → 완전 청산까지 새 진입 금지")

                        # 가격이 한 번에 지나가서 TP3 도 같이 체결됐는지 확인
                        tp3_done = bool(tp3_order_id) and get_order_status(exchange, tp3_order_id) == "closed"

                        # 기존 SL 취소 후, 남은 실제 수량으로 본전 SL 재설정
                        cancel_order_safe(exchange, sl_order_id2)
                        time.sleep(1)
                        position = get_position(exchange)
                        if not position:
                            log.info("📭 TP2 체결 직후 포지션 없음 → 초기화")
                            cancel_all_open_orders(exchange)
                            reset_state()
                            continue

                        tail_qty  = abs(float(position.get("contracts") or 0))
                        tail_sl   = set_sl_order_breakeven(exchange, side, avg_price, tail_qty)
                        if not tail_sl:
                            log.error("❗ 남은 수량 본전 SL 설정 실패! 수동 확인 필요")

                        tp3_pending = bool(tp3_order_id) and not tp3_done
                        allowed = [tail_qty]
                        if tp3_pending:
                            r3 = current_scenario().get("tp3_close_ratio", 0.0)
                            allowed.append(max(tail_qty - total_qty * r3, 0.0))

                        with state_lock:
                            state["entry_count"]  = 4
                            state["tp2_order_id"] = None
                            state["tp3_order_id"] = tp3_order_id if tp3_pending else None
                            state["sl_order_id"]  = tail_sl
                            state["expected_qty"] = tail_qty
                            state["allowed_qtys"] = allowed
                        continue

                # 본전 SL 체결 확인
                if sl_order_id2:
                    sl2_status = get_order_status(exchange, sl_order_id2)
                    if sl2_status == "closed":
                        log.info("📉 본전 SL 체결 → 잔여 포지션 청산 완료")
                        with state_lock:
                            start_tf = state["timeframe"]
                        cooldown_after_profit(start_tf)
                        cancel_all_open_orders(exchange)
                        reset_state()
                        continue

            # ════════════════════════════════════
            # 4단계: 남은 수량 정리 대기
            # - 새 진입 금지 (active 상태 유지 → 웹훅 신호 무시)
            # - 본전 SL 체결 또는 수동 청산으로 포지션이 0이 되면
            #   위쪽 "포지션 없음" 분기에서 자동 초기화 → 새 진입 가능
            # ════════════════════════════════════
            elif entry_count == 4:

                with state_lock:
                    tp3_order_id = state["tp3_order_id"]
                    sl_order_id4 = state["sl_order_id"]
                    avg_price    = state["avg_price"]

                # TP3(마지막 목표가) 체결 확인 → 남은 수량에 맞춰 본전 SL 재설정
                if tp3_order_id:
                    if get_order_status(exchange, tp3_order_id) == "closed":
                        log.info("🎉 TP3 체결! → 남은 수량에 맞춰 본전 SL 재설정")
                        cancel_order_safe(exchange, sl_order_id4)
                        time.sleep(1)
                        position = get_position(exchange)
                        if not position:
                            log.info("📭 TP3 체결 직후 포지션 없음 → 초기화")
                            cancel_all_open_orders(exchange)
                            reset_state()
                            continue

                        final_qty = abs(float(position.get("contracts") or 0))
                        final_sl  = set_sl_order_breakeven(exchange, side, avg_price, final_qty)
                        if not final_sl:
                            log.error("❗ 남은 수량 본전 SL 설정 실패! 수동 확인 필요")

                        with state_lock:
                            state["tp3_order_id"] = None
                            state["sl_order_id"]  = final_sl
                            state["expected_qty"] = final_qty
                            state["allowed_qtys"] = [final_qty]
                        continue

                log.info("⏳ 남은 수량 정리 대기 중 (완전 청산 전까지 새 진입 없음)")

        except Exception as e:
            log.error(f"모니터링 루프 오류: {e}")
            exchange = None   # 연결 문제일 수 있으니 다음 반복에서 새로 연결


# ─────────────────────────────────────────
# 웹훅 엔드포인트
# ─────────────────────────────────────────
# ─────────────────────────────────────────
# 웹훅 수신 → 큐 → 워커 (동시에 온 신호 중 가장 큰 시간봉 우선)
# ─────────────────────────────────────────
pending_signals = []
pending_lock    = threading.Lock()
last_signal_result = {"tf": None, "code": None}   # 마지막으로 처리된 신호 (디버그·테스트용)


def drain_signals(force=False):
    """대기 중인 신호를 처리. 가장 큰 시간봉 1개만 진입, 나머지는 버림. 처리했으면 True"""
    with pending_lock:
        if not pending_signals:
            return False
        top_pri  = max(TF_PRIORITY[x["tf"]] for x in pending_signals)
        waited   = time.time() - min(x["t"] for x in pending_signals)
        # 가장 큰 봉(4h)이 이미 와 있으면 기다릴 필요 없음
        if not force and top_pri < max(TF_PRIORITY.values()) and waited < SIGNAL_WINDOW:
            return False
        batch = list(pending_signals)
        pending_signals.clear()
    # 우선순위 높은 순, 같으면 먼저 도착한 것
    batch.sort(key=lambda x: (-TF_PRIORITY[x["tf"]], x["t"]))
    best = batch[0]
    for x in batch[1:]:
        log.info(f"⏭ [{SCENARIOS[x['tf']]['name']}] 신호 무시 - 더 큰 시간봉 [{SCENARIOS[best['tf']]['name']}] 신호 우선")
    with app.app_context():
        try:
            body, code = process_signal(best["data"])
            last_signal_result["tf"], last_signal_result["code"] = best["tf"], code
            log.info(f"신호 처리 결과 [{SCENARIOS[best['tf']]['name']}] {code}")
        except Exception as e:
            log.error(f"신호 처리 오류: {e}")
    return True


def signal_worker():
    log.info("📨 신호 처리 스레드 시작")
    try:
        ex0 = get_exchange()
        ensure_setup(ex0)                 # 첫 신호가 빨리 들어가도록 미리 설정
        ma, mc = market_limits(ex0)
        log.info(f"📏 거래소 최소 주문: {ma} BTC / {mc} USDT")
    except Exception as e:
        log.warning(f"초기 레버리지 설정 경고: {e}")
    last_beat = time.time()
    while True:
        try:
            drain_signals()
            if time.time() - last_beat >= 300:
                last_beat = time.time()
                log.info(f"💓 신호 워커 동작 중 (대기 {len(pending_signals)}건)")
        except Exception as e:
            log.error(f"신호 워커 오류: {e}")
        time.sleep(0.25)


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    TradingView 알림 메시지 형식 (JSON):
    {
        "token":        "my_secret_token_1234",
        "direction":    "long" or "short",
        "timeframe":    "{{interval}}"   (15 / 60 / 240 또는 15m / 1h / 4h),
        "candle_high":  {{high}},
        "candle_low":   {{low}}
    }
    즉시 200 으로 응답하고, 실제 진입은 신호 워커가 처리합니다.
    """
    try:
        log.info(f"Content-Type: {request.content_type}")
        log.info(f"Headers: {dict(request.headers)}")
        log.info(f"Raw Data: {request.data}")
        data = request.get_json()
        log.info(f"웹훅 수신: {data}")

        if not data or data.get("token") != WEBHOOK_TOKEN:
            log.warning("❌ 유효하지 않은 토큰")
            return jsonify({"error": "Unauthorized"}), 401

        if str(data.get("direction", "")).lower() not in ("long", "short"):
            return jsonify({"error": "direction 값이 잘못됨 (long/short)"}), 400
        if data.get("candle_high") is None or data.get("candle_low") is None:
            return jsonify({"error": "candle_high / candle_low 값이 필요합니다"}), 400
        tf_raw = str(data.get("timeframe", "")).strip().lower()
        tf_key = TIMEFRAME_ALIASES.get(tf_raw)
        if tf_key is None:
            msg = f"timeframe 값이 없거나 잘못됨: '{tf_raw}' (15 / 60 / 240 중 하나여야 함)"
            log.warning(f"❌ {msg}")
            return jsonify({"error": msg}), 400

        with pending_lock:
            pending_signals.append({"tf": tf_key, "data": data, "t": time.time()})
            n_pending = len(pending_signals)
        log.info(f"📥 신호 대기열 추가: {tf_key} (대기 {n_pending}건)")
        return jsonify({"status": "queued", "timeframe": tf_key}), 200
    except Exception as e:
        log.error(f"웹훅 수신 오류: {e}")
        return jsonify({"error": str(e)}), 500


def process_signal(data):
    """큐에서 뽑힌 신호 하나를 실제로 처리 (진입). 반환값은 (json, 상태코드)"""
    got_entry_lock = False
    try:
        direction   = data.get("direction", "").lower()
        candle_high = data.get("candle_high")
        candle_low  = data.get("candle_low")

        if direction not in ("long", "short"):
            return jsonify({"error": "direction 값이 잘못됨 (long/short)"}), 400
        if candle_high is None or candle_low is None:
            return jsonify({"error": "candle_high / candle_low 값이 필요합니다"}), 400

        # ── 시간봉 → 시나리오 선택 (없거나 모르는 값이면 거부: 엉뚱한 시나리오로 진입 방지) ──
        tf_raw = str(data.get("timeframe", "")).strip().lower()
        tf_key = TIMEFRAME_ALIASES.get(tf_raw)
        if tf_key is None:
            msg = f"timeframe 값이 없거나 잘못됨: '{tf_raw}' (15 / 60 / 240 중 하나여야 함)"
            log.warning(f"❌ {msg}")
            return jsonify({"error": msg}), 400
        scenario = SCENARIOS[tf_key]

        # ── 손절 쿨다운 중인 시간봉이면 무시 ──
        remain = cooldown_remaining(tf_key)
        if remain > 0:
            msg = f"[{scenario['name']}] 청산 쿨다운 중 (남은 시간 {remain / 60:.0f}분). 신호 무시."
            log.info(msg)
            return jsonify({"status": "skipped", "reason": msg}), 200

        candle_high = float(candle_high)
        candle_low  = float(candle_low)
        order_side  = "buy" if direction == "long" else "sell"
        dir_kr      = "롱" if order_side == "buy" else "숏"

        # ── 수동 중단 상태 확인 ──
        with state_lock:
            if state["manual_pause"]:
                msg = "수동 개입으로 중단 중. 포지션 청산 후 자동 재시작됩니다."
                log.warning(msg)
                return jsonify({"status": "paused", "reason": msg}), 200

            # ── 이미 진행 중이거나 다른 신호 진입 처리 중이면 무시 ──
            if state["active"] or state["entering"]:
                msg = "이미 포지션 진행 중. 신호 무시."
                log.info(msg)
                return jsonify({"status": "skipped", "reason": msg}), 200
            state["entering"] = True
            got_entry_lock = True

        log.info(f"📊 신호: [{scenario['name']} 시나리오] {dir_kr}  캔들 고점: {candle_high}  저점: {candle_low}")

        # ── 거래소 연결 ──
        exchange = get_exchange()

        # ── 실제 거래소 포지션 확인: 포지션이 하나라도 있으면 절대 진입 안 함 ──
        # (수동으로 잡은 포지션, 서버 재시작으로 상태가 사라진 경우도 막기 위함)
        # 조회에 실패해도 안전하게 진입하지 않음
        try:
            existing = [p for p in exchange.fetch_positions([SYMBOL])
                        if abs(float(p.get("contracts") or 0)) > 0]
        except Exception as e:
            msg = f"포지션 조회 실패 → 안전을 위해 진입 안 함: {e}"
            log.error(msg)
            return jsonify({"status": "skipped", "reason": msg}), 200
        if existing:
            msg = "거래소에 이미 포지션이 있음. 포지션이 없을 때만 진입하므로 신호 무시."
            log.warning(msg)
            return jsonify({"status": "skipped", "reason": msg}), 200

        ensure_setup(exchange)

        # ── 잔고 조회 ──
        balance     = get_balance(exchange)

        # ── 수량 · 2차 지정가 · 손절가 계산 (시나리오별) ──
        try:
            cur_price = float(exchange.fetch_ticker(SYMBOL)["last"])
            plan = compute_order_plan(scenario, order_side, cur_price, candle_high, candle_low, balance)
        except ValueError as e:
            log.warning(f"❌ 주문 계획 계산 실패: {e}")
            return jsonify({"status": "skipped", "reason": str(e)}), 400

        # ── 거래소 최소 주문 조건 확인 (1·2차 모두 최소 수량·최소 금액 이상이어야 함) ──
        min_amt, min_cost = market_limits(exchange)
        q1 = float(exchange.amount_to_precision(SYMBOL, plan["first_qty"]))
        q2 = float(exchange.amount_to_precision(SYMBOL, plan["second_qty"]))
        if q1 < min_amt or q2 < min_amt or q1 * cur_price < min_cost or q2 * cur_price < min_cost:
            msg = (f"잔고 부족: 1차 {q1} / 2차 {q2} BTC 가 거래소 최소 "
                   f"({min_amt} BTC · {min_cost} USDT) 미달")
            log.warning(msg)
            return jsonify({"status": "skipped", "reason": msg}), 200

        log.info(f"  📐 [{scenario['name']}] 계획: 1차 {plan['first_qty']:.4f} / 2차 {plan['second_qty']:.4f} BTC "
                 f"(합계 증거금 잔고의 {plan['margin_ratio']:.1%}{' · 상한 적용' if plan['capped'] else ''})")
        if plan["sl_price"] is not None:
            log.info(f"  📐 손절가 예정: {plan['sl_price']:.2f}  손절 시 예상 손실 약 {plan['est_loss_usdt']:.2f} USDT "
                     f"(잔고의 {plan['est_loss_usdt'] / balance:.2%})")
        else:
            log.info(f"  📐 손절: 평단 기준 -{STOP_LOSS_PCT:.1%}  손절 시 예상 손실 약 {plan['est_loss_usdt']:.2f} USDT "
                     f"(잔고의 {plan['est_loss_usdt'] / balance:.2%})")

        # ── 1차 시장가 진입 ──
        order, entry_price = place_market_order(exchange, order_side, qty=plan["first_qty"])
        if not order or not entry_price:
            return jsonify({"status": "error", "reason": "1차 시장가 주문 실패"}), 500

        # 실제 체결 수량·평균가 확인 (응답에 값이 없어도 0 으로 읽지 않도록)
        qty, entry_price = resolve_fill(exchange, order, plan["first_qty"], entry_price)
        if qty <= 0:
            emergency_close(exchange, order_side, "1차 체결 수량 확인 불가")
            reset_state()
            return jsonify({"status": "error", "reason": "체결 수량 확인 실패 → 긴급 청산"}), 500

        # ── 1차 TP 설정 (전체 수량 100%, SL 없음) ──
        tp_order_id = set_tp_order_full(exchange, order_side, entry_price, qty,
                                        scenario["tp1_pct"], bool(scenario.get("tp_limit")))
        if not tp_order_id:
            time.sleep(0.5)
            tp_order_id = set_tp_order_full(exchange, order_side, entry_price, qty,
                                            scenario["tp1_pct"], bool(scenario.get("tp_limit")))
        if not tp_order_id:
            # 익절 주문 없이 포지션을 두면 봇이 관리할 수 없으므로 즉시 정리
            log.error("❗ 1차 TP 설정 실패(재시도 포함) → 긴급 청산")
            emergency_close(exchange, order_side, "1차 TP 설정 실패")
            reset_state()
            return jsonify({"status": "error", "reason": "1차 TP 설정 실패 → 긴급 청산"}), 500

        # ── 2차 지정가 예약 ──
        # 진입가 기준이면 실제 체결된 1차 평단으로 위치를 다시 계산 (수량 계획은 그대로)
        second_price = plan["limit_price"]
        if SECOND_FROM_ENTRY:
            second_price = (entry_price - scenario["second_offset"]) if order_side == "buy" \
                           else (entry_price + scenario["second_offset"])
        limit_order_id, limit_qty = place_second_limit_order(
            exchange, order_side, candle_high, candle_low, None,
            scenario["second_offset"],
            qty=plan["second_qty"], limit_price=second_price
        )

        # ── 안전장치 B: 2차 지정가 예약 실패 → 1차 포지션을 손절 없이 두지 않음 ──
        protective_sl_id = None
        if not limit_order_id:
            log.error("❗ 2차 지정가 예약 실패 → 1차 포지션에 보호용 SL 설정")
            sl_px = plan["sl_price"] if plan["sl_price"] is not None else None
            protective_sl_id = set_sl_order(exchange, order_side, entry_price, qty, sl_px)
            if not protective_sl_id:
                emergency_close(exchange, order_side, "2차 지정가 실패 + 보호용 SL 설정 실패")
                reset_state()
                return jsonify({"status": "error", "reason": "2차 지정가·보호 SL 모두 실패 → 긴급 청산"}), 500

        # ── 상태 업데이트 ──
        with state_lock:
            state["active"]         = True
            state["manual_pause"]   = False
            state["entry_count"]    = 1
            state["side"]           = order_side
            state["tp_order_id"]    = tp_order_id
            state["limit_order_id"] = limit_order_id
            state["sl_order_id"]    = protective_sl_id
            state["expected_qty"]   = qty
            state["allowed_qtys"]   = [qty, qty + limit_qty] if limit_order_id else [qty]
            state["timeframe"]      = tf_key
            state["sl_price"]       = plan["sl_price"]
            state["planned_loss"]   = float(plan["est_loss_usdt"] or 0.0)
            state["last_signal"]    = datetime.now().isoformat()
        save_state()

        log.info(f"🎯 [{scenario['name']}] 1차 {dir_kr} 진입 완료")
        log.info(f"  진입가: {entry_price:.2f}  수량: {qty} BTC")
        log.info(f"  TP (100%): {qty:.4f} BTC → TP 체결 시 전량 익절 후 재시작")
        log.info(f"  2차 지정가 대기: {second_price:.2f} USDT")

        return jsonify({
            "status": "ok",
            "entry": "1차",
            "timeframe": tf_key,
            "direction": dir_kr,
            "entry_price": entry_price,
            "qty": qty,
            "tp_qty": qty,
            "sl_set": False,
            "second_limit_price": second_price,
            "planned_sl_price": plan["sl_price"],
            "planned_total_qty": plan["first_qty"] + plan["second_qty"],
            "est_loss_at_sl_usdt": plan["est_loss_usdt"],
            "margin_ratio": plan["margin_ratio"],
        }), 200

    except Exception as e:
        log.error(f"웹훅 처리 오류: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if got_entry_lock:
            with state_lock:
                state["entering"] = False


# ─────────────────────────────────────────
# 상태 확인 엔드포인트
# ─────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    try:
        threads = thread_info()
        exchange = get_exchange()
        position = get_position(exchange)
        balance  = get_balance(exchange)

        pos_info = None
        if position:
            pos_info = {
                "side":            "롱" if float(position["contracts"]) > 0 else "숏",
                "size":            position["contracts"],
                "avg_entry_price": position.get("entryPrice"),
                "pnl":             position.get("unrealizedPnl"),
            }

        with state_lock:
            current_state = dict(state)

        return jsonify({
            "balance_usdt": balance,
            "bot_state":    current_state,
            "cooldown_minutes_left": {k: round(cooldown_remaining(k) / 60, 1) for k in SCENARIOS},
            "position":     pos_info,
            "threads":      threads,
        }), 200

    except Exception as e:
        return jsonify({"error": str(e), "threads": thread_info()}), 500


# ─────────────────────────────────────────
# 수동 초기화 엔드포인트 (긴급 시 사용)
# ─────────────────────────────────────────
@app.route("/reset", methods=["POST"])
def manual_reset():
    try:
        data = request.get_json()
        if not data or data.get("token") != WEBHOOK_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401

        exchange = get_exchange()
        cancel_all_open_orders(exchange)
        reset_state()
        with state_lock:
            cooldown_until.clear()
        return jsonify({"status": "ok", "message": "수동 초기화 완료 (쿨다운도 해제)"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
# 모니터링 스레드 시작
# gunicorn(Render)은 이 파일을 import만 하므로 __main__ 안에서 시작하면
# 스레드가 돌지 않습니다. 모듈 로드 시점에 한 번만 시작합니다.
# ※ 상태를 메모리에 두므로 gunicorn은 반드시 --workers 1 로 실행하세요.
# ─────────────────────────────────────────
_threads = {"pid": None, "monitor": None, "worker": None}
_start_lock = threading.Lock()
AUTOSTART_THREADS = True      # 테스트에서만 False


def start_monitor():
    """감시·신호 처리 스레드가 '현재 프로세스'에서 살아 있도록 보장.
    gunicorn --preload 등으로 프로세스가 복제되면 스레드는 복제되지 않으므로,
    요청이 들어올 때마다(before_request) 다시 확인해서 필요하면 새로 시작한다."""
    with _start_lock:
        pid = os.getpid()
        if _threads["pid"] != pid:
            _threads["pid"], _threads["monitor"], _threads["worker"] = pid, None, None
        for key, fn in (("monitor", monitor_loop), ("worker", signal_worker)):
            t = _threads[key]
            if t is None or not t.is_alive():
                t = threading.Thread(target=fn, daemon=True, name=key)
                t.start()
                _threads[key] = t
                log.info(f"🧵 {key} 스레드 가동 (pid {pid})")


@app.before_request
def _ensure_threads():
    if AUTOSTART_THREADS:
        start_monitor()


def thread_info():
    def alive(k):
        t = _threads[k]
        return bool(t is not None and t.is_alive())
    return {"pid": os.getpid(), "monitor_alive": alive("monitor"), "worker_alive": alive("worker"),
            "pending_signals": len(pending_signals), "last_signal": dict(last_signal_result)}


load_state()
start_monitor()


# ─────────────────────────────────────────
if __name__ == "__main__":
    log.info("🤖 BTC OI Delta 웹훅 봇 시작 (로컬 실행)")
    app.run(host="0.0.0.0", port=5000, debug=False)

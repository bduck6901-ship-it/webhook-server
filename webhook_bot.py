"""
BTC/USDT 선물 자동매매 봇 - 웹훅 버전 (최종)
거래소: BingX
트리거: TradingView 웹훅 → Flask 서버 → BingX 주문

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
전략 요약
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[1차 진입] 시장가 (잔고 5%)
  - TP: 진입가 기준 0.3% / 전체 수량 100%
  - SL: 없음
  - 2차 지정가 동시 예약
    · 롱: 캔들 저점 - $200
    · 숏: 캔들 고점 + $200

[경우 A] 1차 TP 100% 체결
  → 전량 익절 → 2차 지정가 취소 → 처음부터 재시작

[경우 B] 2차 지정가 체결
  → 1차 TP 취소
  → 전체 포지션 평균단가 기준
    · TP: 전체 수량의 50% / 0.3%
    · SL: 전체 수량 100% / 0.3%

[수동 개입 감지]
  - 봇 주문 외 수량 변동 감지 시 즉시 모든 자동 명령 중단
  - 포지션 완전 청산 확인 후 자동 재시작 대기
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import ccxt
import logging
import os
import time
import threading
from flask import Flask, request, jsonify
from datetime import datetime

# ─────────────────────────────────────────
# 🔑 API 설정
# ─────────────────────────────────────────
API_KEY       = os.environ.get("BINGX_API_KEY", "YOUR_BINGX_API_KEY")
API_SECRET    = os.environ.get("BINGX_API_SECRET", "YOUR_BINGX_API_SECRET")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "my_secret_token_1234")

# ─────────────────────────────────────────
# ⚙️ 전략 파라미터
# ─────────────────────────────────────────
SYMBOL              = "BTC/USDT:USDT"
LEVERAGE            = 100
MARGIN_MODE         = "isolated"
ENTRY_RATIO         = 0.05      # 1회 진입 비율 (잔고의 5%)
STOP_LOSS_PCT       = 0.003     # 손절 0.3%
TAKE_PROFIT_PCT     = 0.003     # 익절 0.3%
SECOND_ENTRY_OFFSET = 200       # 2차 지정가 오프셋 ($200)
MONITOR_INTERVAL    = 10        # 모니터링 루프 간격 (초)
TP_PARTIAL_RATIO    = 0.5       # TP 수량 비율 (50%)

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
    "tp_order_id":      None,    # 현재 TP 주문 ID
    "limit_order_id":   None,    # 2차 지정가 주문 ID
    "sl_order_id":      None,    # SL 주문 ID (2차 후 설정)
    "expected_qty":     0.0,     # 봇이 관리하는 총 예상 수량
    "last_signal":      None,
}

state_lock = threading.Lock()
app = Flask(__name__)


# ─────────────────────────────────────────
# 거래소 초기화
# ─────────────────────────────────────────
def get_exchange():
    return ccxt.bingx({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })


def setup_leverage(exchange):
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        exchange.set_margin_mode(MARGIN_MODE, SYMBOL)
        log.info(f"레버리지 {LEVERAGE}배 / {MARGIN_MODE} 설정 완료")
    except Exception as e:
        log.warning(f"레버리지 설정 경고: {e}")


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
def get_order_status(exchange, order_id):
    try:
        order = exchange.fetch_order(order_id, SYMBOL)
        return order.get("status")  # "open", "closed", "canceled"
    except Exception as e:
        log.warning(f"주문 상태 조회 실패 ({order_id}): {e}")
        return None


# ─────────────────────────────────────────
# 주문 취소
# ─────────────────────────────────────────
def cancel_order_safe(exchange, order_id):
    if not order_id:
        return False
    try:
        exchange.cancel_order(order_id, SYMBOL)
        log.info(f"  주문 취소: {order_id}")
        return True
    except Exception as e:
        log.warning(f"  주문 취소 실패 ({order_id}): {e}")
        return False


def cancel_all_open_orders(exchange):
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)
        for order in open_orders:
            cancel_order_safe(exchange, order["id"])
        log.info(f"열린 주문 {len(open_orders)}개 전체 취소")
    except Exception as e:
        log.error(f"열린 주문 전체 취소 실패: {e}")


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
        state["limit_order_id"] = None
        state["sl_order_id"]    = None
        state["expected_qty"]   = 0.0
    if manual_pause:
        log.warning("⏸️  수동 개입 감지 → 봇 일시 중단 (포지션 청산 후 자동 재시작)")
    else:
        log.info("🔄 상태 초기화 완료 → 다음 신호 대기 중")


# ─────────────────────────────────────────
# 시장가 주문
# ─────────────────────────────────────────
def place_market_order(exchange, side, usdt_amount):
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price  = ticker["last"]
        qty    = (usdt_amount * LEVERAGE) / price
        qty    = float(exchange.amount_to_precision(SYMBOL, qty))

        order = exchange.create_market_order(SYMBOL, side, qty)
        log.info(f"✅ 시장가 체결: {side.upper()} {qty} BTC @ {price:.2f} USDT")
        return order, price
    except Exception as e:
        log.error(f"시장가 주문 실패: {e}")
        return None, None


# ─────────────────────────────────────────
# 1차 TP 주문 설정 (전체 수량 100%)
# ─────────────────────────────────────────
def set_tp_order_full(exchange, side, ref_price, full_qty):
    """
    1차 진입 시 전체 수량 100% TP로 설정합니다.
    TP 도달 시 전량 익절 후 봇 재시작.
    """
    try:
        close_side = "sell" if side == "buy" else "buy"
        tp_price   = ref_price * (1 + TAKE_PROFIT_PCT) if side == "buy" \
                     else ref_price * (1 - TAKE_PROFIT_PCT)
        tp_price   = float(exchange.price_to_precision(SYMBOL, tp_price))

        tp_order = exchange.create_order(
            SYMBOL, "TAKE_PROFIT_MARKET", close_side, full_qty,
            params={"stopPrice": tp_price, "reduceOnly": True}
        )
        log.info(f"  📈 1차 TP 설정: {tp_price:.2f} USDT  수량: {full_qty} BTC (전체 100%)")
        return tp_order.get("id") if tp_order else None
    except Exception as e:
        log.error(f"1차 TP 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# 2차 TP 주문 설정 (수량의 50%만)
# ─────────────────────────────────────────
def set_tp_order(exchange, side, ref_price, full_qty):
    """
    2차 진입 후 전체 수량의 50%만 TP로 설정합니다.
    ref_price: 평균단가 기준
    """
    try:
        close_side = "sell" if side == "buy" else "buy"
        tp_price   = ref_price * (1 + TAKE_PROFIT_PCT) if side == "buy" \
                     else ref_price * (1 - TAKE_PROFIT_PCT)
        tp_price   = float(exchange.price_to_precision(SYMBOL, tp_price))
        tp_qty     = float(exchange.amount_to_precision(SYMBOL, full_qty * TP_PARTIAL_RATIO))

        tp_order = exchange.create_order(
            SYMBOL, "TAKE_PROFIT_MARKET", close_side, tp_qty,
            params={"stopPrice": tp_price, "reduceOnly": True}
        )
        log.info(f"  📈 2차 TP 설정: {tp_price:.2f} USDT  수량: {tp_qty} BTC (전체의 50%)")
        return tp_order.get("id") if tp_order else None
    except Exception as e:
        log.error(f"2차 TP 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# SL 주문 설정 (전체 수량 100%)
# ─────────────────────────────────────────
def set_sl_order(exchange, side, avg_price, total_qty):
    try:
        close_side = "sell" if side == "buy" else "buy"
        sl_price   = avg_price * (1 - STOP_LOSS_PCT) if side == "buy" \
                     else avg_price * (1 + STOP_LOSS_PCT)
        sl_price   = float(exchange.price_to_precision(SYMBOL, sl_price))

        sl_order = exchange.create_order(
            SYMBOL, "STOP_MARKET", close_side, total_qty,
            params={"stopPrice": sl_price, "reduceOnly": True}
        )
        log.info(f"  📉 SL 설정: {sl_price:.2f} USDT  수량: {total_qty} BTC (전체 100%)")
        return sl_order.get("id") if sl_order else None
    except Exception as e:
        log.error(f"SL 설정 실패: {e}")
        return None


# ─────────────────────────────────────────
# 2차 지정가 주문 예약
# ─────────────────────────────────────────
def place_second_limit_order(exchange, side, candle_high, candle_low, usdt_amount):
    try:
        limit_price = (candle_low - SECOND_ENTRY_OFFSET) if side == "buy" \
                      else (candle_high + SECOND_ENTRY_OFFSET)
        limit_price = float(exchange.price_to_precision(SYMBOL, limit_price))

        ticker        = exchange.fetch_ticker(SYMBOL)
        current_price = ticker["last"]
        qty           = (usdt_amount * LEVERAGE) / current_price
        qty           = float(exchange.amount_to_precision(SYMBOL, qty))

        limit_order = exchange.create_limit_order(SYMBOL, side, qty, limit_price)

        label = f"저점 - ${SECOND_ENTRY_OFFSET}" if side == "buy" else f"고점 + ${SECOND_ENTRY_OFFSET}"
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
    실제 포지션 수량이 봇이 예상하는 수량과 다르면
    수동 개입으로 판단합니다.
    허용 오차: 0.001 BTC (소수점 처리 오차)
    """
    with state_lock:
        if not state["active"]:
            return False
        expected = state["expected_qty"]

    actual = abs(float(position.get("contracts") or 0))
    diff   = abs(actual - expected)

    if diff > 0.001:
        log.warning(f"⚠️  수량 불일치 감지! 예상: {expected:.4f} BTC / 실제: {actual:.4f} BTC")
        return True
    return False


# ─────────────────────────────────────────
# 모니터링 스레드
# ─────────────────────────────────────────
def monitor_loop():
    log.info("🔍 모니터링 스레드 시작")
    while True:
        try:
            time.sleep(MONITOR_INTERVAL)
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
                position = get_position(exchange)
                if not position:
                    log.info("✅ 포지션 완전 청산 확인 → 봇 자동 재시작 대기 중")
                    reset_state(manual_pause=False)
                else:
                    log.info(f"⏸️  수동 중단 중... 포지션 청산 대기 ({abs(float(position.get('contracts', 0))):.4f} BTC 남음)")
                continue

            # ── 비활성 상태면 스킵 ──
            if not is_active:
                continue

            # ── 포지션 조회 ──
            position = get_position(exchange)

            # ── 포지션 없음: 모두 청산됨 ──
            if not position:
                log.info("📭 포지션 없음 → 미체결 주문 정리 후 초기화")
                cancel_all_open_orders(exchange)
                reset_state()
                continue

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
                    if tp_status == "closed":
                        log.info("🎉 1차 TP 전체 체결! → 2차 지정가 취소 → 처음부터 재시작")
                        cancel_order_safe(exchange, limit_order_id)
                        cancel_all_open_orders(exchange)
                        reset_state()
                        continue

                # 2차 지정가 체결 확인
                if limit_order_id:
                    limit_status = get_order_status(exchange, limit_order_id)
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

                        # 전체의 50%만 TP / 전체 100% SL
                        new_tp_id = set_tp_order(exchange, side, avg_price, total_qty)
                        new_sl_id = set_sl_order(exchange, side, avg_price, total_qty)

                        with state_lock:
                            state["entry_count"]    = 2
                            state["tp_order_id"]    = new_tp_id
                            state["sl_order_id"]    = new_sl_id
                            state["limit_order_id"] = None
                            state["expected_qty"]   = total_qty
                        continue

            # ════════════════════════════════════
            # 2차 진입 상태 모니터링
            # ════════════════════════════════════
            elif entry_count == 2:

                # TP 50% 체결 확인
                if tp_order_id:
                    tp_status = get_order_status(exchange, tp_order_id)
                    if tp_status == "closed":
                        log.info("🎉 2차 TP 50% 체결! → 나머지 50% + SL은 유지, 수동 관리")
                        # SL은 그대로 유지, 봇은 종료
                        reset_state()
                        continue

                # SL 체결 확인
                if sl_order_id:
                    sl_status = get_order_status(exchange, sl_order_id)
                    if sl_status == "closed":
                        log.info("📉 SL 체결 → 포지션 청산 완료")
                        cancel_all_open_orders(exchange)
                        reset_state()
                        continue

        except Exception as e:
            log.error(f"모니터링 루프 오류: {e}")


# ─────────────────────────────────────────
# 웹훅 엔드포인트
# ─────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    TradingView 알림 메시지 형식 (JSON):
    {
        "token":        "my_secret_token_1234",
        "direction":    "long" or "short",
        "candle_high":  {{high}},
        "candle_low":   {{low}}
    }
    """
    try:
        data = request.get_json()
        log.info(f"웹훅 수신: {data}")

        # ── 보안 토큰 검증 ──
        if not data or data.get("token") != WEBHOOK_TOKEN:
            log.warning("❌ 유효하지 않은 토큰")
            return jsonify({"error": "Unauthorized"}), 401

        direction   = data.get("direction", "").lower()
        candle_high = data.get("candle_high")
        candle_low  = data.get("candle_low")

        if direction not in ("long", "short"):
            return jsonify({"error": "direction 값이 잘못됨 (long/short)"}), 400
        if candle_high is None or candle_low is None:
            return jsonify({"error": "candle_high / candle_low 값이 필요합니다"}), 400

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

            # ── 이미 진행 중이면 무시 ──
            if state["active"]:
                msg = "이미 포지션 진행 중. 신호 무시."
                log.info(msg)
                return jsonify({"status": "skipped", "reason": msg}), 200

        log.info(f"📊 신호: {dir_kr}  캔들 고점: {candle_high}  저점: {candle_low}")

        # ── 거래소 연결 ──
        exchange = get_exchange()
        setup_leverage(exchange)

        # ── 잔고 조회 ──
        balance     = get_balance(exchange)
        usdt_to_use = balance * ENTRY_RATIO

        if usdt_to_use < 5:
            msg = f"잔고 부족 ({usdt_to_use:.2f} USDT)"
            log.warning(msg)
            return jsonify({"status": "skipped", "reason": msg}), 200

        # ── 1차 시장가 진입 ──
        order, entry_price = place_market_order(exchange, order_side, usdt_to_use)
        if not order or not entry_price:
            return jsonify({"status": "error", "reason": "1차 시장가 주문 실패"}), 500

        qty = float(order.get("filled") or order.get("amount") or 0)

        # ── 1차 TP 설정 (전체 수량 100%, SL 없음) ──
        tp_order_id = set_tp_order_full(exchange, order_side, entry_price, qty)

        # ── 2차 지정가 예약 ──
        limit_order_id, limit_qty = place_second_limit_order(
            exchange, order_side, candle_high, candle_low, usdt_to_use
        )

        second_price = (candle_low - SECOND_ENTRY_OFFSET) if order_side == "buy" \
                       else (candle_high + SECOND_ENTRY_OFFSET)

        # ── 상태 업데이트 ──
        with state_lock:
            state["active"]         = True
            state["manual_pause"]   = False
            state["entry_count"]    = 1
            state["side"]           = order_side
            state["tp_order_id"]    = tp_order_id
            state["limit_order_id"] = limit_order_id
            state["sl_order_id"]    = None
            state["expected_qty"]   = qty
            state["last_signal"]    = datetime.now().isoformat()

        log.info(f"🎯 1차 {dir_kr} 진입 완료")
        log.info(f"  진입가: {entry_price:.2f}  수량: {qty} BTC")
        log.info(f"  TP (100%): {qty:.4f} BTC → TP 체결 시 전량 익절 후 재시작")
        log.info(f"  2차 지정가 대기: {second_price:.2f} USDT")

        return jsonify({
            "status": "ok",
            "entry": "1차",
            "direction": dir_kr,
            "entry_price": entry_price,
            "qty": qty,
            "tp_qty": qty * TP_PARTIAL_RATIO,
            "sl_set": False,
            "second_limit_price": second_price,
        }), 200

    except Exception as e:
        log.error(f"웹훅 처리 오류: {e}")
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
# 상태 확인 엔드포인트
# ─────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    try:
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
            "position":     pos_info,
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"status": "ok", "message": "수동 초기화 완료"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
if __name__ == "__main__":
    log.info("🤖 BTC OI Delta 웹훅 봇 시작")

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=False)

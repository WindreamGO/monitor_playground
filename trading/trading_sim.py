"""
随机游走交易模拟器
------------------
持续模拟一个资产的价格随机游走，每 3~5 秒触发一次买/卖/平仓决策。
通过 prometheus_client 在 :8000/metrics 暴露以下指标：
  - trading_price         当前资产价格（Gauge）
  - trading_position      当前持仓手数（Gauge，正=多头，负=空头）
  - trading_pnl           当前未实现盈亏（Gauge）
  - trading_trades_total  累计成交笔数（Counter）

优雅退出：捕获 SIGTERM / SIGINT，主循环退出后程序正常返回 exit code 0。
"""

import random
import signal
import sys
import time

from prometheus_client import Counter, Gauge, start_http_server

# ── Prometheus 指标定义 ───────────────────────────────────────────────────────

# Gauge：可以任意升降的数值，适合表示"当前状态"
price_gauge = Gauge(
    "trading_price",
    "当前模拟资产价格（随机游走）",
)

position_gauge = Gauge(
    "trading_position",
    "当前持仓手数（正数=多头，负数=空头，0=空仓）",
)

pnl_gauge = Gauge(
    "trading_pnl",
    "当前未实现盈亏（Unrealized P&L）",
)

# Counter：只能递增的计数器，适合表示"累计发生了多少次"
# prometheus_client 中 Counter 的 .inc() 方法使其 +1
trades_counter = Counter(
    "trading_trades_total",
    "累计成交笔数",
)

# Gauge with labels：期权套利信号，每个标签组合代表一个合约
# 值 > 0 表示该合约当前存在套利机会，值 = 0 表示无信号
arbitrage_gauge = Gauge(
    "option_arbitrage_profit",
    "期权套利信号：当前预期收益（美元），0 = 无信号",
    ["contract_id", "strategy_type", "expiry"],
)

# ── 模拟状态变量 ──────────────────────────────────────────────────────────────

price: float = 100.0          # 初始资产价格
position: int = 0             # 当前持仓手数
entry_price: float = 0.0      # 开仓时的成交均价（用于计算盈亏）
_trade_count: int = 0         # 本地记录成交次数（用于控制台输出）
_next_trade_in: int = random.randint(3, 5)  # 距下一次交易的倒计时（秒）

# ── 期权合约池（64 个合约） ────────────────────────────────────────────────────
# 套利类型说明：
#   Put-Call Parity    — 认购/认沽平价套利
#   Box Spread         — 箱式价差套利
#   Calendar Spread    — 跨期套利
#   Vertical Spread    — 垂直价差套利
#   Strike Inversion   — 期权合约价格倒挂
_CONTRACTS: list[tuple[str, str, str]] = [
    # ── BTC（16 个合约）───────────────────────────────────────────────────────
    ("BTC-20260430-60000-C", "Put-Call Parity",  "2026-04-30"),
    ("BTC-20260430-65000-P", "Strike Inversion", "2026-04-30"),
    ("BTC-20260430-70000-C", "Calendar Spread",  "2026-04-30"),
    ("BTC-20260430-75000-P", "Box Spread",        "2026-04-30"),
    ("BTC-20260530-70000-C", "Vertical Spread",  "2026-05-30"),
    ("BTC-20260530-75000-P", "Put-Call Parity",  "2026-05-30"),
    ("BTC-20260530-80000-C", "Strike Inversion", "2026-05-30"),
    ("BTC-20260530-85000-P", "Calendar Spread",  "2026-05-30"),
    ("BTC-20260630-65000-C", "Box Spread",        "2026-06-30"),
    ("BTC-20260630-70000-P", "Put-Call Parity",  "2026-06-30"),
    ("BTC-20260630-80000-C", "Vertical Spread",  "2026-06-30"),
    ("BTC-20260630-90000-P", "Strike Inversion", "2026-06-30"),
    ("BTC-20260930-75000-C", "Calendar Spread",  "2026-09-30"),
    ("BTC-20260930-85000-P", "Box Spread",        "2026-09-30"),
    ("BTC-20260930-90000-C", "Put-Call Parity",  "2026-09-30"),
    ("BTC-20260930-95000-P", "Vertical Spread",  "2026-09-30"),
    # ── ETH（16 个合约）───────────────────────────────────────────────────────
    ("ETH-20260430-2800-C",  "Put-Call Parity",  "2026-04-30"),
    ("ETH-20260430-3000-P",  "Box Spread",        "2026-04-30"),
    ("ETH-20260430-3200-C",  "Calendar Spread",  "2026-04-30"),
    ("ETH-20260430-3400-P",  "Strike Inversion", "2026-04-30"),
    ("ETH-20260530-3000-C",  "Vertical Spread",  "2026-05-30"),
    ("ETH-20260530-3200-P",  "Put-Call Parity",  "2026-05-30"),
    ("ETH-20260530-3400-C",  "Box Spread",        "2026-05-30"),
    ("ETH-20260530-3600-P",  "Strike Inversion", "2026-05-30"),
    ("ETH-20260630-3200-C",  "Calendar Spread",  "2026-06-30"),
    ("ETH-20260630-3400-P",  "Put-Call Parity",  "2026-06-30"),
    ("ETH-20260630-3600-C",  "Vertical Spread",  "2026-06-30"),
    ("ETH-20260630-3800-P",  "Box Spread",        "2026-06-30"),
    ("ETH-20260930-3400-C",  "Strike Inversion", "2026-09-30"),
    ("ETH-20260930-3600-P",  "Calendar Spread",  "2026-09-30"),
    ("ETH-20260930-4000-C",  "Put-Call Parity",  "2026-09-30"),
    ("ETH-20260930-4200-P",  "Vertical Spread",  "2026-09-30"),
    # ── SOL（16 个合约）───────────────────────────────────────────────────────
    ("SOL-20260430-140-C",   "Calendar Spread",  "2026-04-30"),
    ("SOL-20260430-160-P",   "Put-Call Parity",  "2026-04-30"),
    ("SOL-20260430-180-C",   "Strike Inversion", "2026-04-30"),
    ("SOL-20260430-200-P",   "Box Spread",        "2026-04-30"),
    ("SOL-20260530-160-C",   "Vertical Spread",  "2026-05-30"),
    ("SOL-20260530-180-P",   "Put-Call Parity",  "2026-05-30"),
    ("SOL-20260530-200-C",   "Box Spread",        "2026-05-30"),
    ("SOL-20260530-220-P",   "Calendar Spread",  "2026-05-30"),
    ("SOL-20260630-180-C",   "Strike Inversion", "2026-06-30"),
    ("SOL-20260630-200-P",   "Put-Call Parity",  "2026-06-30"),
    ("SOL-20260630-220-C",   "Vertical Spread",  "2026-06-30"),
    ("SOL-20260630-240-P",   "Box Spread",        "2026-06-30"),
    ("SOL-20260930-200-C",   "Calendar Spread",  "2026-09-30"),
    ("SOL-20260930-220-P",   "Strike Inversion", "2026-09-30"),
    ("SOL-20260930-260-C",   "Put-Call Parity",  "2026-09-30"),
    ("SOL-20260930-280-P",   "Vertical Spread",  "2026-09-30"),
    # ── BNB（16 个合约）───────────────────────────────────────────────────────
    ("BNB-20260430-500-C",   "Box Spread",        "2026-04-30"),
    ("BNB-20260430-550-P",   "Strike Inversion", "2026-04-30"),
    ("BNB-20260430-600-C",   "Put-Call Parity",  "2026-04-30"),
    ("BNB-20260430-650-P",   "Calendar Spread",  "2026-04-30"),
    ("BNB-20260530-550-C",   "Vertical Spread",  "2026-05-30"),
    ("BNB-20260530-600-P",   "Box Spread",        "2026-05-30"),
    ("BNB-20260530-650-C",   "Strike Inversion", "2026-05-30"),
    ("BNB-20260530-700-P",   "Put-Call Parity",  "2026-05-30"),
    ("BNB-20260630-600-C",   "Calendar Spread",  "2026-06-30"),
    ("BNB-20260630-650-P",   "Vertical Spread",  "2026-06-30"),
    ("BNB-20260630-700-C",   "Box Spread",        "2026-06-30"),
    ("BNB-20260630-750-P",   "Strike Inversion", "2026-06-30"),
    ("BNB-20260930-650-C",   "Put-Call Parity",  "2026-09-30"),
    ("BNB-20260930-700-P",   "Calendar Spread",  "2026-09-30"),
    ("BNB-20260930-800-C",   "Vertical Spread",  "2026-09-30"),
    ("BNB-20260930-850-P",   "Box Spread",        "2026-09-30"),
]

# 套利信号状态：记录每个合约当前的预期收益，0.0 = 无信号
_arb_state: dict[str, float] = {cid: 0.0 for cid, _, _ in _CONTRACTS}

# ── 优雅退出 ──────────────────────────────────────────────────────────────────

_running: bool = True


def _handle_signal(signum: int, frame) -> None:
    """
    捕获 SIGTERM（docker stop）和 SIGINT（Ctrl+C），通知主循环退出。
    因为用了 ENTRYPOINT exec 形式，Python 是 PID 1，能直接接收这些信号。
    """
    global _running
    sig_name = signal.Signals(signum).name
    print(f"\n[SIGNAL] 收到 {sig_name}，准备优雅退出...", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── 交易决策 ──────────────────────────────────────────────────────────────────


def _execute_trade() -> None:
    """随机触发一次买入、卖出或平仓操作。"""
    global position, entry_price, _trade_count

    action = random.choice(["buy", "sell", "close"])

    if action == "close" and position != 0:
        print(
            f"  → 平仓  持仓={position:+d} 手  "
            f"开仓均价={entry_price:.4f}  当前价={price:.4f}  "
            f"已实现盈亏={(price - entry_price) * position:+.4f}",
            flush=True,
        )
        position = 0
        entry_price = 0.0
        trades_counter.inc()
        _trade_count += 1

    elif action == "buy" and position <= 0:
        lots = random.randint(1, 3)
        position = lots
        entry_price = price
        print(f"  → 买入  {lots} 手 @ {price:.4f}", flush=True)
        trades_counter.inc()
        _trade_count += 1

    elif action == "sell" and position >= 0:
        lots = random.randint(1, 3)
        position = -lots
        entry_price = price
        print(f"  → 卖出  {lots} 手 @ {price:.4f}", flush=True)
        trades_counter.inc()
        _trade_count += 1


# ── 套利信号更新 ──────────────────────────────────────────────────────────────


def _update_arbitrage_signals() -> None:
    """每 tick 更新所有合约的套利信号状态：随机激活 / 随机游走 / 随机消失。"""
    for contract_id, strategy_type, expiry in _CONTRACTS:
        current = _arb_state[contract_id]
        if current <= 0.0:
            # 无信号：5% 概率激活，初始收益随机落在 $30 ~ $600
            if random.random() < 0.05:
                _arb_state[contract_id] = random.uniform(30.0, 600.0)
        else:
            # 有信号：随机游走（微弱向零漂移），2% 概率突然消失
            if random.random() < 0.02:
                _arb_state[contract_id] = 0.0
            else:
                new_val = current + random.gauss(-1.5, 18.0)
                _arb_state[contract_id] = max(new_val, 0.0)
        arbitrage_gauge.labels(
            contract_id=contract_id,
            strategy_type=strategy_type,
            expiry=expiry,
        ).set(_arb_state[contract_id])


# ── 主循环 ────────────────────────────────────────────────────────────────────


def main() -> None:
    global price, position, entry_price, _next_trade_in

    # 启动 Prometheus HTTP 服务器，监听 0.0.0.0:8000
    # 访问 http://localhost:8000/metrics 可看到 Prometheus 文本格式的指标
    start_http_server(8000)
    print("[INIT] Prometheus metrics server 已启动，监听 :8000/metrics", flush=True)
    print("[INIT] 交易模拟器开始运行，每秒输出一次状态...\n", flush=True)

    tick = 0

    while _running:
        tick += 1

        # 价格随机游走：以正态分布模拟每秒的价格变动
        # gauss(均值=0, 标准差=0.005) → 每秒波动约 ±0.5%
        change = random.gauss(0, 0.005)
        price = max(price * (1 + change), 0.01)  # 保底 0.01，防止归零

        # 计算未实现盈亏：(当前价 - 开仓均价) × 持仓手数
        # position=0 时 pnl 固定为 0
        pnl = (price - entry_price) * position if position != 0 else 0.0

        # 将最新数值写入 Prometheus Gauge
        price_gauge.set(price)
        position_gauge.set(position)
        pnl_gauge.set(pnl)

        # 更新期权套利信号看板
        _update_arbitrage_signals()

        # 倒计时触发交易
        _next_trade_in -= 1
        if _next_trade_in <= 0:
            _execute_trade()
            _next_trade_in = random.randint(3, 5)

        # 控制台持续输出当前状态
        direction = "多头" if position > 0 else ("空头" if position < 0 else "空仓")
        print(
            f"[Tick {tick:>5}]  "
            f"价格={price:>9.4f}  "
            f"持仓={position:>+3d}({direction})  "
            f"盈亏={pnl:>+9.4f}  "
            f"成交次数={_trade_count:>4d}",
            flush=True,
        )

        time.sleep(1)

    print("\n[EXIT] 交易模拟器已停止。", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()

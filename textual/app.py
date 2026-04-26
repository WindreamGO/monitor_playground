"""
交易监控 Textual App
--------------------
每秒从 Prometheus HTTP API 查询交易指标，在浏览器中以 TUI 形式展示：

  顶部摘要面板
  ├── PRICE     当前模拟资产价格
  ├── POSITION  当前持仓手数（正=多头，负=空头，0=空仓）
  ├── P&L       未实现盈亏
  └── TRADES    累计成交笔数

  底部表格
  └── 当前有效的期权套利信号（profit > 0），按收益降序排列

依赖：textual、httpx
"""

import asyncio
import os

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Label, Static

# ── 配置 ──────────────────────────────────────────────────────────────────────

PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://prometheus:9090/api/v1/query",
)

# ── 自定义 Widget ──────────────────────────────────────────────────────────────


class MetricCard(Static):
    """
    单个指标卡片：显示一个标签（小字）和一个数值（大字粗体）。

    初始内容为 "—"（等待数据），数据就绪后调用 set_value() 更新。
    """

    def __init__(self, label: str, **kwargs) -> None:
        super().__init__(f"[dim]{label}[/]\n—", **kwargs)
        self._label = label

    def set_value(self, value: str, color: str = "white") -> None:
        """更新显示数值，color 为 Rich 颜色名称。"""
        self.update(f"[dim]{self._label}[/]\n[bold {color}]{value}[/]")


# ── 主应用 ────────────────────────────────────────────────────────────────────


class TradingMonitorApp(App):
    """
    交易监控主应用，通过 textual-serve 在浏览器中运行。

    按 Q 退出，按 R 立即触发一次刷新。
    """

    TITLE = "Trading Monitor"
    SUB_TITLE = "Real-time · Powered by Prometheus"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh Now"),
    ]

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="metrics-row"):
            yield MetricCard("PRICE", id="card-price")
            yield MetricCard("POSITION", id="card-position")
            yield MetricCard("P&L", id="card-pnl")
            yield MetricCard("TRADES", id="card-trades")
        yield Label("  Arbitrage Signals  (active only · sorted by profit)", id="signals-title")
        yield DataTable(id="signals-table")
        yield Footer()

    # ── 挂载后初始化 ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        table = self.query_one("#signals-table", DataTable)
        table.add_columns("Contract ID", "Strategy", "Expiry", "Expected Profit ($)")
        table.cursor_type = "row"
        # 立即拉取一次，然后每秒定时刷新
        self.fetch_and_update()
        self.set_interval(1.0, self.fetch_and_update)

    # ── 数据拉取（异步 Worker，exclusive=True 防止并发重叠）────────────────────

    @work(exclusive=True)
    async def fetch_and_update(self) -> None:
        """并行查询 Prometheus 所有指标，然后刷新 UI。"""
        async with httpx.AsyncClient(timeout=3.0) as client:
            responses = await asyncio.gather(
                client.get(PROMETHEUS_URL, params={"query": "trading_price"}),
                client.get(PROMETHEUS_URL, params={"query": "trading_position"}),
                client.get(PROMETHEUS_URL, params={"query": "trading_pnl"}),
                client.get(PROMETHEUS_URL, params={"query": "trading_trades_total"}),
                client.get(
                    PROMETHEUS_URL,
                    params={"query": "option_arbitrage_profit > 0"},
                ),
                return_exceptions=True,
            )

        # ── 解析辅助函数 ──────────────────────────────────────────────────────

        def scalar(resp) -> float | None:
            """从 Prometheus instant-query 响应中提取第一个标量值。"""
            if isinstance(resp, Exception):
                return None
            try:
                results = resp.json()["data"]["result"]
                return float(results[0]["value"][1]) if results else None
            except Exception:
                return None

        def series(resp) -> list[dict]:
            """从 Prometheus instant-query 响应中提取所有带标签的时序。"""
            if isinstance(resp, Exception):
                return []
            try:
                return [
                    {
                        "contract_id": item["metric"].get("contract_id", ""),
                        "strategy_type": item["metric"].get("strategy_type", ""),
                        "expiry": item["metric"].get("expiry", ""),
                        "profit": float(item["value"][1]),
                    }
                    for item in resp.json()["data"]["result"]
                ]
            except Exception:
                return []

        price_r, pos_r, pnl_r, trades_r, arb_r = responses

        price = scalar(price_r)
        position = scalar(pos_r)
        pnl = scalar(pnl_r)
        trades = scalar(trades_r)
        arb_signals = series(arb_r)

        # ── 更新摘要卡片 ──────────────────────────────────────────────────────

        self.query_one("#card-price", MetricCard).set_value(
            f"${price:.2f}" if price is not None else "—"
        )

        if position is not None:
            pos_int = int(position)
            sign = "+" if pos_int > 0 else ""
            color = "green" if pos_int > 0 else ("red" if pos_int < 0 else "white")
            self.query_one("#card-position", MetricCard).set_value(
                f"{sign}{pos_int}", color
            )
        else:
            self.query_one("#card-position", MetricCard).set_value("—")

        if pnl is not None:
            pnl_color = "green" if pnl >= 0 else "red"
            self.query_one("#card-pnl", MetricCard).set_value(f"${pnl:.2f}", pnl_color)
        else:
            self.query_one("#card-pnl", MetricCard).set_value("—")

        self.query_one("#card-trades", MetricCard).set_value(
            f"{int(trades)}" if trades is not None else "—"
        )

        # ── 更新套利信号表格 ──────────────────────────────────────────────────

        table = self.query_one("#signals-table", DataTable)
        table.clear()
        for sig in sorted(arb_signals, key=lambda x: -x["profit"]):
            table.add_row(
                sig["contract_id"],
                sig["strategy_type"],
                sig["expiry"],
                f"${sig['profit']:.2f}",
            )

    # ── Action ────────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        """绑定到 R 键：立即触发一次数据刷新。"""
        self.fetch_and_update()


# ── 入口（本地直接运行时使用）────────────────────────────────────────────────

if __name__ == "__main__":
    TradingMonitorApp().run()

import time
from queue import Queue
import pandas as pd

# 导入我们重构的组件和类型
from events import MarketEvent, SignalEvent, OrderEvent, FillEvent
from backtest_components import DuckDBDataHandler, Portfolio, SimulatedExecutionHandler
from backtest_gateway import BacktestTradingGateway
from strategy import Strategy

# 导入一个重构后的策略作为示例
from strategies.dual_ma_crossover_strategy import DualMaCrossoverStrategy

class EventDrivenBacktester:
    """
    事件驱动回测引擎主类。
    负责初始化所有组件，并运行主事件循环。
    """
    def __init__(self, strategy_class, symbol: str, timeframe: str, start_date: str, end_date: str, initial_cash: float):
        self.strategy_class = strategy_class
        self.symbol = symbol
        self.timeframe = timeframe
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = initial_cash

        self.events = Queue()
        self.strategy = None

        self._setup_components()

    def _setup_components(self):
        """初始化所有回测组件。"""
        print("Initializing backtest components...")
        
        # 1. 数据处理器 (Data Handler)
        self.data_handler = DuckDBDataHandler(self.events, [self.symbol], self.timeframe, self.start_date, self.end_date)

        # 2. 投资组合管理器 (Portfolio)
        self.portfolio = Portfolio(self.events, self.data_handler, self.initial_cash)

        # 3. 执行处理器 (Execution Handler)
        self.execution_handler = SimulatedExecutionHandler(self.events, self.data_handler)

        # 4. 回测交易网关 (Backtest Trading Gateway)
        backtest_gateway = BacktestTradingGateway(self.events, self.portfolio, self.data_handler)

        # 从策略类中提取默认参数
        strategy_params = {k: v['default'] for k, v in self.strategy_class.strategy_params_config.items()}

        # 5. 策略实例 (Strategy)
        # 注意：我们将回测网关和参数注入到策略中
        self.strategy = self.strategy_class(backtest_gateway, self.symbol, self.timeframe, params=strategy_params)
        print("Components initialized successfully.")

    def run_backtest(self):
        """运行主事件循环。"""
        print(f"\n--- Running Backtest for {self.strategy.strategy_name} ---")
        print(f"Symbol: {self.symbol} | Timeframe: {self.timeframe} | Period: {self.start_date} to {self.end_date}\n")
        
        self.strategy.on_init()

        # 启动事件循环，由第一个MarketEvent驱动
        self.data_handler.update_bars()

        while True:
            if not self.data_handler.continue_backtest and self.events.empty():
                break

            try:
                event = self.events.get(block=False)
            except self.events.empty():
                # 如果事件队列为空，但数据还没完，就继续获取数据
                self.data_handler.update_bars()
            else:
                if event is not None:
                    if isinstance(event, MarketEvent):
                        # 市场事件：更新投资组合，然后运行策略逻辑
                        print(f"-- Market Event: {pd.to_datetime(event.time, unit='s')} --")
                        self.portfolio.on_bar(event)
                        self.strategy.on_bar(event)
                        # 处理完市场事件后，立即请求下一个数据点
                        self.data_handler.update_bars()

                    elif isinstance(event, SignalEvent):
                        # 信号事件：由投资组合处理
                        print(f"-- Signal Event: {event.direction} {event.symbol} --")
                        self.portfolio.on_signal(event)

                    elif isinstance(event, OrderEvent):
                        # 订单事件：由执行处理器处理
                        print(f"-- Order Event: {event.direction} {event.quantity} {event.symbol} --")
                        self.execution_handler.execute_order(event)

                    elif isinstance(event, FillEvent):
                        # 成交事件：由投资组合处理
                        print(f"-- Fill Event: {event.direction} {event.quantity} {event.symbol} at {event.fill_price:.5f} --")
                        self.portfolio.on_fill(event)

        self.strategy.on_deinit()
        return self.generate_report()

    def generate_report(self):
        """生成并返回最终的回测报告字符串。"""
        final_equity = self.portfolio.equity
        total_return = (final_equity / self.initial_cash - 1) * 100

        report = (
            "\n--- Backtest Finished ---\n"
            f"Initial Cash: {self.initial_cash:,.2f}\n"
            f"Final Equity:   {final_equity:,.2f}\n"
            f"Total Return:   {total_return:.2f}%\n"
            "-------------------------\n"
        )
        print(report)
        return report

# --- 主程序入口 ---
if __name__ == '__main__':
    # 配置回测参数
    backtest_config = {
        "strategy_class": DualMaCrossoverStrategy,
        "symbol": "EURUSD",
        "timeframe": "H1",
        "start_date": "2023-01-01",
        "end_date": "2023-03-31",
        "initial_cash": 10000.0
    }

    # 创建并运行回测
    backtester = EventDrivenBacktester(**backtest_config)
    backtester.run_backtest()

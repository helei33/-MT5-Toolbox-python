import unittest
from unittest.mock import Mock, MagicMock
from queue import Queue
import pandas as pd

# 导入需要测试的组件和事件
from backtest_components import Portfolio, SimulatedExecutionHandler
from events import SignalEvent, OrderEvent, FillEvent, MarketEvent

class TestPortfolio(unittest.TestCase):
    """测试 Portfolio 组件"""

    def setUp(self):
        """在每个测试前运行，设置一个干净的环境"""
        self.events_queue = Queue()
        self.mock_data_handler = MagicMock()
        self.initial_cash = 10000.0
        self.portfolio = Portfolio(self.events_queue, self.mock_data_handler, self.initial_cash)

    def test_on_signal_creates_order(self):
        """测试：当收到SignalEvent时，应生成一个OrderEvent"""
        signal = SignalEvent(symbol='EURUSD', direction='BUY')
        self.portfolio.on_signal(signal)
        
        # 检查事件队列中是否有一个OrderEvent
        self.assertFalse(self.events_queue.empty(), "事件队列不应为空")
        event = self.events_queue.get()
        self.assertIsInstance(event, OrderEvent, "事件应为OrderEvent类型")
        self.assertEqual(event.symbol, 'EURUSD')
        self.assertEqual(event.direction, 'BUY')
        self.assertEqual(event.order_type, 'MKT')

    def test_on_fill_opens_new_position(self):
        """测试：当收到FillEvent时，应正确开立一个新仓位"""
        fill = FillEvent(symbol='EURUSD', direction='BUY', quantity=0.1, fill_price=1.1000, commission=1.0)
        self.portfolio.on_fill(fill)

        # 检查现金和持仓状态
        self.assertEqual(self.portfolio.cash, self.initial_cash - 1.0, "现金应扣除手续费")
        self.assertIn('EURUSD', self.portfolio.positions, "应创建EURUSD的持仓")
        
        position = self.portfolio.positions['EURUSD']
        self.assertEqual(position['volume'], 0.1)
        self.assertEqual(position['price_open'], 1.1000)
        self.assertEqual(position['type'], 0, "持仓类型应为买入(0)")

    def test_on_bar_updates_equity(self):
        """测试：当收到MarketEvent时，应正确更新持仓盈亏和总净值"""
        # 1. 先开一个仓位
        fill = FillEvent(symbol='EURUSD', direction='BUY', quantity=0.1, fill_price=1.1000, commission=1.0)
        self.portfolio.on_fill(fill)
        self.assertEqual(self.portfolio.equity, self.initial_cash, "开仓后，初始净值应等于初始资金")

        # 2. 模拟一个新的市场数据
        new_bar_data = pd.Series({'close': 1.1050})
        self.mock_data_handler.get_latest_bar.return_value = new_bar_data
        market = MarketEvent(symbol='EURUSD', time=123456789)

        # 3. 触发on_bar事件
        self.portfolio.on_bar(market)

        # 4. 检查净值是否更新
        # 正确的简化计算: (1.1050 - 1.1000) * 0.1 * 100000 = 50.0
        expected_profit = 50.0
        expected_equity = self.initial_cash - 1.0 + expected_profit # 10000 - 1 + 50 = 10049.0
        self.assertAlmostEqual(self.portfolio.equity, expected_equity, places=2, msg="净值应反映浮动盈亏")


class TestSimulatedExecutionHandler(unittest.TestCase):
    """测试 SimulatedExecutionHandler 组件"""

    def setUp(self):
        self.events_queue = Queue()
        self.mock_data_handler = MagicMock()
        self.execution_handler = SimulatedExecutionHandler(self.events_queue, self.mock_data_handler, commission_per_trade=1.5, slippage_points=2)

    def test_execute_mkt_buy_order(self):
        """测试：执行一个市价买单"""
        # 模拟下一根K线的数据
        next_bar = pd.Series({'open': 1.2000, 'close': 1.2050})
        self.mock_data_handler.get_latest_bar.return_value = next_bar

        # 创建一个订单事件
        order = OrderEvent(symbol='GBPUSD', order_type='MKT', direction='BUY', quantity=0.5)
        self.execution_handler.execute_order(order)

        # 检查队列中的成交事件
        self.assertFalse(self.events_queue.empty(), "事件队列不应为空")
        event = self.events_queue.get()
        self.assertIsInstance(event, FillEvent, "事件应为FillEvent类型")

        # 检查成交价格是否正确（开盘价 + 滑点）
        # 假设 point = 0.00001, slippage = 2 * 0.00001 = 0.00002
        expected_price = 1.2000 + 0.00002
        self.assertAlmostEqual(event.fill_price, expected_price, places=5)
        self.assertEqual(event.commission, 1.5, "手续费应正确")
        self.assertEqual(event.direction, 'BUY')
        self.assertEqual(event.quantity, 0.5)


if __name__ == '__main__':
    unittest.main()

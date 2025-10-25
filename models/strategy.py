from abc import ABC, abstractmethod
from trading_gateway import TradingGateway
from .events import MarketEvent

class Strategy(ABC):
    """
    统一的策略抽象基类。

    策略通过依赖注入的方式接收一个 `TradingGateway` 对象，
    从而实现与执行环境（实时或回测）的完全解耦。
    """
    def __init__(self, gateway: TradingGateway, symbol: str, timeframe: str, params: dict = None):
        self.gateway = gateway
        self.symbol = symbol
        self.timeframe = timeframe
        self.params = params if params is not None else {}
        self._init_mt5_constants()

    def on_init(self):
        """在策略开始时调用，用于初始化。"""
        pass

    @abstractmethod
    def on_bar(self, event: MarketEvent):
        """
        在每个市场事件（新K线）上调用。
        这是策略的核心逻辑所在。
        """
        pass

    def on_deinit(self):
        """在策略结束时调用，用于清理。"""
        pass

    def _init_mt5_constants(self):
        """提供MT5常量以便策略代码兼容。"""
        # 在实际使用中，这些常量可以从一个单独的`constants.py`模块导入
        # 为了方便，暂时在这里定义
        # 订单类型
        self.ORDER_TYPE_BUY = 0
        self.ORDER_TYPE_SELL = 1
        # 交易操作
        self.TRADE_ACTION_DEAL = 1
        # 时间周期 (示例)
        self.TIMEFRAME_M1 = 1
        self.TIMEFRAME_H1 = 16385
        self.TIMEFRAME_D1 = 16408
        # 订单填充策略
        self.ORDER_FILLING_IOC = 1
        # 订单有效时间
        self.ORDER_TIME_GTC = 0

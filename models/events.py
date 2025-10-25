from dataclasses import dataclass
from typing import Literal, Optional, Any

@dataclass
class Event:
    """所有事件的基类。"""
    pass

@dataclass
class MarketEvent(Event):
    """
    市场事件，表示一个新的市场数据点（例如，一根新的K线）已经到达。
    这是驱动策略逻辑运行的“心跳”。
    """
    symbol: str
    time: Optional[int] = None
    timeframe: Optional[str] = None
    bar_data: Optional[Any] = None
    type: Literal['MARKET'] = 'MARKET'

@dataclass
class SignalEvent(Event):
    """
    信号事件，由策略（Strategy）生成，表达一个交易意图。
    """
    symbol: str
    direction: Literal['BUY', 'SELL', 'CLOSE']
    type: Literal['SIGNAL'] = 'SIGNAL'
    strength: float = 1.0  # 信号强度，可用于仓位管理

@dataclass
class OrderEvent(Event):
    """
    订单事件，由投资组合管理器（Portfolio）在评估信号后生成。
    它包含了具体的交易指令，将被发送给执行处理器。
    """
    symbol: str
    order_type: Literal['MKT', 'LMT', 'STP'] # 市价、限价、止损
    direction: Literal['BUY', 'SELL']
    quantity: float
    type: Literal['ORDER'] = 'ORDER'
    price: float = 0.0 # 对于LMT/STP订单的价格

@dataclass
class FillEvent(Event):
    """
    成交事件，由执行处理器（ExecutionHandler）在模拟订单成交后生成。
    它包含了成交的详细信息，用于更新投资组合的状态。
    """
    symbol: str
    direction: Literal['BUY', 'SELL']
    quantity: float
    fill_price: float
    type: Literal['FILL'] = 'FILL'
    commission: float = 0.0
    slippage: float = 0.0
from dataclasses import dataclass
from typing import Tuple
import numpy as np

@dataclass
class AccountInfo:
    """模拟 mt5.account_info() 返回的 namedtuple"""
    login: int
    balance: float
    equity: float
    profit: float
    margin: float
    margin_free: float
    margin_level: float
    currency: str

@dataclass
class SymbolInfo:
    """模拟 mt5.symbol_info() 返回的 namedtuple"""
    name: str
    point: float
    spread: int
    digits: int
    trade_mode: int # 例如 mt5.SYMBOL_TRADE_MODE_FULL
    volume_min: float
    volume_max: float
    volume_step: float

@dataclass
class Tick:
    """模拟 mt5.symbol_info_tick() 返回的 namedtuple"""
    time: int
    bid: float
    ask: float
    last: float
    volume: int

@dataclass
class TradeResult:
    """模拟 mt5.order_send() 返回的 namedtuple"""
    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    comment: str

@dataclass
class PositionInfo:
    """模拟 mt5.positions_get() 返回的元组中的 namedtuple"""
    ticket: int
    symbol: str
    volume: float
    price_open: float
    profit: float
    type: int # 0 for buy, 1 for sell
    time: int
    magic: int

# MT5 K线数据的NumPy结构化数组类型定义
# 这有助于确保DataHandler和回测引擎使用一致的数据格式
RatesDTO = np.dtype([
    ('time', 'i8'),
    ('open', 'f8'),
    ('high', 'f8'),
    ('low', 'f8'),
    ('close', 'f8'),
    ('tick_volume', 'i8'),
    ('spread', 'i4'),
    ('real_volume', 'i8')
])

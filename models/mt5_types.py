from dataclasses import dataclass
from typing import Tuple, List, Optional
import numpy as np
import MetaTrader5 as mt5
import logging

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

class MT5Connection:
    """
    封装单个MT5账户的连接。
    注意：MetaTrader5库在单个进程中只支持一个全局连接。
    这个类旨在隔离连接逻辑，但在多账户同时操作时，
    需要由调用方（如AccountService）来管理连接的切换。
    """
    def __init__(self, login: int, password: str, server: str, logger: logging.Logger):
        self.login = login
        self.password = password
        self.server = server
        self.logger = logger
        # 每个实例都使用全局的MetaTrader5包
        # 注意：这在多线程中需要一个锁来保证线程安全
        self.mt5 = mt5

    def connect(self) -> bool:
        """初始化与此账户的连接"""
        # 每次连接都重新初始化，这会覆盖之前的任何连接
        if not self.mt5.initialize(
            login=self.login,
            password=self.password,
            server=self.server
        ):
            self.logger.error(f"MT5 initialize() 失败 for account {self.login}: {self.mt5.last_error()}")
            return False
        return True

    def shutdown(self):
        """关闭MT5连接"""
        self.mt5.shutdown()

    def get_account_info(self) -> Optional[AccountInfo]:
        """获取账户信息"""
        info = self.mt5.account_info()
        if info:
            # 将MT5的namedtuple转换为我们的dataclass
            return AccountInfo(**info._asdict())
        self.logger.warning(f"无法获取账户信息 for {self.login}")
        return None

    def get_positions(self) -> Optional[List['Position']]:
        """获取持仓"""
        positions = self.mt5.positions_get(login=self.login)
        if positions is None:
            # 检查错误
            if self.mt5.last_error()[0] != 1: # 忽略"no positions"的"错误"
                self.logger.error(f"positions_get failed for account {self.login}: {self.mt5.last_error()}")
            return [] # 返回空列表表示没有持仓或出错
        return [Position(**p._asdict()) for p in positions]

    def create_market_order(self, symbol: str, volume: float, order_type: int, magic: int, comment: str) -> Optional[TradeResult]:
        """创建市价单"""
        tick = self.mt5.symbol_info_tick(symbol)
        if not tick:
            self.logger.error(f"无法获取 {symbol} 的价格信息。")
            return None

        price = tick.ask if order_type == self.mt5.ORDER_TYPE_BUY else tick.bid

        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "magic": magic,
            "comment": comment,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "deviation": 20, # 滑点
        }
        
        result = self.mt5.order_send(request)
        if result:
            if result.retcode != self.mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"订单执行失败 for {self.login}: {result.comment} (retcode={result.retcode})")
            return TradeResult(**result._asdict())
        self.logger.error(f"order_send 失败 for {self.login}: {self.mt5.last_error()}")
        return None

# 为了兼容CopierService中的类型提示，重命名PositionInfo
Position = PositionInfo


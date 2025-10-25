from abc import ABC, abstractmethod
from typing import Optional, Tuple, Dict, Any
import numpy as np

from models.mt5_types import AccountInfo, SymbolInfo, Tick, TradeResult, Position, RatesDTO

class TradingGateway(ABC):
    """
    交易接口的抽象基类，定义了策略与执行环境（实时或回测）交互的统一契约。
    所有方法签名和返回类型都力求与 MetaTrader5 官方库兼容。
    """

    @abstractmethod
    def initialize(self, **kwargs) -> bool:
        """
        初始化连接。
        对于实时交易，这将连接到MT5终端。
        对于回测，这将设置回测引擎的初始状态。
        """
        pass

    @abstractmethod
    def shutdown(self) -> bool:
        """
        关闭连接或结束会话。
        """
        pass

    @abstractmethod
    def account_info(self) -> Optional[AccountInfo]:
        """
        获取账户信息。
        :return: AccountInfo 数据类实例或 None。
        """
        pass

    @abstractmethod
    def symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """
        获取交易品种的信息。
        :param symbol: 交易品种名称。
        :return: SymbolInfo 数据类实例或 None。
        """
        pass

    @abstractmethod
    def symbol_info_tick(self, symbol: str) -> Optional[Tick]:
        """
        获取交易品种的最新报价。
        在回测中，这通常是当前K线的收盘价或模拟的tick。
        :param symbol: 交易品种名称。
        :return: Tick 数据类实例或 None。
        """
        pass

    @abstractmethod
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[np.ndarray]:
        """
        从指定位置获取历史K线数据。
        返回一个与MT5 API格式完全相同的NumPy结构化数组。
        :param symbol: 交易品种。
        :param timeframe: 时间周期（例如 mt5.TIMEFRAME_H1）。
        :param start_pos: 起始位置，0是当前K线。
        :param count: 要获取的K线数量。
        :return: NumPy数组，其dtype应为RatesDTO。
        """
        pass

    @abstractmethod
    def positions_get(self, symbol: Optional[str] = None) -> Tuple[Position, ...]:
        """
        获取当前持仓。
        :param symbol: 如果指定，则只返回该品种的持仓。
        :return: 一个包含 PositionInfo 实例的元组。
        """
        pass

    @abstractmethod
    def order_send(self, request: Dict[str, Any]) -> Optional[TradeResult]:
        """
        发送交易订单。
        在实时交易中，这会向经纪商发送真实订单。
        在回测中，这会将一个交易意图（信号或订单事件）放入事件队列。
        :param request: 一个与MT5 API格式兼容的字典。
        :return: TradeResult 数据类实例或 None。
        """
        pass

    @abstractmethod
    def order_calc_margin(self, action: int, symbol: str, volume: float, price: float) -> Optional[float]:
        """
        计算指定交易订单所需的保证金。
        :return: 所需的保证金金额。
        """
        pass

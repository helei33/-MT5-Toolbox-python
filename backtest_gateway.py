from queue import Queue
from typing import Optional, Tuple, Dict, Any
import numpy as np

from trading_gateway import TradingGateway
from mt5_types import AccountInfo, SymbolInfo, Tick, TradeResult, PositionInfo
from events import SignalEvent
from backtest_components import Portfolio, DataHandler

# 模拟MT5返回码
TRADE_RETCODE_DONE = 10009

class BacktestTradingGateway(TradingGateway):
    """
    回测环境的网关实现。
    它将策略代码的API调用转换为回测系统中的事件，
    或从回测组件（如Portfolio）中查询状态来响应API调用。
    """
    def __init__(self, events_queue: Queue, portfolio: Portfolio, data_handler: DataHandler):
        self.events = events_queue
        self.portfolio = portfolio
        self.data_handler = data_handler

    def initialize(self, **kwargs) -> bool:
        # 在回测中，初始化由回测引擎主循环处理，这里直接返回成功
        return True

    def shutdown(self) -> bool:
        # 在回测中，关闭由回测引擎主循环处理，这里直接返回成功
        return True

    def account_info(self) -> Optional[AccountInfo]:
        """从Portfolio组件获取账户信息。"""
        info_dict = self.portfolio.get_account_info()
        return AccountInfo(
            login=12345, # 模拟的登录ID
            currency="USD", # 模拟的货币
            **info_dict
        )

    def symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """获取模拟的交易品种信息。"""
        # TODO: 这个信息应该由一个专门的SymbolManager或DataHandler提供
        # 简化处理
        point = 0.00001
        if "JPY" in symbol:
            point = 0.001
        
        return SymbolInfo(
            name=symbol,
            point=point,
            spread=5, # 模拟点差
            digits=5,
            trade_mode=0 # SYMBOL_TRADE_MODE_FULL
        )

    def symbol_info_tick(self, symbol: str) -> Optional[Tick]:
        """从DataHandler获取当前K线的模拟报价。"""
        bar = self.data_handler.get_latest_bar(symbol)
        if bar is not None:
            return Tick(
                time=int(bar.name.timestamp()),
                bid=bar['close'],
                ask=bar['close'],
                last=bar['close'],
                volume=int(bar['tick_volume'])
            )
        return None

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[np.ndarray]:
        """从DataHandler获取历史K线数据。"""
        # TODO: 实现更精确的基于位置的访问
        # 简化实现：直接返回所有数据的一部分
        if self.data_handler.all_data is not None:
            return self.data_handler.all_data.iloc[-count:].to_records(index=False)
        return None

    def positions_get(self, symbol: Optional[str] = None) -> Tuple[PositionInfo, ...]:
        """从Portfolio组件获取持仓信息。"""
        positions_list = self.portfolio.get_positions_info(symbol)
        # 将字典列表转换为PositionInfo元组
        return tuple(PositionInfo(**p) for p in positions_list)

    def order_send(self, request: Dict[str, Any]) -> Optional[TradeResult]:
        """
        将交易请求转换为一个SignalEvent并放入事件队列。
        这是策略与回测引擎交互的核心。
        """
        action = request.get("action")
        symbol = request.get('symbol')
        volume = request.get('volume')
        order_type = request.get('type')

        # TODO: 处理更复杂的订单类型和操作
        if action == 1: # TRADE_ACTION_DEAL
            direction = 'BUY' if order_type == 0 else 'SELL'
            
            # 创建信号事件
            signal = SignalEvent(
                symbol=symbol,
                direction=direction,
                strength=1.0 # strength可以用来决定手数
            )
            self.events.put(signal)

            # 立即返回一个模拟的成功回执
            # 注意：这不代表订单已成交，只是表示请求已被接受
            return TradeResult(
                retcode=TRADE_RETCODE_DONE,
                deal=0, # 在回测中，deal/order ID由Portfolio/ExecutionHandler生成
                order=0,
                volume=volume,
                price=0, # 价格在成交时确定
                comment="Request accepted by backtest engine"
            )
        return None

    def order_calc_margin(self, action: int, symbol: str, volume: float, price: float) -> Optional[float]:
        """从Portfolio组件计算保证金。"""
        # TODO: 在Portfolio中实现这个计算
        # 简化实现
        return (volume * 100000 * price) / self.portfolio.leverage

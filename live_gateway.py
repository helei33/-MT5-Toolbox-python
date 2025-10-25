import MetaTrader5 as mt5
from typing import Optional, Tuple, Dict, Any
import numpy as np

from trading_gateway import TradingGateway
from mt5_types import AccountInfo, SymbolInfo, Tick, TradeResult, PositionInfo, RatesDTO

class LiveTradingGateway(TradingGateway):
    """
    连接真实MetaTrader 5环境的网关实现。

    它封装了对 `MetaTrader5` 库的直接调用，并将返回的数据
    转换为项目内部统一的 `dataclass` 类型。
    """

    def initialize(self, **kwargs) -> bool:
        """
        初始化与MT5终端的连接。
        需要 path, login, password, server 等参数。
        """
        return mt5.initialize(**kwargs)

    def shutdown(self) -> bool:
        """关闭与MT5终端的连接。"""
        mt5.shutdown()
        return True

    def account_info(self) -> Optional[AccountInfo]:
        """获取账户信息。"""
        info = mt5.account_info()
        if info:
            # 将MT5的namedtuple转换为我们自定义的AccountInfo dataclass
            return AccountInfo(
                login=info.login,
                balance=info.balance,
                equity=info.equity,
                profit=info.profit,
                margin=info.margin,
                margin_free=info.margin_free,
                margin_level=info.margin_level,
                currency=info.currency
            )
        return None

    def symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """获取交易品种信息。"""
        info = mt5.symbol_info(symbol)
        if info:
            return SymbolInfo(
                name=info.name,
                point=info.point,
                spread=info.spread,
                digits=info.digits,
                trade_mode=info.trade_mode,
                volume_min=info.volume_min,
                volume_max=info.volume_max,
                volume_step=info.volume_step
            )
        return None

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        """
        Selects a symbol in the MarketWatch window or removes a symbol from the window.
        """
        return mt5.symbol_select(symbol, enable)

    def symbol_info_tick(self, symbol: str) -> Optional[Tick]:
        """获取最新报价。"""
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            return Tick(
                time=tick.time,
                bid=tick.bid,
                ask=tick.ask,
                last=tick.last,
                volume=tick.volume
            )
        return None

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[np.ndarray]:
        """获取历史K线数据。"""
        rates = mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
        if rates is not None and len(rates) > 0:
            # MT5返回的已经是NumPy结构化数组，但为了确保类型一致性，可以进行检查或转换
            # 在此我们假设其结构与RatesDTO兼容
            return rates
        return None

    def positions_get(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> Tuple[PositionInfo, ...]:
        """获取持仓信息。"""
        kwargs = {}
        if symbol:
            kwargs['symbol'] = symbol
        if magic:
            kwargs['magic'] = magic
            
        positions = mt5.positions_get(**kwargs)
        
        if positions is None:
            return tuple()

        # 将MT5的Position对象元组转换为我们的PositionInfo dataclass元组
        return tuple(
            PositionInfo(
                ticket=p.ticket,
                symbol=p.symbol,
                volume=p.volume,
                price_open=p.price_open,
                profit=p.profit,
                type=p.type,
                time=p.time,
                magic=p.magic
            ) for p in positions
        )

    def order_send(self, request: Dict[str, Any]) -> Optional[TradeResult]:
        """发送交易订单。"""
        result = mt5.order_send(request)
        if result:
            return TradeResult(
                retcode=result.retcode,
                deal=result.deal,
                order=result.order,
                volume=result.volume,
                price=result.price,
                comment=result.comment
            )
        return None

    def order_calc_margin(self, action: int, symbol: str, volume: float, price: float) -> Optional[float]:
        """计算订单保证金。"""
        success, margin = mt5.order_calc_margin(action, symbol, volume, price)
        return margin if success else None

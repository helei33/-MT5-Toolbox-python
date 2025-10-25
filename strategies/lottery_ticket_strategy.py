# strategies/lottery_ticket_strategy.py (已重构)
from models.strategy import Strategy
from models.events import MarketEvent
import random
from datetime import datetime, timedelta

class LotteryTicketStrategy(Strategy):
    """
    彩票型交易策略 (已重构)
    """
    strategy_name = "彩票型交易策略 (重构版)"

    strategy_description = """
一个极端的、基于概率的交易策略，旨在用小资金博取巨大回报。
1.  **随机方向**: 不预测市场，完全随机选择买入或卖出。
2.  **最大仓位**: 根据设定的保证金使用比例，开立尽可能大的仓位。
3.  **固定持仓**: 订单会持有固定的时间（可配置），到期后自动平仓。
4.  **极端盈利**: 当浮动盈利达到开仓时净值的特定倍数时，会提前止盈。
5.  **循环抽奖**: 一笔交易结束后，只要账户未爆仓，策略会自动开始下一轮。
"""

    strategy_params_config = {
        'symbol': {'label': '交易品种', 'type': 'str', 'default': 'XAUUSD'},
        'timeframe': {'label': 'K线周期', 'type': 'str', 'default': 'M1'},
        'holding_time_minutes': {'label': '持仓时间(分钟)', 'type': 'int', 'default': 60},
        'margin_usage_percent': {'label': '保证金使用比例(%)', 'type': 'float', 'default': 95.0},
        'extreme_profit_multiplier': {'label': '极端盈利倍数(相对净值)', 'type': 'float', 'default': 2.0},
        'magic': {'label': '魔术号', 'type': 'int', 'default': 202407},
    }

    def __init__(self, gateway, symbol, timeframe, params):
        super().__init__(gateway, symbol, timeframe, params)
        self.log("策略正在初始化...")
        self._load_params()

    def on_init(self):
        self.log("策略初始化完成。")
        return True

    def on_bar(self, event: MarketEvent):
        if event.symbol != self.symbol:
            return

        try:
            position = self._get_my_position()

            if position is None:
                self.log("未检测到持仓，准备开始新一轮抽奖...")
                self._place_new_trade()
            else:
                self._monitor_trade(position)
        except Exception as e:
            self.log(f"on_bar 循环中出现异常: {e}")

    def on_deinit(self):
        self.log("策略已停止。")

    def _load_params(self):
        self.holding_time_minutes = int(self.params.get('holding_time_minutes', 60))
        self.margin_usage_percent = float(self.params.get('margin_usage_percent', 95.0))
        self.extreme_profit_multiplier = float(self.params.get('extreme_profit_multiplier', 2.0))
        self.magic = int(self.params.get('magic', 202407))

    def _place_new_trade(self):
        if not self.gateway.symbol_select(self.symbol, True):
            self.log(f"订阅品种 {self.symbol} 失败。")
            return

        account_info = self.gateway.get_account_info()
        if not account_info:
            self.log("获取账户信息失败，无法开仓。")
            return

        trade_type = random.choice([self.ORDER_TYPE_BUY, self.ORDER_TYPE_SELL])
        volume = self._calculate_max_volume()
        if volume <= 0:
            self.log(f"计算出的可交易手数为0，无法开仓。可能保证金不足。")
            return

        tick = self.gateway.symbol_info_tick(self.symbol)
        if not tick:
            self.log(f"获取 {self.symbol} 价格失败。")
            return

        price = tick.ask if trade_type == self.ORDER_TYPE_BUY else tick.bid
        
        request = {
            "action": self.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": volume,
            "type": trade_type,
            "price": price,
            "magic": self.magic,
            "comment": f"{self.strategy_name}|EQ={account_info.equity:.2f}",
            "type_time": self.ORDER_TIME_GTC,
            "type_filling": self.ORDER_FILLING_IOC,
        }

        result = self.gateway.order_send(request)
        if result and result.retcode == 10009: # TRADE_RETCODE_DONE
            self.log(f"新彩票已购买！订单号: {result.order}, 手数: {volume}, 方向: {'BUY' if trade_type == self.ORDER_TYPE_BUY else 'SELL'}")
        else:
            self.log(f"开仓失败: {result.comment if result else '未知错误'}")

    def _monitor_trade(self, position):
        initial_equity = 0
        try:
            comment_parts = position.comment.split('|')
            if len(comment_parts) > 1 and comment_parts[1].startswith('EQ='):
                initial_equity = float(comment_parts[1].split('=')[1])
        except (ValueError, IndexError):
            self.log(f"警告: 无法从订单 {position.ticket} 的备注中解析初始净值。")
            account_info = self.gateway.get_account_info()
            if account_info: initial_equity = account_info.balance

        if initial_equity > 0 and position.profit >= initial_equity * self.extreme_profit_multiplier:
            profit_target = initial_equity * self.extreme_profit_multiplier
            self.log(f"恭喜！触发极端盈利！盈利 {position.profit:.2f} >= 目标 {profit_target:.2f}。正在平仓...")
            self._close_trade(position)
            return

        open_time = datetime.fromtimestamp(position.time)
        if datetime.now() >= open_time + timedelta(minutes=self.holding_time_minutes):
            self.log(f"持仓时间已到 ({self.holding_time_minutes}分钟)，正在平仓...")
            self._close_trade(position)
            return
        
        self.log(f"监控中... 订单: {position.ticket}, 浮盈: {position.profit:.2f}")

    def _close_trade(self, position):
        tick = self.gateway.symbol_info_tick(position.symbol)
        if not tick: return

        request = {
            "action": self.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": self.ORDER_TYPE_SELL if position.type == self.ORDER_TYPE_BUY else self.ORDER_TYPE_BUY,
            "price": tick.bid if position.type == self.ORDER_TYPE_BUY else tick.ask,
            "deviation": 20,
            "magic": self.magic,
            "comment": "Close by Lottery Strategy",
            "type_time": self.ORDER_TIME_GTC,
            "type_filling": self.ORDER_FILLING_IOC,
        }
        self.gateway.order_send(request)

    def _calculate_max_volume(self):
        account_info = self.gateway.get_account_info()
        if not account_info: return 0.0
        
        margin_to_use = account_info.margin_free * (self.margin_usage_percent / 100.0)
        
        margin_per_lot = self.gateway.order_calc_margin(self.ORDER_TYPE_BUY, self.symbol, 1.0)
        if margin_per_lot is None or margin_per_lot <= 0: return 0.0

        volume = margin_to_use / margin_per_lot
        
        symbol_info = self.gateway.symbol_info(self.symbol)
        if not symbol_info: return 0.0
        
        min_vol, max_vol, step_vol = symbol_info.volume_min, symbol_info.volume_max, symbol_info.volume_step

        if volume > min_vol:
            volume = round(volume / step_vol) * step_vol
        elif volume > 0:
            volume = min_vol

        volume = min(max_vol, volume)
        if volume < min_vol: return 0.0

        return round(volume, 2)

    def _get_my_position(self):
        positions = self.gateway.positions_get(magic=self.magic, symbol=self.symbol)
        if positions and len(positions) > 0:
            return positions[0]
        return None

    def log(self, message):
        print(f"[{self.strategy_name}@{self.symbol}] {message}")

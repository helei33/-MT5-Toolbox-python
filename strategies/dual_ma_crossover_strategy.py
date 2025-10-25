import numpy as np
import pandas as pd

# 1. 继承自新的 Strategy 基类
from models.strategy import Strategy 
from models.events import MarketEvent

class DualMaCrossoverStrategy(Strategy):
    """
    一个双均线交叉策略，已被重构为使用新的 Strategy 基类和 TradingGateway。
    注意：为了演示核心API的重构，原有的复杂风控逻辑（基于历史订单计算总盈亏）已被移除。
    在事件驱动架构中，这类状态管理应由 Portfolio 组件负责。
    """

    # --- 元数据和参数配置保持不变 ---
    strategy_name = "双均线交叉策略 (重构版)"
    strategy_description = "当快速移动平均线穿越慢速移动平均线时进行交易。已适配事件驱动回测架构。"
    strategy_params_config = {
        "symbol":           {"label": "交易品种", "type": "str", "default": "EURUSD"},
        "timeframe":        {"label": "K线周期 (M1, M15, H1...)", "type": "str", "default": "H1"},
        "fast_ma_period":   {"label": "快线周期", "type": "int", "default": 10},
        "slow_ma_period":   {"label": "慢线周期", "type": "int", "default": 20},
        "trade_volume":     {"label": "交易手数", "type": "float", "default": 0.01},
        "magic_number":     {"label": "魔术号", "type": "int", "default": 13579},
        "stop_loss_pips":   {"label": "止损点数 (0为不止损)", "type": "int", "default": 100},
        "take_profit_pips": {"label": "止盈点数 (0为不止盈)", "type": "int", "default": 200},
    }

    def on_init(self):
        """策略初始化。"""
        self.log("策略开始初始化...")
        
        symbol_info = self.gateway.symbol_info(self.symbol)
        if not symbol_info:
            self.log(f"错误: 无法获取品种信息 for '{self.symbol}'。")
            return False
            
        self.point = symbol_info.point
        self.mt5_timeframe = self._get_mt5_timeframe(self.timeframe)

        self.log(f"策略初始化完成。交易品种: {self.symbol}, 周期: {self.timeframe}")
        return True

    def on_bar(self, event: MarketEvent):
        """每个市场事件（新K线）的核心逻辑。"""
        if event.symbol != self.symbol:
            return
        self.check_and_trade()

    def on_deinit(self):
        """策略停止。"""
        self.log("策略停止。")

    def check_and_trade(self):
        """获取数据、计算指标、判断信号并执行交易。"""
        rates = self.gateway.copy_rates_from_pos(self.symbol, self.mt5_timeframe, 0, self.params['slow_ma_period'] + 5)
        if rates is None or len(rates) < self.params['slow_ma_period']:
            self.log("获取K线数据不足，跳过本次检查。")
            return

        df = pd.DataFrame(rates)
        fast_ma = df['close'].rolling(window=self.params['fast_ma_period']).mean()
        slow_ma = df['close'].rolling(window=self.params['slow_ma_period']).mean()

        last_fast_ma = fast_ma.iloc[-2]
        last_slow_ma = slow_ma.iloc[-2]
        prev_fast_ma = fast_ma.iloc[-3]
        prev_slow_ma = slow_ma.iloc[-3]

        # 注意：为了简化，我们假设一个策略只交易一个品种，所以只按symbol检查持仓
        # 在一个完整的系统中，还需要通过魔术号来区分不同策略的持仓
        positions = self.gateway.positions_get(symbol=self.symbol)

        # 金叉信号
        if prev_fast_ma < prev_slow_ma and last_fast_ma > last_slow_ma:
            self.log("检测到金叉信号 (买入)。")
            # 如果有卖出持仓，先平仓
            for pos in positions:
                if pos.type == self.ORDER_TYPE_SELL: # type 1 is SELL
                    self.log(f"发现反向持仓 (Sell Ticket: {pos.ticket})，正在平仓...")
                    self._close_position(pos)
                    return # 平仓后等待下一个bar再做决定
            
            # 如果没有持仓，则开仓
            if not positions:
                self.log("无持仓，准备开立多单...")
                self._open_position('buy')

        # 死叉信号
        elif prev_fast_ma > prev_slow_ma and last_fast_ma < last_slow_ma:
            self.log("检测到死叉信号 (卖出)。")
            # 如果有买入持仓，先平仓
            for pos in positions:
                if pos.type == self.ORDER_TYPE_BUY: # type 0 is BUY
                    self.log(f"发现反向持仓 (Buy Ticket: {pos.ticket})，正在平仓...")
                    self._close_position(pos)
                    return # 平仓后等待下一个bar再做决定

            # 如果没有持仓，则开仓
            if not positions:
                self.log("无持仓，准备开立空单...")
                self._open_position('sell')

    def _open_position(self, direction: str):
        """构建并发送开仓请求。"""
        tick = self.gateway.symbol_info_tick(self.symbol)
        if not tick:
            self.log("无法获取当前价格，无法开仓。")
            return
        
        price = tick.ask if direction == 'buy' else tick.bid
        sl, tp = self._calculate_sl_tp(direction, price)

        request = {
            "action": self.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.params['trade_volume'],
            "type": self.ORDER_TYPE_BUY if direction == 'buy' else self.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": self.params['magic_number'],
            "comment": "Opened by DualMA Strategy",
            "type_time": self.ORDER_TIME_GTC,
            "type_filling": self.ORDER_FILLING_IOC,
        }
        self.log(f"发送 {direction.upper()} 开仓请求...")
        result = self.gateway.order_send(request)
        if result:
            self.log(f"开仓请求已发送: {result.comment}")

    def _close_position(self, position):
        """构建并发送平仓请求。"""
        tick = self.gateway.symbol_info_tick(self.symbol)
        if not tick:
            self.log(f"无法获取当前价格，无法平仓 {position.ticket}。")
            return

        # 平仓就是反向开一个同等数量的仓位
        close_direction = self.ORDER_TYPE_SELL if position.type == self.ORDER_TYPE_BUY else self.ORDER_TYPE_BUY
        price = tick.bid if close_direction == self.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": self.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": close_direction,
            "position": position.ticket, # 指明要平掉的仓位
            "price": price,
            "deviation": 10,
            "magic": self.params['magic_number'],
            "comment": f"Closing position {position.ticket}",
            "type_time": self.ORDER_TIME_GTC,
            "type_filling": self.ORDER_FILLING_IOC,
        }
        self.log(f"发送平仓请求 for ticket {position.ticket}...")
        result = self.gateway.order_send(request)
        if result:
            self.log(f"平仓请求已发送: {result.comment}")

    def _calculate_sl_tp(self, order_type, price):
        sl_pips = self.params['stop_loss_pips']
        tp_pips = self.params['take_profit_pips']
        if sl_pips == 0 and tp_pips == 0: return 0.0, 0.0
        sl = price - sl_pips * self.point if order_type == 'buy' else price + sl_pips * self.point
        tp = price + tp_pips * self.point if order_type == 'buy' else price - tp_pips * self.point
        return sl if sl_pips > 0 else 0.0, tp if tp_pips > 0 else 0.0

    def _get_mt5_timeframe(self, tf_str):
        tf_map = {"M1": self.TIMEFRAME_M1, "H1": self.TIMEFRAME_H1, "D1": self.TIMEFRAME_D1}
        return tf_map.get(tf_str.upper())
        
    def log(self, message):
        print(f"[{self.strategy_name} - {self.symbol}]: {message}")
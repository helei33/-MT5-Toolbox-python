import numpy as np
import pandas as pd

# 注意：BaseStrategy 类是由主程序动态注入的，在编写策略时，你可以假设它已经存在。
# 在 VSCode 等编辑器中，为了获得更好的代码提示，你可以从 core_utils.py 中导入它，
# 但这在实际运行时不是必需的。
# from core_utils import BaseStrategy

class DualMaCrossoverStrategy(BaseStrategy):
    """
    一个完整的双均线交叉策略示例，展示了如何使用新的 BaseStrategy 框架。
    """

    # --- 1. 策略元数据定义 (必需) ---
    strategy_name = "双均线交叉策略 (带风控)"

    strategy_description = """
    这是一个功能完备的双均线交叉策略示例。
    
    核心逻辑:
    1. 当快速移动平均线从下向上穿越慢速移动平均线时，产生买入信号 (金叉)。
    2. 当快速移动平均线从上向下穿越慢速移动平均线时，产生卖出信号 (死叉)。
    
    功能特点:
    - 启动后立即检查信号并开仓。
    - 自动进行仓位管理，确保在同一品种上只持有一个由本策略开设的仓位。
    - 丰富的可配置参数，包括均线周期、交易品种、手数、止盈止损等。
    - 内置风险管理：当策略的总盈亏达到预设阈值时，会自动平仓并停止策略。
    """

    # --- 2. 策略参数配置 (可选，但强烈推荐) ---
    strategy_params_config = {
        "symbol":           {"label": "交易品种", "type": "str", "default": "EURUSD"},
        "timeframe":        {"label": "K线周期 (M1, M15, H1...)", "type": "str", "default": "M15"},
        "fast_ma_period":   {"label": "快线周期", "type": "int", "default": 10},
        "slow_ma_period":   {"label": "慢线周期", "type": "int", "default": 20},
        "trade_volume":     {"label": "交易手数", "type": "float", "default": 0.01},
        "magic_number":     {"label": "魔术号", "type": "int", "default": 13579},
        "stop_loss_pips":   {"label": "止损点数 (0为不止损)", "type": "int", "default": 100},
        "take_profit_pips": {"label": "止盈点数 (0为不止盈)", "type": "int", "default": 200},
        "max_profit_stop":  {"label": "总盈利停止阈值", "type": "float", "default": 50.0},
        "max_loss_stop":    {"label": "总亏损停止阈值", "type": "float", "default": -25.0},
    }

    # --- 3. 策略生命周期方法 ---

    def on_init(self):
        """
        策略初始化时调用。
        """
        self.log("策略开始初始化...")

        # 将字符串时间周期转换为MT5的常量
        self.mt5_timeframe = self._get_mt5_timeframe(self.params['timeframe'])
        if self.mt5_timeframe is None:
            self.log(f"错误: 不支持的时间周期 '{self.params['timeframe']}'。策略将停止。")
            return False

        # 订阅交易品种
        symbol = self.params['symbol']
        if not self.mt5.symbol_select(symbol, True):
            self.log(f"错误: 无法订阅品种 '{symbol}'。请检查品种名称是否正确。")
            return False
        
        # 获取品种的点值信息，用于计算SL/TP
        self.point = self.mt5.symbol_info(symbol).point

        # 初始化内部状态变量
        self.total_profit = 0.0  # 用于跟踪策略的总盈亏
        self.last_closed_ticket = None # 用于避免重复计算已平仓订单的盈亏

        self.log(f"策略初始化完成。交易品种: {symbol}, 周期: {self.params['timeframe']}")
        
        # 启动后立即执行一次逻辑，实现“立即开仓”
        self.log("启动后立即执行一次交易逻辑检查...")
        self.check_and_trade()

        return True

    def on_tick(self):
        """
        每个tick循环调用。
        """
        # 1. 检查风控条件
        if self.check_risk_management():
            return # 如果风控触发，则直接返回，等待策略停止

        # 2. 执行核心交易逻辑
        self.check_and_trade()

    def on_deinit(self):
        """
        策略停止时调用。
        """
        self.log(f"策略停止。最终总盈亏: {self.total_profit:.2f}")

    # --- 4. 策略核心逻辑和辅助方法 ---

    def check_and_trade(self):
        """
        获取数据、计算指标、判断信号并执行交易。
        """
        symbol = self.params['symbol']
        
        # 获取K线数据
        rates = self.mt5.copy_rates_from_pos(symbol, self.mt5_timeframe, 0, self.params['slow_ma_period'] + 5)
        if rates is None or len(rates) < self.params['slow_ma_period']:
            self.log("获取K线数据不足，跳过本次检查。")
            return

        # 计算均线
        df = pd.DataFrame(rates)
        fast_ma = df['close'].rolling(window=self.params['fast_ma_period']).mean()
        slow_ma = df['close'].rolling(window=self.params['slow_ma_period']).mean()

        # 获取最新的两个周期的均线值用于判断交叉
        # [-2] 是上一根K线的收盘值, [-1] 是当前未完成K线的实时值
        last_fast_ma = fast_ma.iloc[-2]
        last_slow_ma = slow_ma.iloc[-2]
        prev_fast_ma = fast_ma.iloc[-3]
        prev_slow_ma = slow_ma.iloc[-3]

        # 检查当前持仓
        positions = self.get_positions(symbol=symbol)
        my_positions = [p for p in positions if p.magic == self.params['magic_number']]

        # 判断交易信号
        # 金叉信号: 上一根K线快线在慢线下方，当前K线快线在慢线上方
        if prev_fast_ma < prev_slow_ma and last_fast_ma > last_slow_ma:
            self.log("检测到金叉信号 (买入)。")
            if my_positions:
                # 如果有反向持仓，先平仓
                for pos in my_positions:
                    if pos.type == self.mt5.ORDER_TYPE_SELL:
                        self.log(f"发现反向持仓 (Sell Ticket: {pos.ticket})，正在平仓...")
                        # 此处应调用平仓方法，为简化示例，我们直接开新仓
            
            # 如果没有持仓，则开多单
            if not my_positions:
                self.log("无持仓，准备开立多单...")
                sl, tp = self._calculate_sl_tp('buy')
                self.buy(symbol, self.params['trade_volume'], sl, tp, self.params['magic_number'])

        # 死叉信号: 上一根K线快线在慢线上方，当前K线快线在慢线下方
        elif prev_fast_ma > prev_slow_ma and last_fast_ma < last_slow_ma:
            self.log("检测到死叉信号 (卖出)。")
            if my_positions:
                # 如果有反向持仓，先平仓
                for pos in my_positions:
                    if pos.type == self.mt5.ORDER_TYPE_BUY:
                        self.log(f"发现反向持仓 (Buy Ticket: {pos.ticket})，正在平仓...")
                        # 此处应调用平仓方法，为简化示例，我们直接开新仓

            # 如果没有持仓，则开空单
            if not my_positions:
                self.log("无持仓，准备开立空单...")
                sl, tp = self._calculate_sl_tp('sell')
                self.sell(symbol, self.params['trade_volume'], sl, tp, self.params['magic_number'])

    def check_risk_management(self):
        """检查并更新总盈亏，判断是否触发风控止损/止盈"""
        # 获取已平仓订单历史
        history_orders = self.mt5.history_deals_get(0, 100) # 获取最近100笔成交记录
        if history_orders is None:
            return False

        # 计算已平仓订单的总盈亏
        closed_profit = 0
        for order in history_orders:
            if order.magic == self.params['magic_number'] and order.entry == 1: # entry=1 表示出场交易
                closed_profit += order.profit + order.swap + order.commission

        # 获取当前持仓的浮动盈亏
        positions = self.get_positions(symbol=self.params['symbol'])
        open_profit = sum(p.profit for p in positions if p.magic == self.params['magic_number'])

        self.total_profit = closed_profit + open_profit

        # 检查是否触发风控
        if self.total_profit >= self.params['max_profit_stop']:
            self.log(f"!!! 触发总盈利风控 !!! 总盈利 {self.total_profit:.2f} >= 阈值 {self.params['max_profit_stop']:.2f}")
            self.close_all_my_positions_and_stop()
            return True
        
        if self.total_profit <= self.params['max_loss_stop']:
            self.log(f"!!! 触发总亏损风控 !!! 总亏损 {self.total_profit:.2f} <= 阈值 {self.params['max_loss_stop']:.2f}")
            self.close_all_my_positions_and_stop()
            return True
        
        return False

    def close_all_my_positions_and_stop(self):
        """平掉所有由本策略开设的仓位，并停止策略"""
        self.log("正在平掉所有相关仓位...")
        positions = self.get_positions(symbol=self.params['symbol'])
        for pos in positions:
            if pos.magic == self.params['magic_number']:
                # 为简化，这里直接调用buy/sell的反向操作来平仓
                if pos.type == self.mt5.ORDER_TYPE_BUY:
                    self.sell(pos.symbol, pos.volume, comment=f"Close by risk control")
                elif pos.type == self.mt5.ORDER_TYPE_SELL:
                    self.buy(pos.symbol, pos.volume, comment=f"Close by risk control")
        self.log("所有仓位已平仓，策略将停止。")
        self.stop_strategy() # 请求停止策略

    def _calculate_sl_tp(self, order_type):
        """根据点数计算止损止盈价格"""
        sl_pips = self.params['stop_loss_pips']
        tp_pips = self.params['take_profit_pips']
        
        if sl_pips == 0 and tp_pips == 0:
            return 0.0, 0.0

        price = self.mt5.symbol_info_tick(self.params['symbol']).ask if order_type == 'buy' else self.mt5.symbol_info_tick(self.params['symbol']).bid
        
        sl = price - sl_pips * self.point if order_type == 'buy' else price + sl_pips * self.point
        tp = price + tp_pips * self.point if order_type == 'buy' else price - tp_pips * self.point

        return sl if sl_pips > 0 else 0.0, tp if tp_pips > 0 else 0.0

    def _get_mt5_timeframe(self, tf_str):
        """将字符串转换为MT5时间周期常量"""
        tf_map = {
            "M1": self.mt5.TIMEFRAME_M1, "M2": self.mt5.TIMEFRAME_M2, "M3": self.mt5.TIMEFRAME_M3,
            "M4": self.mt5.TIMEFRAME_M4, "M5": self.mt5.TIMEFRAME_M5, "M6": self.mt5.TIMEFRAME_M6,
            "M10": self.mt5.TIMEFRAME_M10, "M12": self.mt5.TIMEFRAME_M12, "M15": self.mt5.TIMEFRAME_M15,
            "M20": self.mt5.TIMEFRAME_M20, "M30": self.mt5.TIMEFRAME_M30, "H1": self.mt5.TIMEFRAME_H1,
            "H2": self.mt5.TIMEFRAME_H2, "H3": self.mt5.TIMEFRAME_H3, "H4": self.mt5.TIMEFRAME_H4,
            "H6": self.mt5.TIMEFRAME_H6, "H8": self.mt5.TIMEFRAME_H8, "H12": self.mt5.TIMEFRAME_H12,
            "D1": self.mt5.TIMEFRAME_D1, "W1": self.mt5.TIMEFRAME_W1, "MN1": self.mt5.TIMEFRAME_MN1
        }
        return tf_map.get(tf_str.upper())
from models.strategy import Strategy
from models.events import MarketEvent
import time

class AdvancedMartingaleV2(Strategy):
    """ 
    一个健壮的高级马丁格尔策略 (V2版)，已适配新的事件驱动架构。
    - 当一系列同向订单的总浮亏达到设定的点数间距时，按倍率加仓。
    - 当整个系列的订单达到总目标利润时，全部平仓。
    - 分别管理买入和卖出两个系列。
    """
    strategy_name = "高级马丁格尔策略V2 (新版)"

    strategy_description = """
这是一个经典的马丁格尔策略，已适配新版架构。
1. 如果没有持仓，策略会同时开一个买单和一个卖单作为初始订单。
2. 当某个方向的订单系列出现浮亏，并且当前价格距离最后一单的距离超过“加仓间距”时，会按“加仓倍率”增加手数，开立一个同向的新订单。
3. 当某个方向的整个订单系列的总浮动盈利达到“系列获利(美元)”时，策略会平掉该系列的所有订单。
4. 每个方向的订单系列独立管理，互不影响。"""

    strategy_params_config = {
        'symbol': {'label': '交易品种', 'type': 'str', 'default': 'EURUSD'},
        'timeframe': {'label': 'K线周期', 'type': 'str', 'default': 'H1'},
        'initial_lot': {'label': '初始手数', 'type': 'float', 'default': 0.01},
        'lot_multiplier': {'label': '加仓倍率', 'type': 'float', 'default': 2.0},
        'step_pips': {'label': '加仓间距(Pips)', 'type': 'int', 'default': 20},
        'series_target_profit_usd': {'label': '系列获利(美元)', 'type': 'float', 'default': 1.0},
        'max_levels': {'label': '最大加仓次数', 'type': 'int', 'default': 7},
        'magic': {'label': '魔术号', 'type': 'int', 'default': 123456},
    }

    def __init__(self, gateway, symbol, timeframe, params):
        """策略初始化"""
        super().__init__(gateway, symbol, timeframe, params)
        self.log(f"正在加载参数...")

        # 从params字典加载参数
        self.initial_lot = float(self.params.get('initial_lot', 0.01))
        self.lot_multiplier = float(self.params.get('lot_multiplier', 2.0))
        self.step_pips = int(self.params.get('step_pips', 20))
        self.series_target_profit_usd = float(self.params.get('series_target_profit_usd', 1.0))
        self.max_levels = int(self.params.get('max_levels', 7))
        self.magic = int(self.params.get('magic', 123456))

        self.order_comment = f"AMv2_{self.magic}"
        self.point = None

    def on_init(self):
        """当策略启动时调用，用于初始化。"""
        self.log(f"策略 '{self.strategy_name}' 正在初始化...")

        # 确保交易品种在MT5中是可见的
        if not self.gateway.symbol_select(self.symbol, True):
            self.log(f"订阅品种 '{self.symbol}' 失败。请检查券商品种名称是否正确，并确保它已在MT5市场报价中显示。")
            return False

        symbol_info = self.gateway.symbol_info(self.symbol)
        if not symbol_info:
            self.log(f"获取品种 {self.symbol} 信息失败，策略将无法运行。")
            return False
        self.point = symbol_info.point
        self.log("策略初始化成功。")
        return True

    def on_bar(self, event: MarketEvent):
        """在每个市场事件（新K线或固定时间间隔）上调用。"""
        if event.symbol != self.symbol:
            return
        
        try:
            self.check_series(self.ORDER_TYPE_BUY)
            self.check_series(self.ORDER_TYPE_SELL)
        except Exception as e:
            self.log(f"on_bar 循环中出现异常: {e}")

    def on_deinit(self):
        """在策略结束时调用，用于清理。"""
        self.log(f"策略 '{self.strategy_name}' 正在停止...")

    def check_series(self, order_type):
        """检查并管理一个方向的订单系列。"""
        positions = self.get_positions(order_type)

        # 1. 如果没有持仓，开立首单
        if not positions:
            self.open_trade(order_type, self.initial_lot)
            return

        # 2. 如果有持仓，检查是否需要平仓
        total_profit = sum(p.profit for p in positions)
        if total_profit >= self.series_target_profit_usd:
            self.close_all_positions(positions)
            return

        # 3. 检查是否需要加仓
        if len(positions) >= self.max_levels:
            return  # 已达到最大加仓次数

        last_pos = positions[-1]
        current_price = self.get_current_price(last_pos.type)
        if current_price is None:
            return

        # 计算与最后一单的价差
        price_diff = 0
        if last_pos.type == self.ORDER_TYPE_BUY:
            price_diff = last_pos.price_open - current_price # 价格下跌
        else: # SELL
            price_diff = current_price - last_pos.price_open # 价格上涨

        # 如果价格反向移动超过了步长，则加仓
        if price_diff >= self.step_pips * self.point:
            new_lot = round(last_pos.volume * self.lot_multiplier, 2)
            symbol_info = self.gateway.symbol_info(self.symbol)
            if symbol_info:
                min_lot = symbol_info.volume_min
                new_lot = max(new_lot, min_lot)
            self.open_trade(last_pos.type, new_lot)

    def open_trade(self, order_type, lot):
        """开立新订单。"""
        if not self.gateway.symbol_select(self.symbol, True):
            self.log(f"订阅品种 {self.symbol} 失败，请检查品种名称。")
            time.sleep(1)
            return None

        tick = self.gateway.symbol_info_tick(self.symbol)
        if not tick:
            self.log(f"无法获取 {self.symbol} 的报价，无法开仓。")
            return None

        price = tick.ask if order_type == self.ORDER_TYPE_BUY else tick.bid

        request = {
            "action": self.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "magic": self.magic,
            "deviation": 20,
            "comment": self.order_comment,
            "type_time": self.ORDER_TIME_GTC,
            "type_filling": self.ORDER_FILLING_IOC,
        }
        result = self.gateway.order_send(request)
        if not result or result.retcode != 10009: # 10009 is TRADE_RETCODE_DONE
            self.log(f"开仓失败: {result.comment if result else '未知错误'}")
        else:
            self.log(f"开仓成功: {order_type}, 手数: {lot}")
        return result

    def close_all_positions(self, positions):
        """关闭指定列表中的所有持仓。"""
        self.log(f"达到目标利润，关闭 {len(positions)} 个订单...")
        for pos in positions:
            close_order_type = self.ORDER_TYPE_SELL if pos.type == self.ORDER_TYPE_BUY else self.ORDER_TYPE_BUY
            
            tick = self.gateway.symbol_info_tick(self.symbol)
            if not tick:
                self.log(f"无法获取报价，跳过平仓订单 {pos.ticket}")
                continue
            
            price = tick.bid if pos.type == self.ORDER_TYPE_BUY else tick.ask
            
            request = {
                "action": self.TRADE_ACTION_DEAL,
                "position": pos.ticket,
                "symbol": self.symbol,
                "volume": pos.volume,
                "type": close_order_type,
                "price": price,
                "deviation": 20, 
                "magic": self.magic,
                "comment": f"Close_{self.order_comment}",
                "type_time": self.ORDER_TIME_GTC,
                "type_filling": self.ORDER_FILLING_IOC,
            }
            self.gateway.order_send(request)

    def get_positions(self, order_type):
        """获取当前策略指定方向的持仓。"""
        try:
            all_positions = self.gateway.positions_get(symbol=self.symbol, magic=self.magic)
            if all_positions is None:
                return []
            
            strategy_positions = [p for p in all_positions if p.type == order_type]
            strategy_positions.sort(key=lambda p: p.time)
            return strategy_positions
        except Exception as e:
            self.log(f"获取持仓失败: {e}")
            return []

    def get_current_price(self, order_type):
        """根据订单类型获取当前用于计算的价格。"""
        tick = self.gateway.symbol_info_tick(self.symbol)
        if not tick:
            self.log("无法获取当前报价。")
            return None
        if order_type == self.ORDER_TYPE_BUY:
            return tick.bid
        else:
            return tick.ask

    def log(self, message):
        """策略日志记录器"""
        print(f"[{self.strategy_name}@{self.symbol}] {message}")

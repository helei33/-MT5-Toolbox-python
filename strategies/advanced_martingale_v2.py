import MetaTrader5 as mt5
import time

class AdvancedMartingaleV2(BaseStrategy):
    """ 
    一个健壮的高级马丁格尔策略 (V2版)。
    - 当一系列同向订单的总浮亏达到设定的点数间距时，按倍率加仓。
    - 当整个系列的订单达到总目标利润时，全部平仓。
    - 分别管理买入和卖出两个系列。
    """
    strategy_name = "高级马丁格尔策略V2"

    strategy_description = """
这是一个经典的马丁格尔策略。
1. 如果没有持仓，策略会同时开一个买单和一个卖单作为初始订单。
2. 当某个方向的订单系列出现浮亏，并且当前价格距离最后一单的距离超过“加仓间距”时，会按“加仓倍率”增加手数，开立一个同向的新订单。
3. 当某个方向的整个订单系列的总浮动盈利达到“系列获利(美元)”时，策略会平掉该系列的所有订单。
4. 每个方向的订单系列独立管理，互不影响。"""

    strategy_params_config = {
        'symbol': {'label': '交易品种', 'type': 'str', 'default': 'EURUSD'},
        'initial_lot': {'label': '初始手数', 'type': 'float', 'default': 0.01},
        'lot_multiplier': {'label': '加仓倍率', 'type': 'float', 'default': 2.0},
        'step_pips': {'label': '加仓间距(Pips)', 'type': 'int', 'default': 20},
        'series_target_profit_usd': {'label': '系列获利(美元)', 'type': 'float', 'default': 1.0},
        'max_levels': {'label': '最大加仓次数', 'type': 'int', 'default': 7},
        'magic': {'label': '魔术号', 'type': 'int', 'default': 123456},
    }

    def __init__(self, config, log_queue, params):
        """策略初始化"""
        super().__init__(config, log_queue, params)
        self.is_first_run = True
        self.log_queue.put(f"[{self.strategy_name}] 已启动。")

        # 加载参数
        self.symbol = self.params.get('symbol', 'EURUSD')
        self.initial_lot = float(self.params.get('initial_lot', 0.01))
        self.lot_multiplier = float(self.params.get('lot_multiplier', 2.0))
        self.step_pips = int(self.params.get('step_pips', 20))
        self.series_target_profit_usd = float(self.params.get('series_target_profit_usd', 1.0))
        self.max_levels = int(self.params.get('max_levels', 7))
        self.magic = int(self.params.get('magic', 123456))

        # 核心优化：创建一个简短、无中文的备注，以提高券商兼容性
        self.order_comment = f"AMv2_{self.magic}"

    def initialize_mt5(self):
        """连接到MT5并进行初始化"""
        if not mt5.initialize(
            path=self.config['path'], login=int(self.config['login']),
            password=self.config['password'], server=self.config['server'], timeout=10000
        ):
            self.log_queue.put(f"[{self.strategy_name}] MT5初始化失败: {mt5.last_error()}")
            return False
        self.mt5 = mt5
        
        symbol_info = self.mt5.symbol_info(self.symbol)
        if not symbol_info:
            self.log_queue.put(f"[{self.strategy_name}] 获取品种 {self.symbol} 信息失败，策略暂停。")
            self.mt5.shutdown()
            return False
        self.point = symbol_info.point
        return True

    def run(self):
        """策略主循环"""
        while self.is_running():
            try:
                if not self.initialize_mt5():
                    self.sleep(20)
                    continue

                # 分别处理买入和卖出系列
                self.check_series(mt5.ORDER_TYPE_BUY)
                self.check_series(mt5.ORDER_TYPE_SELL)

                self.mt5.shutdown()
            except Exception as e:
                self.log_queue.put(f"[{self.strategy_name}] 循环异常: {e}")
            
            self.sleep(5) # 短暂休眠，避免CPU占用过高
        self.log_queue.put(f"[{self.strategy_name}] 已停止。")

    def check_series(self, order_type):
        """检查并管理一个方向的订单系列"""
        positions = self.get_positions(order_type)

        # 1. 如果没有持仓，开立首单
        if not positions and self.is_running(): # 确保在停止过程中不开新仓
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
        if last_pos.type == mt5.ORDER_TYPE_BUY:
            price_diff = last_pos.price_open - current_price # 价格下跌
        else: # SELL
            price_diff = current_price - last_pos.price_open # 价格上涨

        # 如果价格反向移动超过了步长，则加仓
        if price_diff >= self.step_pips * self.point:
            new_lot = round(last_pos.volume * self.lot_multiplier, 2)
            # 确保手数不小于最小手数
            min_lot = self.mt5.symbol_info(self.symbol).volume_min
            new_lot = max(new_lot, min_lot)
            self.open_trade(last_pos.type, new_lot)

    def open_trade(self, order_type, lot):
        """开立新订单"""
        # 核心修复：在下单前确保品种已在市场报价中订阅
        if not self.mt5.symbol_select(self.symbol, True):
            self.log_queue.put(f"[{self.strategy_name}] 订阅品种 {self.symbol} 失败，请检查品种名称是否正确。")
            time.sleep(1) # 等待一下，避免频繁失败
            return None

        tick = self.mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.log_queue.put(f"[{self.strategy_name}] 无法获取 {self.symbol} 的报价，无法开仓。")
            return None

        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "magic": self.magic,
            "deviation": 20,
            "comment": self.order_comment, # 使用优化后的备注
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = self.mt5.order_send(request)
        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log_queue.put(f"[{self.strategy_name}] 开仓失败: {result.comment if result else '未知错误'}")
        else:
            self.log_queue.put(f"[{self.strategy_name}] 开仓成功: {order_type}, 手数: {lot}")
        return result

    def close_all_positions(self, positions):
        """关闭指定列表中的所有持仓"""
        self.log_queue.put(f"[{self.strategy_name}] 达到目标利润，关闭 {len(positions)} 个订单...")
        for pos in positions:
            close_order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            
            tick = self.mt5.symbol_info_tick(self.symbol)
            if not tick:
                self.log_queue.put(f"[{self.strategy_name}] 无法获取报价，跳过平仓订单 {pos.ticket}")
                continue
            
            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": pos.ticket,
                "symbol": self.symbol,
                "volume": pos.volume,
                "type": close_order_type,
                "price": price,
                "deviation": 20, 
                "magic": self.magic,
                "comment": f"Close_{self.order_comment}", # 使用优化后的备注
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            self.mt5.order_send(request)

    def get_positions(self, order_type):
        """获取当前策略指定方向的持仓"""
        try:
            all_positions = self.mt5.positions_get(symbol=self.symbol, magic=self.magic)
            if all_positions is None:
                return []
            
            # 筛选出特定方向的持仓
            strategy_positions = [p for p in all_positions if p.type == order_type]
            
            # 按开仓时间排序，确保顺序正确
            strategy_positions.sort(key=lambda p: p.time)
            return strategy_positions
        except Exception as e:
            self.log_queue.put(f"[{self.strategy_name}] 获取持仓失败: {e}")
            return []

    def get_current_price(self, order_type):
        """根据订单类型获取当前用于计算的价格"""
        tick = self.mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.log_queue.put(f"[{self.strategy_name}] 无法获取当前报价。")
            return None
        if order_type == mt5.ORDER_TYPE_BUY:
            return tick.bid # 对于买单系列，用卖价来计算浮亏
        else:
            return tick.ask # 对于卖单系列，用买价来计算浮亏

    def sleep(self, seconds):
        """可中断的休眠"""
        for _ in range(seconds):
            if not self.is_running(): break
            time.sleep(1)
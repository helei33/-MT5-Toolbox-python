import MetaTrader5 as mt5
import time
import random
from datetime import datetime, timedelta

class LotteryTicketStrategy(BaseStrategy):
    """
    彩票型交易策略
    核心理念是“要么赢得全部，要么输个精光”，将外汇交易当作一种“彩票”机制。
    """
    strategy_name = "彩票型交易策略"

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
        'holding_time_minutes': {'label': '持仓时间(分钟)', 'type': 'int', 'default': 60},
        'margin_usage_percent': {'label': '保证金使用比例(%)', 'type': 'float', 'default': 95.0},
        'extreme_profit_multiplier': {'label': '极端盈利倍数(相对净值)', 'type': 'float', 'default': 2.0},
        'magic': {'label': '魔术号', 'type': 'int', 'default': 202407},
    }

    def initialize_mt5(self):
        """连接到MT5"""
        if not mt5.initialize(
            path=self.config['path'], login=int(self.config['login']),
            password=self.config['password'], server=self.config['server'], timeout=10000
        ):
            self.log_queue.put(f"[{self.strategy_name}] MT5初始化失败: {mt5.last_error()}")
            return False
        self.mt5 = mt5
        return True

    def run(self):
        """策略主循环"""
        self.log_queue.put(f"[{self.strategy_name}] 已启动。")
        self._load_params()

        while self.is_running():
            try:
                if not self.initialize_mt5():
                    self.sleep(20)
                    continue

                position = self.get_my_position()

                if position is None:
                    # 没有持仓，开始新一轮抽奖
                    self.log_queue.put(f"[{self.strategy_name}] 未检测到持仓，准备开始新一轮抽奖...")
                    self.place_new_trade()
                else:
                    # 有持仓，进入监控模式
                    self.monitor_trade(position)

                self.mt5.shutdown()
            except Exception as e:
                self.log_queue.put(f"[{self.strategy_name}] 发生异常: {e}")
            
            self.sleep(15) # 每15秒检查一次

        self.log_queue.put(f"[{self.strategy_name}] 已停止。")

    def _load_params(self):
        """加载并转换所有策略参数"""
        self.params_dict = {}
        for key, config in self.strategy_params_config.items():
            val = self.params.get(key, config.get('default'))
            param_type = config.get('type')
            
            if param_type == 'float': self.params_dict[key] = float(val)
            elif param_type == 'int': self.params_dict[key] = int(val)
            else: self.params_dict[key] = val

    def place_new_trade(self):
        """开启一笔新的随机交易"""
        symbol = self.params_dict['symbol']
        if not self.mt5.symbol_select(symbol, True):
            self.log_queue.put(f"[{self.strategy_name}] 订阅品种 {symbol} 失败。")
            return

        # 核心优化：在开仓前获取当前净值
        account_info = self.mt5.account_info()
        if not account_info:
            self.log_queue.put(f"[{self.strategy_name}] 获取账户信息失败，无法开仓。")
            return

        # 1. 随机方向
        trade_type = random.choice([mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL])
        
        # 2. 计算最大仓位
        volume = self._calculate_max_volume(symbol)
        if volume <= 0:
            self.log_queue.put(f"[{self.strategy_name}] 计算出的可交易手数为0，无法开仓。可能保证金不足。")
            return

        # 3. 获取价格并下单
        tick = self.mt5.symbol_info_tick(symbol)
        if not tick:
            self.log_queue.put(f"[{self.strategy_name}] 获取 {symbol} 价格失败。")
            return

        price = tick.ask if trade_type == mt5.ORDER_TYPE_BUY else tick.bid
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": trade_type,
            "price": price,
            "magic": self.params_dict['magic'],
            "comment": f"{self.strategy_name}|EQ={account_info.equity:.2f}", # 将开仓时净值记录在comment中
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self.mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log_queue.put(f"[{self.strategy_name}] 新彩票已购买！订单号: {result.order}, 手数: {volume}, 方向: {'BUY' if trade_type == mt5.ORDER_TYPE_BUY else 'SELL'}")
        else:
            self.log_queue.put(f"[{self.strategy_name}] 开仓失败: {result.comment if result else '未知错误'}")

    def monitor_trade(self, position):
        """监控现有持仓，检查平仓条件"""
        initial_equity = 0
        # 核心优化：从comment中解析出开仓时的净值
        try:
            comment_parts = position.comment.split('|')
            if len(comment_parts) > 1 and comment_parts[1].startswith('EQ='):
                initial_equity = float(comment_parts[1].split('=')[1])
        except (ValueError, IndexError):
            self.log_queue.put(f"[{self.strategy_name}] 警告: 无法从订单 {position.ticket} 的备注中解析初始净值。")
            # 如果解析失败，使用一个备用逻辑，例如使用账户当前余额，但这不太准确
            account_info = self.mt5.account_info()
            if account_info: initial_equity = account_info.balance

        # 1. 检查极端盈利
        if initial_equity > 0 and position.profit >= initial_equity * self.params_dict['extreme_profit_multiplier']:
            profit_target = initial_equity * self.params_dict['extreme_profit_multiplier']
            self.log_queue.put(f"[{self.strategy_name}] 恭喜！触发极端盈利！盈利 {position.profit:.2f} >= 目标 {profit_target:.2f}。正在平仓...")
            self.close_trade(position)
            return

        # 2. 检查持仓时间
        holding_minutes = self.params_dict['holding_time_minutes']
        open_time = datetime.fromtimestamp(position.time)
        if datetime.now() >= open_time + timedelta(minutes=holding_minutes):
            self.log_queue.put(f"[{self.strategy_name}] 持仓时间已到 ({holding_minutes}分钟)，正在平仓...")
            self.close_trade(position)
            return
        
        self.log_queue.put(f"[{self.strategy_name}] 监控中... 订单: {position.ticket}, 浮盈: {position.profit:.2f}")

    def close_trade(self, position):
        """平掉指定的仓位"""
        tick = self.mt5.symbol_info_tick(position.symbol)
        if not tick: return

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask,
            "deviation": 20,
            "magic": self.params_dict['magic'],
            "comment": "Close by Lottery Strategy",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        self.mt5.order_send(request)

    def _calculate_max_volume(self, symbol):
        """根据保证金使用比例计算最大可开仓手数"""
        account_info = self.mt5.account_info()
        if not account_info: return 0.0
        
        margin_to_use = account_info.margin_free * (self.params_dict['margin_usage_percent'] / 100.0)
        
        margin_per_lot = self.mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, 1.0)
        if margin_per_lot is None or margin_per_lot <= 0: return 0.0

        volume = margin_to_use / margin_per_lot
        
        # 规格化手数
        symbol_info = self.mt5.symbol_info(symbol)
        if not symbol_info: 
            return 0.0
        
        min_vol = symbol_info.volume_min
        max_vol = symbol_info.volume_max
        step_vol = symbol_info.volume_step

        if volume > min_vol:
            # 如果计算出的手数大于最小手数，正常规格化
            volume = round(volume / step_vol) * step_vol
        elif volume > 0:
            # 如果计算出的手数小于最小手数但大于0，则强制使用最小手数
            volume = min_vol

        # 确保最终手数在允许范围内
        volume = min(max_vol, volume)
        if volume < min_vol:
            return 0.0

        return round(volume, 2)

    def get_my_position(self):
        """获取由本策略创建的持仓"""
        positions = self.mt5.positions_get(magic=self.params_dict['magic'])
        if positions and len(positions) > 0:
            return positions[0]
        return None

    def sleep(self, seconds):
        """可中断的休眠"""
        for _ in range(seconds):
            if not self.is_running(): break
            time.sleep(1)
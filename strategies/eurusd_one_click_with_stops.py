# 策略文件: eurusd_one_click_with_stops.py
import MetaTrader5 as mt5
import time

class OneClickWithStopsStrategy(BaseStrategy):
    """欧美货币对一键开仓与双向挂单策略"""
    # 策略基本信息
    strategy_name = "欧美一键开仓+双向挂单"
    
    strategy_description = """
启动后立即开0.1手欧美，同时双向挂10档0.1手订单。
1. 自动开仓：策略启动时立即在当前价格开仓
2. 双向挂单：在当前价格上方和下方自动放置多档限价挂单
3. 可自定义挂单数量和间隔，灵活调整策略参数
    """
    
    # 参数配置
    strategy_params_config = {
        'symbol': {'label': '交易品种', 'type': 'str', 'default': 'EURUSD'},
        'initial_volume': {'label': '初始开仓手数', 'type': 'float', 'default': 0.1},
        'grid_levels': {'label': '挂单档数', 'type': 'int', 'default': 10},
        'grid_spacing': {'label': '每档间隔', 'type': 'float', 'default': 0.0005},
        'magic': {'label': '魔术号', 'type': 'int', 'default': 123456}
    }
    
    def __init__(self, config, log_queue, params):
        """初始化策略"""
        super().__init__(config, log_queue, params)
        self.is_first_run = True
        self.log_queue.put(f"[{self.strategy_name}] 已启动。")
        
        # 加载参数
        self.symbol = self.params.get('symbol', 'EURUSD')
        self.initial_volume = float(self.params.get('initial_volume', 0.1))
        self.grid_levels = int(self.params.get('grid_levels', 10))
        self.grid_spacing = float(self.params.get('grid_spacing', 0.0005))
        self.magic = int(self.params.get('magic', 123456))
        
        # 状态变量
        self.initial_order_executed = False  # 初始订单是否已执行
        self.grid_orders_placed = False      # 网格挂单是否已放置
        self.initial_open_price = None       # 初始开仓价格
        
        # 创建一个简短、无中文的备注，以提高券商兼容性
        self.order_comment = f"OCS_{self.magic}"
    
    def initialize_mt5(self):
        """连接到MT5并进行初始化"""
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
        self._running = True  # 初始化运行标志
        
        try:
            while self._running:
                try:
                    # 每次循环开始都检查运行状态
                    if not self._running:
                        break
                    
                    if not self.initialize_mt5():
                        self.sleep(20)
                        continue
                    
                    # 获取当前价格
                    symbol_info_tick = self.mt5.symbol_info_tick(self.symbol)
                    if symbol_info_tick is None:
                        self.log_queue.put(f"[{self.strategy_name}] 获取{self.symbol}行情失败")
                        self.sleep(20)
                        continue
                    
                    current_price = symbol_info_tick.ask  # 使用卖价作为开仓价格
                    
                    # 1. 执行初始开仓（仅一次）
                    if not self.initial_order_executed and self._running:
                        self.initial_open_price = current_price  # 记录开仓价格
                        self._execute_initial_order()
                        self.initial_order_executed = True
                        self.log_queue.put(f"[{self.strategy_name}] 执行初始开仓：{self.symbol} {self.initial_volume}手，价格：{current_price}")
                    
                    # 2. 放置双向网格挂单（仅一次）
                    if self.initial_order_executed and not self.grid_orders_placed and self._running:
                        self._place_grid_orders()
                        self.grid_orders_placed = True
                    
                    # 如果所有任务都已完成，进入低频率检查模式
                    if self.initial_order_executed and self.grid_orders_placed:
                        # 任务完成后，降低检查频率但仍定期检查运行状态
                        check_interval = 300  # 5分钟
                        elapsed = 0
                        while elapsed < check_interval and self._running:
                            self.sleep(10)  # 每10秒检查一次运行状态
                            elapsed += 10
                    else:
                        # 任务未完成时，使用较短的检查间隔
                        self.sleep(60)
                        
                except Exception as e:
                    self.log_queue.put(f"[{self.strategy_name}] 运行出错: {str(e)}")
                    # 出错后检查运行状态，如果已停止则退出
                    if not self._running:
                        break
                    self.sleep(20)
        finally:
            # 确保在退出时关闭MT5连接
            if hasattr(self, 'mt5'):
                try:
                    self.mt5.shutdown()
                except:
                    pass
            self.log_queue.put(f"[{self.strategy_name}] 策略已停止运行")
            
    def stop(self):
        """停止策略运行"""
        self.log_queue.put(f"[{self.strategy_name}] 收到停止信号，正在终止策略...")
        self._running = False
        # 立即中断可能的sleep
        if hasattr(self, '_sleep_event'):
            self._sleep_event.set()
            
    def is_running(self):
        """检查策略是否应该继续运行"""
        return getattr(self, '_running', False)
    
    def _execute_initial_order(self):
        """执行初始开仓订单"""
        # 每次操作前检查运行状态
        if not self._running:
            self.log_queue.put(f"[{self.strategy_name}] 策略已停止，取消开仓操作")
            return
            
        # 检查交易品种是否可交易
        symbol_info = self.mt5.symbol_info(self.symbol)
        if not symbol_info or not symbol_info.visible:
            self.log_queue.put(f"[{self.strategy_name}] 无法获取或选择交易品种 {self.symbol}")
            return
            
        if not symbol_info.visible:
            if not self.mt5.symbol_select(self.symbol, True):
                self.log_queue.put(f"[{self.strategy_name}] 无法选择交易品种 {self.symbol}")
                return
        
        # 安全地获取最新价格
        try:
            tick_info = self.mt5.symbol_info_tick(self.symbol)
            if tick_info is None:
                self.log_queue.put(f"[{self.strategy_name}] 无法获取最新价格")
                return
        except Exception as e:
            self.log_queue.put(f"[{self.strategy_name}] 获取价格时出错: {str(e)}")
            return
            
        # 创建订单请求
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.initial_volume,
            "type": self.mt5.ORDER_TYPE_BUY,
            "price": tick_info.ask,
            "deviation": 20,
            "magic": self.magic,
            "comment": self.order_comment,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        
        # 安全地发送订单
        try:
            result = self.mt5.order_send(request)
            
            # 检查结果是否为None
            if result is None:
                self.log_queue.put(f"[{self.strategy_name}] 初始开仓请求返回None，可能连接已断开")
            elif result.retcode != self.mt5.TRADE_RETCODE_DONE:
                self.log_queue.put(f"[{self.strategy_name}] 初始开仓失败: {result.retcode}")
            else:
                self.log_queue.put(f"[{self.strategy_name}] 初始开仓成功，订单号: {result.order}")
        except Exception as e:
            self.log_queue.put(f"[{self.strategy_name}] 初始开仓过程中出错: {str(e)}")
    
    def _place_grid_orders(self):
        """批量放置双向网格挂单"""
        # 每次操作前检查运行状态
        if not self._running:
            self.log_queue.put(f"[{self.strategy_name}] 策略已停止，取消挂单操作")
            return
            
        self.log_queue.put(f"[{self.strategy_name}] 开始批量放置双向挂单：以 {self.initial_open_price} 为基准，共 {self.grid_levels} 档，间隔 {self.grid_spacing}")
        
        # 检查交易品种是否可交易
        symbol_info = self.mt5.symbol_info(self.symbol)
        if not symbol_info or not symbol_info.visible:
            self.log_queue.put(f"[{self.strategy_name}] 无法获取或选择交易品种 {self.symbol}")
            return
            
        if not symbol_info.visible:
            if not self.mt5.symbol_select(self.symbol, True):
                self.log_queue.put(f"[{self.strategy_name}] 无法选择交易品种 {self.symbol}")
                return
        
        # 批量创建所有订单请求
        order_requests = []
        
        # 上方挂单（止盈方向，做多时的卖出挂单）
        for i in range(1, self.grid_levels + 1):
            order_price = self.initial_open_price + (i * self.grid_spacing)
            request = {
                "action": self.mt5.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": self.initial_volume,
                "type": self.mt5.ORDER_TYPE_SELL_LIMIT,
                "price": order_price,
                "deviation": 20,
                "magic": self.magic,
                "comment": f"{self.order_comment}_UP{i}",
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self.mt5.ORDER_FILLING_IOC,
            }
            order_requests.append(request)
        
        # 下方挂单（止损方向，做多时的买入挂单）
        for i in range(1, self.grid_levels + 1):
            order_price = self.initial_open_price - (i * self.grid_spacing)
            request = {
                "action": self.mt5.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": self.initial_volume,
                "type": self.mt5.ORDER_TYPE_BUY_LIMIT,
                "price": order_price,
                "deviation": 20,
                "magic": self.magic,
                "comment": f"{self.order_comment}_DN{i}",
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self.mt5.ORDER_FILLING_IOC,
            }
            order_requests.append(request)
        
        # 批量执行订单请求
        success_count = 0
        failure_count = 0
        
        for request in order_requests:
            # 每次发送订单前都检查运行状态
            if not self._running:
                self.log_queue.put(f"[{self.strategy_name}] 策略已停止，中断挂单操作")
                break
                
            # 安全地发送订单并处理可能的None返回值
            try:
                result = self.mt5.order_send(request)
                
                # 检查结果是否为None
                if result is None:
                    failure_count += 1
                    if failure_count <= 5:
                        self.log_queue.put(f"[{self.strategy_name}] 挂单请求返回None，可能连接已断开，价格: {request['price']}")
                    # 连接可能已断开，尝试重新初始化
                    if not self.initialize_mt5():
                        self.log_queue.put(f"[{self.strategy_name}] 无法重新连接MT5，停止挂单操作")
                        break
                elif result.retcode != self.mt5.TRADE_RETCODE_DONE:
                    failure_count += 1
                    # 只记录部分失败情况，避免日志过多
                    if failure_count <= 5:  # 最多记录5个失败信息
                        self.log_queue.put(f"[{self.strategy_name}] 挂单失败，价格: {request['price']}, 错误代码: {result.retcode}")
                else:
                    success_count += 1
                    # 只记录部分成功情况，避免日志过多
                    if success_count <= 3:  # 最多记录3个成功信息
                        self.log_queue.put(f"[{self.strategy_name}] 挂单成功，订单号: {result.order}, 价格: {request['price']}")
            except Exception as e:
                failure_count += 1
                if failure_count <= 5:
                    self.log_queue.put(f"[{self.strategy_name}] 挂单过程中出错: {str(e)}, 价格: {request['price']}")
        
        # 记录最终统计信息
        processed_count = success_count + failure_count
        self.log_queue.put(f"[{self.strategy_name}] 批量挂单完成：成功 {success_count} 笔，失败 {failure_count} 笔，总计 {processed_count} 笔订单")
        
        # 如果有失败的订单，提供重试选项
        if failure_count > 0:
            self.log_queue.put(f"[{self.strategy_name}] 注意：有 {failure_count} 笔订单挂单失败，可考虑增加重试机制或检查交易环境")
    
    def sleep(self, seconds):
        """封装的sleep函数，支持在策略运行时被中断"""
        start_time = time.time()
        while time.time() - start_time < seconds and getattr(self, '_running', False):
            # 使用较短的sleep间隔，以便能够更快响应停止信号
            time.sleep(0.5)
    
    def is_running(self):
        """检查策略是否应该继续运行"""
        # 基类应该提供这个方法，这里作为后备实现
        return getattr(self, '_running', True)

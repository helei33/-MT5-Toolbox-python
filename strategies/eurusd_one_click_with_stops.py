# 策略文件: eurusd_one_click_with_stops.py (已重构)
from models.strategy import Strategy
from models.events import MarketEvent
import time

class OneClickWithStopsStrategy(Strategy):
    """欧美货币对一键开仓与双向挂单策略 (已重构)"""
    # 策略基本信息
    strategy_name = "欧美一键开仓+双向挂单 (重构版)"
    
    strategy_description = """
启动后立即开0.1手欧美，同时双向挂10档0.1手订单。
1. 自动开仓：策略启动时立即在当前价格开仓
2. 双向挂单：在当前价格上方和下方自动放置多档限价挂单
3. 可自定义挂单数量和间隔，灵活调整策略参数
    """
    
    # 参数配置
    strategy_params_config = {
        'symbol': {'label': '交易品种', 'type': 'str', 'default': 'EURUSD'},
        'timeframe': {'label': 'K线周期', 'type': 'str', 'default': 'M1'}, # 添加timeframe
        'initial_volume': {'label': '初始开仓手数', 'type': 'float', 'default': 0.1},
        'grid_levels': {'label': '挂单档数', 'type': 'int', 'default': 10},
        'grid_spacing': {'label': '每档间隔(Pips)', 'type': 'int', 'default': 50}, # 改为Pips
        'magic': {'label': '魔术号', 'type': 'int', 'default': 123456}
    }
    
    def __init__(self, gateway, symbol, timeframe, params):
        """初始化策略"""
        super().__init__(gateway, symbol, timeframe, params)
        self.log(f"[{self.strategy_name}] 正在加载参数...")
        
        # 加载参数
        self.initial_volume = float(self.params.get('initial_volume', 0.1))
        self.grid_levels = int(self.params.get('grid_levels', 10))
        self.grid_spacing_pips = int(self.params.get('grid_spacing', 50))
        self.magic = int(self.params.get('magic', 123456))
        
        self.order_comment = f"OCS_{self.magic}"
        self.point = None

    def on_init(self):
        """策略启动时，执行所有核心逻辑。"""
        self.log("策略启动，开始执行一次性任务...")

        symbol_info = self.gateway.symbol_info(self.symbol)
        if not symbol_info:
            self.log(f"获取品种 {self.symbol} 信息失败，策略终止。")
            return False
        self.point = symbol_info.point
        self.grid_spacing = self.grid_spacing_pips * self.point

        # 1. 执行初始开仓
        tick = self.gateway.symbol_info_tick(self.symbol)
        if not tick:
            self.log("获取当前价格失败，无法执行初始开仓。")
            return False
        
        initial_open_price = tick.ask
        self._execute_initial_order(initial_open_price)
        self.log(f"执行初始开仓：{self.symbol} {self.initial_volume}手，价格：{initial_open_price}")

        # 2. 放置双向网格挂单
        self._place_grid_orders(initial_open_price)
        
        self.log("所有一次性任务已完成。策略进入待机模式。")
        return True

    def on_bar(self, event: MarketEvent):
        """对于此策略，所有操作都在on_init中完成，on_bar无需操作。"""
        pass

    def on_deinit(self):
        """策略停止时的清理工作。"""
        self.log("策略正在停止。")

    def _execute_initial_order(self, price):
        """执行初始开仓订单"""
        request = {
            "action": self.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.initial_volume,
            "type": self.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 20,
            "magic": self.magic,
            "comment": self.order_comment,
            "type_time": self.ORDER_TIME_GTC,
            "type_filling": self.ORDER_FILLING_IOC,
        }
        
        result = self.gateway.order_send(request)
        if not result or result.retcode != 10009: # TRADE_RETCODE_DONE
            self.log(f"初始开仓失败: {result.comment if result else '未知错误'}")
        else:
            self.log(f"初始开仓成功，订单号: {result.order}")

    def _place_grid_orders(self, base_price):
        """批量放置双向网格挂单"""
        self.log(f"开始批量放置双向挂单：以 {base_price} 为基准...")
        
        # 上方挂单 (Sell Limit)
        for i in range(1, self.grid_levels + 1):
            order_price = round(base_price + (i * self.grid_spacing), self.gateway.symbol_info(self.symbol).digits)
            request = {
                "action": self.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": self.initial_volume,
                "type": self.ORDER_TYPE_SELL_LIMIT,
                "price": order_price,
                "deviation": 20,
                "magic": self.magic,
                "comment": f"{self.order_comment}_UP{i}",
                "type_time": self.ORDER_TIME_GTC,
                "type_filling": self.ORDER_FILLING_IOC,
            }
            self.gateway.order_send(request)

        # 下方挂单 (Buy Limit)
        for i in range(1, self.grid_levels + 1):
            order_price = round(base_price - (i * self.grid_spacing), self.gateway.symbol_info(self.symbol).digits)
            request = {
                "action": self.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": self.initial_volume,
                "type": self.ORDER_TYPE_BUY_LIMIT,
                "price": order_price,
                "deviation": 20,
                "magic": self.magic,
                "comment": f"{self.order_comment}_DN{i}",
                "type_time": self.ORDER_TIME_GTC,
                "type_filling": self.ORDER_FILLING_IOC,
            }
            self.gateway.order_send(request)
        
        self.log(f"批量挂单请求已全部发送 ({self.grid_levels * 2} 笔)。")

    def log(self, message):
        """策略日志记录器"""
        # 使用 print 或 logging，取决于项目配置
        print(f"[{self.strategy_name}@{self.symbol}] {message}")
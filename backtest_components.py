from abc import ABC, abstractmethod
from queue import Queue
import pandas as pd

from events import MarketEvent, SignalEvent, OrderEvent, FillEvent
from data_manager import DataManager

class DataHandler(ABC):
    """
    DataHandler的抽象基类。
    负责提供市场数据，并在回测中生成MarketEvent心跳。
    """
    @abstractmethod
    def get_latest_bar(self, symbol: str) -> pd.Series:
        """获取指定品种的最新K线数据。"""
        pass

    @abstractmethod
    def update_bars(self) -> bool:
        """将数据指针向前移动一根K线，并生成一个新的MarketEvent。
        如果数据结束，则返回False。
        """
        pass

class DuckDBDataHandler(DataHandler):
    """
    从DuckDB加载数据，并在内存中进行回测的数据处理器。
    """
    def __init__(self, events_queue: Queue, symbols: list[str], timeframe: str, start_date: str, end_date: str):
        """
        :param events_queue: 事件队列。
        :param symbols: 要交易的品种列表 (当前版本简化为单个symbol)。
        :param timeframe: K线周期。
        :param start_date: 回测开始日期。
        :param end_date: 回测结束日期。
        """
        self.events = events_queue
        # TODO: 当前简化为只处理第一个symbol
        self.symbol = symbols[0]
        self.data_manager = DataManager()

        # 将所有数据一次性加载到内存
        self.all_data = self.data_manager.get_data(self.symbol, timeframe, start_date, end_date)
        if self.all_data is None or self.all_data.empty:
            raise ValueError(f"无法从DataManager获取到 {self.symbol} 的数据，请检查数据是否存在或时间范围是否正确。")

        self.latest_symbol_data = {self.symbol: None}
        self._data_iterator = self.all_data.iterrows()
        self.continue_backtest = True

    def get_latest_bar(self, symbol: str) -> pd.Series:
        """
        返回最新的K线数据。在事件驱动模型中，这应该是当前MarketEvent所指向的K线。
        """
        if symbol in self.latest_symbol_data:
            return self.latest_symbol_data[symbol]
        return None

    def update_bars(self) -> bool:
        """
        从数据迭代器中获取下一条记录，更新latest_symbol_data，
        并向事件队列中放入一个新的MarketEvent。
        """
        try:
            index, bar = next(self._data_iterator)
            bar.name = index # 将时间戳赋给Series的name属性
            self.latest_symbol_data[self.symbol] = bar
            
            # 创建并推送市场事件
            market_event = MarketEvent(
                symbol=self.symbol,
                time=int(bar.name.timestamp()) # 使用K线的时间戳
            )
            self.events.put(market_event)
            return True
        except StopIteration:
            # 数据结束
            self.continue_backtest = False
            return False

class Portfolio:
    """
    投资组合管理器，是回测系统的核心状态机。
    - 响应SignalEvent，进行风险检查并生成OrderEvent。
    - 响应FillEvent，更新持仓和账户状态。
    - 在每个MarketEvent上更新当前持仓的市价和浮动盈亏。
    - 提供接口供BacktestTradingGateway查询，以模拟account_info()等函数。
    """
    def __init__(self, events_queue: Queue, data_handler: DataHandler, initial_cash: float = 10000.0, leverage: int = 100):
        self.events = events_queue
        self.data_handler = data_handler
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.leverage = leverage
        
        self.positions = {}  # key by symbol
        self.equity = initial_cash
        self.margin_used = 0.0
        self.trade_history = []

    def on_bar(self, event: MarketEvent):
        """
        在每个新的市场事件（K线）上被调用，用于更新所有持仓的当前价值和浮动盈亏。
        """
        if event.type != 'MARKET':
            return

        current_equity = self.cash
        latest_bar = self.data_handler.get_latest_bar(event.symbol)
        
        if latest_bar is None:
            return

        for symbol, pos in self.positions.items():
            # 更新当前价格和浮动盈亏
            profit = 0
            if pos['type'] == 0:  # 0 for buy
                profit = (latest_bar['close'] - pos['price_open']) * pos['volume'] * 100000  # 简化计算
            else:  # 1 for sell
                profit = (pos['price_open'] - latest_bar['close']) * pos['volume'] * 100000  # 简化计算
            
            self.positions[symbol]['profit'] = profit
            self.positions[symbol]['price_current'] = latest_bar['close']
            current_equity += profit

        self.equity = current_equity

    def on_signal(self, event: SignalEvent):
        """
        处理来自策略的信号事件。
        """
        if event.type != 'SIGNAL':
            return
        
        # TODO: 在这里添加更复杂的投资组合逻辑和风险管理
        # 例如：基于信号强度调整订单大小
        
        # 简化逻辑：直接将信号转换为市价单事件
        order = OrderEvent(
            symbol=event.symbol,
            order_type='MKT',
            direction=event.direction,
            quantity=0.1  # 固定的交易量
        )
        self.events.put(order)

    def on_fill(self, event: FillEvent):
        """
        处理来自执行处理器的成交事件，更新持仓和现金。
        """
        if event.type != 'FILL':
            return

        # 更新现金（扣除成本）
        # 假设成本 = 名义价值 * 手续费率
        # commission = event.fill_price * event.quantity * 100000 * 0.0001 
        self.cash -= event.commission

        # 更新持仓
        pos_dir = 1 if event.direction == 'BUY' else -1
        
        if event.symbol not in self.positions:
            # 开新仓
            self.positions[event.symbol] = {
                'type': 0 if event.direction == 'BUY' else 1,
                'volume': event.quantity,
                'price_open': event.fill_price,
                'price_current': event.fill_price,
                'profit': -event.commission, # 初始利润为负的佣金
                'open_time': self.data_handler.get_latest_bar(event.symbol).name
            }
        else:
            # 更新现有仓位（加仓/减仓）
            existing_pos = self.positions[event.symbol]
            # TODO: 实现更复杂的加减仓逻辑
            # 此处简化为平仓再开仓
            print("WARN: Portfolio logic for adjusting existing positions is simplified.")
            self.cash += existing_pos['profit'] # 实现利润
            del self.positions[event.symbol]

    def get_account_info(self) -> dict:
        """
        返回一个模拟的账户信息字典，供Gateway使用。
        """
        return {
            "balance": self.cash,
            "equity": self.equity,
            "profit": self.equity - self.cash,
            "margin": self.margin_used,
            "margin_free": self.equity - self.margin_used,
            "margin_level": (self.equity / self.margin_used) if self.margin_used > 0 else 0,
        }

    def get_positions_info(self, symbol: str = None) -> list:
        """
        返回一个模拟的持仓信息列表，供Gateway使用。
        """
        if symbol:
            pos = self.positions.get(symbol)
            return [pos] if pos else []
        return list(self.positions.values())

class ExecutionHandler(ABC):
    """
    执行处理器的抽象基类，负责接收订单并执行它们。
    """
    @abstractmethod
    def execute_order(self, event: OrderEvent):
        pass

class SimulatedExecutionHandler(ExecutionHandler):
    """
    模拟的执行处理器，用于回测环境。
    它接收OrderEvent，并根据下一根K线的数据模拟成交，然后生成FillEvent。
    """
    def __init__(self, events_queue: Queue, data_handler: DataHandler, commission_per_trade: float = 0.1, slippage_points: int = 2):
        self.events = events_queue
        self.data_handler = data_handler
        self.commission_per_trade = commission_per_trade
        self.slippage_points = slippage_points

    def execute_order(self, event: OrderEvent):
        """
        模拟订单执行。为了防止前视偏差，此方法应在收到新K线数据后被调用。
        它基于当前K线的数据（被视为下一根K线）来决定成交价。
        """
        if event.type != 'ORDER':
            return

        bar = self.data_handler.get_latest_bar(event.symbol)
        if bar is None:
            print(f"Execution Error: No market data for {event.symbol}")
            return

        fill_price = 0.0
        slippage = 0.0

        # 模拟市价单（MKT）
        if event.order_type == 'MKT':
            # 假设在下一根K线的开盘价成交
            fill_price = bar['open']
            
            # 模拟滑点
            # TODO: 从symbol_info获取point大小
            point = 0.00001 # 临时硬编码
            slippage_adj = self.slippage_points * point
            if event.direction == 'BUY':
                slippage = slippage_adj
                fill_price += slippage
            elif event.direction == 'SELL':
                slippage = -slippage_adj
                fill_price += slippage # 卖出时，价格更低

        # TODO: 在此实现其他订单类型（LMT, STP）的逻辑

        # 创建成交事件
        fill_event = FillEvent(
            symbol=event.symbol,
            direction=event.direction,
            quantity=event.quantity,
            fill_price=fill_price,
            commission=self.commission_per_trade,
            slippage=slippage
        )
        self.events.put(fill_event)

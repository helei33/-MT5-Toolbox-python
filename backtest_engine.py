# backtest_engine.py

import pandas as pd
import numpy as np
from queue import Queue
from collections import namedtuple
import time
import uuid
import importlib.util
import sys

# 模拟MT5的Position对象，方便策略代码复用
SimulatedPosition = namedtuple('SimulatedPosition', [
    'ticket', 'symbol', 'type', 'volume', 'price_open', 'sl', 'tp', 'price_current', 'profit', 'magic'
])

# 模拟MT5的SymbolInfo对象
SimulatedSymbolInfo = namedtuple('SimulatedSymbolInfo', ['point'])

class SimulatedPortfolio:
    """
    模拟投资组合，负责管理回测中的虚拟资产、持仓和交易历史。
    """
    def __init__(self, start_cash=10000.0, leverage=100):
        self.start_cash = start_cash
        self.cash = start_cash
        self.equity = start_cash
        self.leverage = leverage
        self.positions = {}  # 使用字典存储持仓，key为ticket
        self.trade_history = []
        self.point_size = {} # 缓存每个symbol的point大小

    def _get_point_size(self, symbol):
        # 简化处理，实际应从symbol info获取
        if symbol not in self.point_size:
            if "JPY" in symbol:
                self.point_size[symbol] = 0.001
            else:
                self.point_size[symbol] = 0.00001
        return self.point_size[symbol]

    def on_bar(self, bar_data):
        """
        在每个新的K线数据上调用，处理持仓更新和SL/TP检查。
        bar_data 是一个包含 'symbol', 'high', 'low', 'close' 的 Series/dict
        """
        positions_to_close = []
        current_equity = self.cash
        
        for ticket, pos in list(self.positions.items()):
            # 更新当前价格和浮动盈亏
            profit = 0
            if pos['type'] == 0: # 0 for buy
                profit = (bar_data['close'] - pos['price_open']) * pos['volume'] * 100000 # 简化计算
            else: # 1 for sell
                profit = (pos['price_open'] - bar_data['close']) * pos['volume'] * 100000 # 简化计算
            
            self.positions[ticket]['profit'] = profit
            self.positions[ticket]['price_current'] = bar_data['close']

            # 检查SL/TP
            close_price = None
            if pos['type'] == 0: # Buy
                if pos['sl'] > 0 and bar_data['low'] <= pos['sl']:
                    close_price = pos['sl']
                elif pos['tp'] > 0 and bar_data['high'] >= pos['tp']:
                    close_price = pos['tp']
            else: # Sell
                if pos['sl'] > 0 and bar_data['high'] >= pos['sl']:
                    close_price = pos['sl']
                elif pos['tp'] > 0 and bar_data['low'] <= pos['tp']:
                    close_price = pos['tp']
            
            if close_price:
                positions_to_close.append((ticket, close_price))

            current_equity += profit

        # 执行平仓
        for ticket, close_price in positions_to_close:
            if ticket in self.positions:
                self.close_position(ticket, close_price)

        self.equity = current_equity


    def execute_trade(self, order_type, symbol, volume, price, sl, tp, magic):
        """
        执行一个模拟交易。
        order_type: 0 for buy, 1 for sell
        """
        margin_required = (volume * 100000 * price) / self.leverage
        if self.equity - margin_required < 0:
            print(f"Backtest Warning: Not enough margin to execute trade on {symbol}")
            return None

        ticket = str(uuid.uuid4())
        position = {
            'ticket': ticket, 'symbol': symbol, 'type': order_type, 'volume': volume,
            'price_open': price, 'sl': sl, 'tp': tp, 'magic': magic,
            'open_time': time.time(), 'price_current': price, 'profit': 0.0
        }
        self.positions[ticket] = position
        return position

    def close_position(self, ticket, close_price):
        """
        平仓一个现有持仓。
        """
        if ticket not in self.positions:
            return False
            
        pos = self.positions.pop(ticket)
        
        profit = 0
        if pos['type'] == 0: # Buy
            profit = (close_price - pos['price_open']) * pos['volume'] * 100000
        else: # Sell
            profit = (pos['price_open'] - close_price) * pos['volume'] * 100000
            
        self.cash += profit
        
        closed_trade = pos.copy()
        closed_trade.update({'close_price': close_price, 'close_time': time.time(), 'profit': profit})
        self.trade_history.append(closed_trade)
        return True

    def get_positions(self, symbol=None, magic=None):
        """
        返回符合条件的当前模拟持仓列表 (以namedtuple格式)。
        """
        result = []
        for pos in self.positions.values():
            if (symbol is None or pos['symbol'] == symbol) and (magic is None or pos['magic'] == magic):
                result.append(SimulatedPosition(**pos))
        return result

class BacktestStrategyBase:
    """
    模拟的策略基类，伪装成core_utils.BaseStrategy，为策略提供模拟的MT5 API。
    """
    def __init__(self, config, log_queue, params):
        self.config = config
        self.log_queue = log_queue
        self.params = params
        self.full_data = None
        self.portfolio = None
        self.current_bar_index = 0
        self.mt5 = self
        self.active = True

    def stop(self):
        self.active = False

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        end_index = self.current_bar_index + 1
        start_index = max(0, end_index - count)
        return self.full_data.iloc[start_index:end_index].to_records(index=False)

    def symbol_info_tick(self, symbol):
        current_bar = self.full_data.iloc[self.current_bar_index]
        return namedtuple('Tick', ['ask', 'bid'])(current_bar.close, current_bar.close)

    def symbol_info(self, symbol):
        point = self.portfolio._get_point_size(symbol)
        return SimulatedSymbolInfo(point=point)

    def get_positions(self, symbol=None, magic=None):
        return self.portfolio.get_positions(symbol=symbol, magic=magic)

    def buy(self, symbol, volume, sl=0.0, tp=0.0, magic=0, comment=""):
        price = self.full_data.iloc[self.current_bar_index].close
        return self.portfolio.execute_trade(0, symbol, volume, price, sl, tp, magic)

    def sell(self, symbol, volume, sl=0.0, tp=0.0, magic=0, comment=""):
        price = self.full_data.iloc[self.current_bar_index].close
        return self.portfolio.execute_trade(1, symbol, volume, price, sl, tp, magic)

    def close_position(self, ticket, comment=""):
        price = self.full_data.iloc[self.current_bar_index].close
        return self.portfolio.close_position(ticket, price)

    def on_init(self): pass
    def on_tick(self): raise NotImplementedError("Strategy must implement on_tick")
    def on_deinit(self): pass

class Backtester:
    """
    回测器主类，负责加载策略、循环数据并生成结果。
    """
    def __init__(self, strategy_info, full_data, params, config, log_queue, start_cash=10000.0):
        self.log_queue = log_queue
        self.portfolio = SimulatedPortfolio(start_cash=start_cash)
        self.full_data = full_data
        self.strategy = None

        self._prepare_strategy(strategy_info, config, params)

    def _prepare_strategy(self, strategy_info, config, params):
        self.log_queue.put(f"[Backtester] 正在加载策略: {strategy_info['path']}...")
        spec = importlib.util.spec_from_file_location(strategy_info['module_name'], strategy_info['path'])
        module = importlib.util.module_from_spec(spec)
        
        module.BaseStrategy = BacktestStrategyBase 
        sys.modules[strategy_info['module_name']] = module
        spec.loader.exec_module(module)

        StrategyClass = None
        for item in dir(module):
            obj = getattr(module, item)
            if isinstance(obj, type) and issubclass(obj, BacktestStrategyBase) and obj is not BacktestStrategyBase:
                StrategyClass = obj
                break
        
        if not StrategyClass:
            raise ValueError(f"在策略文件 {strategy_info['path']} 中找不到策略类")

        self.strategy = StrategyClass(config, self.log_queue, params)
        self.strategy.full_data = self.full_data
        self.strategy.portfolio = self.portfolio
        self.log_queue.put("[Backtester] 策略加载成功。")

    def run(self):
        """
        执行回测主循环。
        """
        self.log_queue.put("回测开始...")
        self.strategy.on_init()

        for i in range(len(self.full_data)):
            self.strategy.current_bar_index = i
            bar = self.full_data.iloc[i]
            
            self.portfolio.on_bar(bar)
            
            if self.strategy.active:
                self.strategy.on_tick()
            else:
                self.log_queue.put("策略已停止，结束回测。")
                break
        
        self.strategy.on_deinit()
        self.log_queue.put("回测完成。")
        return self.get_results()

    def get_results(self):
        """
        分析交易历史并计算统计数据。
        """
        history = pd.DataFrame(self.portfolio.trade_history)
        if history.empty:
            return "回测完成，但未执行任何交易。"

        total_trades = len(history)
        winning_trades = history[history['profit'] > 0]
        losing_trades = history[history['profit'] <= 0]
        
        win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0
        total_pnl = history['profit'].sum()
        
        equity_curve = self.portfolio.start_cash + history['profit'].cumsum()
        peak = equity_curve.expanding(min_periods=1).max()
        drawdown = (equity_curve - peak) / peak
        max_drawdown = drawdown.min()

        report = f"""
        --- 回测报告 ---
        时间范围: {self.full_data.index[0].date()} to {self.full_data.index[-1].date()}
        
        初始净值: {self.portfolio.start_cash:,.2f}
        最终净值:   {self.portfolio.equity:,.2f}
        
        总净盈亏:  {total_pnl:,.2f}
        总回报率:   {(self.portfolio.equity / self.portfolio.start_cash - 1):.2%}

        总交易数:   {total_trades}
        胜率:       {win_rate:.2%}

        平均盈利:       {winning_trades['profit'].mean():,.2f}
        平均亏损:      {losing_trades['profit'].mean():,.2f}
        盈亏比:   {abs(winning_trades['profit'].mean() / losing_trades['profit'].mean()) if len(losing_trades)>0 and losing_trades['profit'].mean()!=0 else np.inf:.2f}

        最大回撤:   {max_drawdown:.2%}
        ------------------------
        """
        return report
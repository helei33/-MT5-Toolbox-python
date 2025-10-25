import threading
import time
import traceback

from live_gateway import LiveTradingGateway
from events import MarketEvent

class StrategyRunner(threading.Thread):
    """
    一个线程，用于运行新的事件驱动策略。
    """
    def __init__(self, strategy_class, config, log_queue, params):
        super().__init__(daemon=True)
        self.strategy_class = strategy_class
        self.config = config
        self.log_queue = log_queue
        self.params = params
        self._stop_event = threading.Event()
        self.strategy = None
        self.gateway = None

    def run(self):
        account_id = self.config.get('account_id', 'Unknown')
        try:
            self.gateway = LiveTradingGateway()
            mt5_config = {
                'path': self.config['path'],
                'login': int(self.config['login']),
                'password': self.config['password'],
                'server': self.config['server'],
                'timeout': 10000
            }
            if not self.gateway.initialize(**mt5_config):
                self.log_queue.put(f"[{account_id}] 策略引擎连接MT5失败。 ")
                return

            symbol = self.params.get('symbol')
            timeframe = self.params.get('timeframe')
            if not symbol or not timeframe:
                self.log_queue.put(f"[{account_id}] 策略缺少 symbol 或 timeframe 参数。 ")
                return

            self.strategy = self.strategy_class(self.gateway, symbol, timeframe, self.params)
            
            if self.strategy.on_init() is False:
                self.log_queue.put(f"[{account_id}] 策略 on_init() 执行失败，策略终止。 ")
                return

            self.log_queue.put(f"[{account_id}] 策略 '{self.strategy.strategy_name}' 已启动。 ")

            last_check_time = 0
            check_interval = 5 # seconds
            while not self._stop_event.is_set():
                current_time = time.time()
                if current_time - last_check_time > check_interval:
                    last_check_time = current_time
                    event = MarketEvent(symbol=symbol, time=int(time.time()))
                    self.strategy.on_bar(event)
                
                time.sleep(1)

        except Exception as e:
            self.log_queue.put(f"[{account_id}] 策略执行时发生严重错误: {e}\n{traceback.format_exc()}")
        finally:
            if self.strategy:
                self.strategy.on_deinit()
            if self.gateway:
                self.gateway.shutdown()
            self.log_queue.put(f"[{account_id}] 策略已停止。 ")

    def stop_strategy(self):
        self._stop_event.set()

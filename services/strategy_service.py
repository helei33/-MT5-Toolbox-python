# --- services/strategy_service.py (修复后) ---
import logging
import threading
import time
import traceback
import importlib.util
import os
import inspect
from queue import Queue
from typing import Dict

from services.account_service import AccountService # <-- 依赖 AccountService
from live_gateway import LiveTradingGateway
from models.strategy import Strategy
from models.events import MarketEvent

class StrategyRunner(threading.Thread):
    """
    一个线程，用于运行一个策略实例。
    它接收一个已经初始化的策略对象。
    """
    def __init__(self, strategy: Strategy, log_queue: Queue):
        super().__init__(daemon=True)
        self.strategy = strategy
        self.log_queue = log_queue
        self._stop_event = threading.Event()
        self.logger = logging.getLogger("MT5Toolbox")

    def run(self):
        # 假设 gateway 有一个获取 account_id 的方法
        account_id = self.strategy.gateway.mt5_conn.login
        try:
            if self.strategy.on_init() is False:
                self.logger.error(f"[{account_id}] 策略 on_init() 执行失败，策略终止。")
                return

            self.logger.info(f"[{account_id}] 策略 '{self.strategy.strategy_name if hasattr(self.strategy, 'strategy_name') else type(self.strategy).__name__}' 已启动。")

            last_check_time = 0
            check_interval = 5 # seconds, can be made configurable per strategy later

            while not self._stop_event.is_set():
                current_time = time.time()
                if current_time - last_check_time > check_interval:
                    last_check_time = current_time
                    event = MarketEvent(symbol=self.strategy.symbol, time=int(time.time()))
                    self.strategy.on_bar(event)
                
                time.sleep(1)

        except Exception as e:
            self.logger.error(f"[{account_id}] 策略执行时发生严重错误: {e}", exc_info=True)
        finally:
            if self.strategy:
                self.strategy.on_deinit()
            # Gateway connection is managed by AccountService, so we don't shut it down here.
            self.logger.info(f"[{account_id}] 策略已停止。")

    def stop(self):
        self._stop_event.set()

class StrategyService:
    # 1. 在构造函数中注入 AccountService
    def __init__(self, log_queue: Queue, account_service: AccountService):
        self.logger = logging.getLogger("MT5Toolbox")
        self.log_queue = log_queue
        self.account_service = account_service # <-- 保存实例
        self.running_strategies: Dict[int, Dict[str, StrategyRunner]] = {}
        self.available_strategies = self._discover_strategies()

    def _discover_strategies(self) -> Dict:
        """动态发现并加载 'strategies' 目录下的所有策略。"""
        strategies = {}
        strategies_dir = "strategies"
        for filename in os.listdir(strategies_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                strategy_name = filename[:-3]
                try:
                    module_path = f"{strategies_dir}.{strategy_name}"
                    spec = importlib.util.find_spec(module_path)
                    if spec is None:
                        continue
                    
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    
                    for name, obj in inspect.getmembers(module):
                        if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy:
                            # 使用文件名作为key
                            strategies[strategy_name] = {
                                'class': obj,
                                'params_config': getattr(obj, 'params_config', {})
                            }
                            self.logger.info(f"发现策略: {strategy_name} -> {obj.__name__}")
                except Exception as e:
                    self.logger.error(f"加载策略 {strategy_name} 失败: {e}", exc_info=True)
        return strategies

    def start_strategy(self, account_id: int, strategy_name: str, strategy_params: dict):
        self.logger.info(f"尝试为账户 {account_id} 启动策略 {strategy_name}...")
        
        # 2. 不再接收 mt5_conn，而是向 AccountService 请求
        mt5_conn = self.account_service.get_connection(account_id)
        
        if not mt5_conn:
            self.logger.error(f"启动策略失败：账户 {account_id} 未连接。")
            return
            
        if self.running_strategies.get(account_id, {}).get(strategy_name):
            self.logger.warning(f"策略 {strategy_name} 已在账户 {account_id} 上运行，请先停止。")
            return
        
        try:
            strategy_info = self.available_strategies.get(strategy_name)
            if not strategy_info:
                self.logger.error(f"启动策略失败：找不到名为 '{strategy_name}' 的策略。")
                return

            strategy_class = strategy_info['class']
            
            gateway = LiveTradingGateway(mt5_conn, self.logger)
            
            # 准备参数 (合并默认参数和用户参数)
            final_params = {k: v.get('default') for k, v in strategy_info.get('params_config', {}).items() if 'default' in v}
            final_params.update(strategy_params)

            if not final_params.get('symbol') or not final_params.get('timeframe'):
                self.logger.error(f"启动策略 {strategy_name} 失败: 缺少 'symbol' 或 'timeframe' 参数。")
                return

            # 创建策略实例
            strategy = strategy_class(
                gateway=gateway,
                symbol=final_params['symbol'],
                timeframe=final_params['timeframe'],
                params=final_params
            )
            
            # 创建并启动运行器
            runner = StrategyRunner(strategy, self.log_queue)
            runner.start()
            
            # 管理状态
            if account_id not in self.running_strategies:
                self.running_strategies[account_id] = {}
            self.running_strategies[account_id][strategy_name] = runner
            
            self.logger.info(f"策略 {strategy_name} 已成功为账户 {account_id} 启动。")

        except Exception as e:
            self.logger.error(f"启动策略 {strategy_name} 失败: {e}", exc_info=True)

    def stop_strategy(self, account_id: int, strategy_name: str):
        runner = self.running_strategies.get(account_id, {}).pop(strategy_name, None)
        if runner:
            runner.stop()
            self.logger.info(f"策略 {strategy_name} 已为账户 {account_id} 停止。")
        else:
            self.logger.warning(f"尝试停止一个不存在的策略实例: {account_id}/{strategy_name}")
        
    def stop_all_strategies(self):
        """(为 CoreService.stop() 新增的方法)"""
        self.logger.info("正在停止所有运行中的策略...")
        # Create a copy of items to avoid runtime errors during dictionary modification
        for account_id, strategies in list(self.running_strategies.items()):
            for strategy_name, runner in list(strategies.items()):
                self.logger.debug(f"停止账户 {account_id} 的策略 {strategy_name}...")
                runner.stop()
        self.running_strategies.clear()
        self.logger.info("所有策略已停止。")
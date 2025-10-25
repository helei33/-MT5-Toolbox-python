# --- services/core_service.py (修改后) ---
import logging
from queue import Queue, Empty
from threading import Thread
import time

# 导入所有需要的服务
from services.strategy_service import StrategyService
from services.account_service import AccountService
from services.copier_service import CopierService

class CoreService:
    def __init__(self, log_queue: Queue, task_queue: Queue, account_update_queue: Queue):
        self.logger = logging.getLogger("MT5Toolbox")
        self.log_queue = log_queue
        self.task_queue = task_queue
        self.account_update_queue = account_update_queue
        
        # 1. 初始化所有服务
        self.account_service = AccountService(self.account_update_queue)
        self.copier_service = CopierService(self.account_service) # 依赖注入
        self.strategy_service = StrategyService(self.log_queue, self.account_service) # (见下方缺陷修复)
        
        self.running = True
        self.worker_thread = Thread(target=self._worker, daemon=True)

        # 跟单逻辑的配置 (这些也可以通过task_queue从UI更新)
        self.copy_mode = "open_only" 
        self.lots_multiplier = 1.0
        self.reverse_copy = False

    def start(self):
        self.worker_thread.start()

    def stop(self):
        self.logger.info("正在停止核心服务...")
        self.running = False
        self.task_queue.put(None) # 发送停止信号
        self.worker_thread.join()
        
        # 2. 调用所有服务的关闭方法
        self.strategy_service.stop_all_strategies() # (需要添加此方法到StrategyService)
        self.copier_service.shutdown()
        self.account_service.shutdown_all()
        self.logger.info("核心服务已停止。")

    def _worker(self):
        """
        唯一的后台工作线程。
        负责处理两件事：
        1. 响应来自UI的任务队列 (task_queue)
        2. 定期执行后台轮询任务 (账户更新、跟单)
        """
        self.logger.info("核心服务工作线程已启动。")
        self._send_copier_status_update() # 发送初始跟单状态
        self._send_strategy_list_update() # 发送初始策略列表
        last_poll_time = time.time()
        
        while self.running:
            try:
                # 1. 检查来自UI的任务 (非阻塞)
                task = self.task_queue.get(block=False)
                if task is None:
                    continue # 可能是停止信号
                
                # 3. 处理来自UI的任务
                self.handle_task(task)

            except Empty:
                # 任务队列为空，继续执行
                pass
            
            # 2. 执行定期轮询任务 (例如每秒一次)
            current_time = time.time()
            if current_time - last_poll_time >= 1.0:
                # (这里就是 app.py 中旧 _worker 的逻辑)
                
                # 2a. 更新账户信息
                self.account_service.process_account_updates()
                
                # 2b. 执行跟单逻辑
                self.copier_service.process_copying(
                    self.copy_mode, self.lots_multiplier, self.reverse_copy
                )
                
                # 2c. (TODO) 监控策略状态等
                
                last_poll_time = current_time

            # 避免CPU占用过高
            time.sleep(0.01) 

    def _send_copier_status_update(self):
        """获取最新的跟单状态并发送到UI队列。"""
        status = self.copier_service.get_status()
        self.account_update_queue.put({
            'action': 'COPIER_STATUS_UPDATE',
            'payload': status
        })

    def _send_strategy_list_update(self):
        """获取可用策略列表并发送到UI队列。"""
        strategies = list(self.strategy_service.available_strategies.keys())
        self.account_update_queue.put({
            'action': 'STRATEGY_LIST_UPDATE',
            'payload': {'strategies': strategies}
        })

    def handle_task(self, task: dict):
        """
        解析并执行来自 task_queue 的指令
        """
        action = task.get('action')
        payload = task.get('payload', {})
        self.logger.debug(f"收到任务: action={action}, payload={payload}")

        try:
            if action == 'LOGIN':
                self.account_service.login(
                    payload['account_id'],
                    payload['password'],
                    payload['server']
                )
            
            elif action == 'LOGOUT':
                self.account_service.logout(payload['account_id'])
                self._send_copier_status_update() # Logout might affect master/slave status
                
            elif action == 'SET_MASTER':
                self.copier_service.set_master(payload['account_id'])
                self._send_copier_status_update()

            elif action == 'TOGGLE_SLAVE':
                self.copier_service.toggle_slave(payload['account_id'])
                self._send_copier_status_update()
            
            elif action == 'UPDATE_COPIER_SETTINGS':
                self.lots_multiplier = payload.get('lots_multiplier', self.lots_multiplier)
                self.reverse_copy = payload.get('reverse_copy', self.reverse_copy)
                self.logger.info(f"跟单设置已更新: 手数={self.lots_multiplier}, 反向={self.reverse_copy}")

            elif action == 'START_STRATEGY':
                self.strategy_service.start_strategy(
                    payload['account_id'],
                    payload['strategy_name'],
                    payload['strategy_params']
                    # 注意：不再需要传递 mt5_conn，StrategyService会自己从AccountService获取
                )
                
            elif action == 'STOP_STRATEGY':
                self.strategy_service.stop_strategy(
                    payload['account_id'],
                    payload['strategy_name']
                )

            elif action == 'GET_STRATEGY_PARAMS':
                strategy_name = payload.get('strategy_name')
                if strategy_name in self.strategy_service.available_strategies:
                    params_config = self.strategy_service.available_strategies[strategy_name].get('params_config', {})
                    self.account_update_queue.put({
                        'action': 'STRATEGY_PARAMS_UPDATE',
                        'payload': {'strategy_name': strategy_name, 'params_config': params_config}
                    })
            
            else:
                self.logger.warning(f"未知的任务 action: {action}")
                
        except Exception as e:
            self.logger.error(f"处理任务 {action} 时出错: {e}", exc_info=True)
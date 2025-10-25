# --- services/account_service.py (新文件) ---
import logging
import MetaTrader5 as mt5
from queue import Queue
from models.mt5_types import MT5Connection, AccountInfo
from typing import Dict, Optional

class AccountService:
    """
    专门负责所有与MT5账户相关的操作，包括连接、登录、信息获取和状态管理。
    """
    def __init__(self, account_update_queue: Queue):
        self.logger = logging.getLogger("MT5Toolbox")
        self.account_update_queue = account_update_queue
        
        # 核心状态：所有已连接的账户实例
        # 键: account_id (int), 值: MT5Connection
        self.connected_accounts: Dict[int, MT5Connection] = {}
        # 键: account_id (int), 值: AccountInfo
        self.account_details: Dict[int, AccountInfo] = {}

    def login(self, account_id: int, password: str, server: str) -> bool:
        """
        处理登录逻辑
        """
        if account_id in self.connected_accounts:
            self.logger.warning(f"账户 {account_id} 已经登录。")
            return True

        self.logger.info(f"正在尝试登录账户 {account_id}...")
        
        # 初始化MT5连接
        conn = MT5Connection(
            login=account_id,
            password=password,
            server=server,
            logger=self.logger
        )
        
        if not conn.connect():
            self.logger.error(f"账户 {account_id} 登录失败。")
            conn.shutdown() # 确保释放资源
            return False

        account_info = conn.get_account_info()
        if not account_info:
            self.logger.error(f"账户 {account_id} 登录成功，但获取账户信息失败。")
            conn.shutdown()
            return False

        self.logger.info(f"账户 {account_id} ({account_info.name}) 登录成功。")
        self.connected_accounts[account_id] = conn
        self.account_details[account_id] = account_info
        
        # 将更新推送到UI
        self.account_update_queue.put({
            'action': 'LOGIN',
            'account_id': account_id,
            'details': account_info._asdict()
        })
        return True

    def logout(self, account_id: int):
        """
        处理登出逻辑
        """
        conn = self.connected_accounts.pop(account_id, None)
        self.account_details.pop(account_id, None)
        
        if conn:
            conn.shutdown()
            self.logger.info(f"账户 {account_id} 已注销。")
            
            # 将更新推送到UI
            self.account_update_queue.put({
                'action': 'LOGOUT',
                'account_id': account_id
            })
        else:
            self.logger.warning(f"尝试注销一个不存在的账户: {account_id}")

    def get_connection(self, account_id: int) -> Optional[MT5Connection]:
        """
        获取指定账户的连接实例
        """
        return self.connected_accounts.get(account_id)

    def get_all_connections(self) -> Dict[int, MT5Connection]:
        """
        获取所有已连接的账户
        """
        return self.connected_accounts

    def process_account_updates(self):
        """
        (由CoreService的worker定期调用)
        遍历所有已登录账户，检查并推送更新。
        """
        if not self.connected_accounts:
            return
            
        account_ids = list(self.connected_accounts.keys()) # 复制key以防遍历时修改
        for account_id in account_ids:
            conn = self.connected_accounts.get(account_id)
            if not conn:
                continue

            new_info = conn.get_account_info()
            if not new_info:
                self.logger.warning(f"无法获取账户 {account_id} 的更新信息，可能已断开。")
                self.logout(account_id)
                continue

            # 检查是否有变化
            if new_info != self.account_details.get(account_id):
                self.account_details[account_id] = new_info
                self.account_update_queue.put({
                    'action': 'UPDATE',
                    'account_id': account_id,
                    'details': new_info._asdict()
                })

    def shutdown_all(self):
        """
        关闭所有账户连接
        """
        self.logger.info("正在关闭所有MT5账户连接...")
        account_ids = list(self.connected_accounts.keys())
        for account_id in account_ids:
            self.logout(account_id)

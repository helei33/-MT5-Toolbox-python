# --- services/copier_service.py (新文件) ---
import logging
import MetaTrader5 as mt5
from typing import Dict, Optional, Set
from services.account_service import AccountService
from models.mt5_types import MT5Connection, Position

class CopierService:
    """
    独立负责所有跟单逻辑。
    依赖 AccountService 来获取账户连接。
    """
    def __init__(self, account_service: AccountService):
        self.logger = logging.getLogger("MT5Toolbox")
        self.account_service = account_service
        
        self.master_account_id: Optional[int] = None
        self.slave_account_ids: Set[int] = set()
        
        # 跟踪已复制的订单/持仓，防止重复执行
        # 键: (master_ticket_id), 值: (slave_ticket_id)
        self.copied_positions: Dict[int, int] = {} 
        self.copy_in_progress: Set[int] = set() # 防止重复开仓

    def set_master(self, account_id: int):
        self.logger.info(f"设置主账户为: {account_id}")
        self.master_account_id = account_id
        # 确保主账户不会是自己的从账户
        if account_id in self.slave_account_ids:
            self.slave_account_ids.remove(account_id)

    def toggle_slave(self, account_id: int):
        """切换一个账户的从账户状态。"""
        if account_id == self.master_account_id:
            self.logger.warning(f"不能将主账户 {account_id} 同时设为从账户。")
            return

        if account_id in self.slave_account_ids:
            self.logger.info(f"移除从账户: {account_id}")
            self.slave_account_ids.remove(account_id)
        else:
            self.logger.info(f"添加从账户: {account_id}")
            self.slave_account_ids.add(account_id)

    def get_status(self):
        """返回当前跟单服务的状态。"""
        return {
            'master': self.master_account_id,
            'slaves': list(self.slave_account_ids) # 返回列表以便JSON序列化
        }

    def process_copying(self, copy_mode: str, lots_multiplier: float, reverse_copy: bool):
        """
        (由CoreService的worker定期调用)
        执行一次完整的跟单检查和操作。
        """
        if not self.master_account_id or not self.slave_account_ids:
            return # 没有主账户或从账户，直接返回

        master_conn = self.account_service.get_connection(self.master_account_id)
        if not master_conn:
            self.logger.warning("主账户未连接，跟单暂停。")
            return
            
        master_positions = master_conn.get_positions()
        if master_positions is None:
            self.logger.warning("获取主账户持仓失败。")
            return

        # 1. 遍历主账户的每一个持仓
        for pos in master_positions:
            if pos.ticket in self.copied_positions:
                continue # 已经复制过
                
            if pos.ticket in self.copy_in_progress:
                continue # 正在复制中

            self.logger.info(f"检测到主账户新持仓: {pos.ticket} (Symbol: {pos.symbol}, Type: {pos.type}, Vol: {pos.volume})")
            self.copy_in_progress.add(pos.ticket)

            # 2. 为每一个从账户执行复制
            for slave_id in self.slave_account_ids:
                slave_conn = self.account_service.get_connection(slave_id)
                if not slave_conn:
                    self.logger.warning(f"从账户 {slave_id} 未连接，跳过。")
                    continue
                
                self._execute_copy_for_slave(master_conn, slave_conn, pos, lots_multiplier, reverse_copy)
            
            # TODO: 这里的逻辑需要更健壮，如果所有从账户都成功了，
            # 才将其加入 copied_positions。目前暂时简化处理。
            # self.copied_positions[pos.ticket] = ... 
            # self.copy_in_progress.remove(pos.ticket)


        # 3. (TODO) 处理平仓逻辑：
        # 检查 copied_positions 中的 master_ticket_id 是否还在 master_positions 中
        # 如果不在了，说明主账户已平仓，从账户也应平仓。
        pass


    def _execute_copy_for_slave(self, 
                                master_conn: MT5Connection, 
                                slave_conn: MT5Connection, 
                                master_pos: Position, 
                                lots_multiplier: float, 
                                reverse_copy: bool):
        """
        (这部分逻辑基本来自你原有的 _copy_trades_for_slave)
        """
        slave_account_id = slave_conn.login
        self.logger.info(f"正在为从账户 {slave_account_id} 复制持仓 {master_pos.ticket}...")
        
        # 计算手数
        volume = round(master_pos.volume * lots_multiplier, 2)
        if volume <= 0:
            volume = 0.01 # (应使用品种的最小手数)

        # 计算订单类型（正向/反向）
        order_type = master_pos.type
        if reverse_copy:
            order_type = 1 - order_type # 0 (BUY) 变 1 (SELL), 1 变 0

        # (省略了你原有的'copy_mode'检查，这里假设总是开仓)
        
        result = slave_conn.create_market_order(
            symbol=master_pos.symbol,
            volume=volume,
            order_type=order_type,
            magic=master_pos.magic,
            comment=f"Copy from {self.master_account_id} / Tkt {master_pos.ticket}"
        )
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.logger.info(f"账户 {slave_account_id} 复制成功。新票据: {result.order}")
            # 记录复制关系
            self.copied_positions[master_pos.ticket] = result.order
        else:
            self.logger.error(f"账户 {slave_account_id} 复制失败。{result.comment if result else 'N/A'}")
            
        # 无论成功与否，都结束本次复制尝试
        self.copy_in_progress.remove(master_pos.ticket)

    def shutdown(self):
        self.logger.info("跟单服务已停止。")
        # 清理状态
        self.copied_positions.clear()
        self.copy_in_progress.clear()

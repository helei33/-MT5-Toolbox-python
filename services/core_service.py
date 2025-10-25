import threading
import time
import traceback
from queue import Queue, Empty

import MetaTrader5 as mt5

from services.strategy_service import StrategyRunner
from mt5_utils import _connect_mt5
from constants import NUM_SLAVES, NUM_MASTERS
from data_manager import DataManager

# --- Global Queues and Events ---
task_queue = Queue()
log_queue = Queue()
account_info_queue = Queue()
data_task_queue = Queue()
data_log_queue = Queue()
backtest_result_queue = Queue()
stop_event = threading.Event()
mt5_lock = threading.Lock()
# --- End of Global Queues and Events ---

class CoreService:
    def __init__(self, app_config, available_strategies, data_manager):
        self.app_config = app_config
        self.available_strategies = available_strategies
        self.data_manager = data_manager

        # State moved from TradeCopierApp
        self.strategy_instances = {}
        self.connection_failures = {}
        self.logged_in_accounts = set()
        self.verified_passwords = set()
        self.pending_verification_config = {}
        self.per_slave_mapping = {}
        self.equity_data = {}
        self.risk_config = {'enable_global_equity_stop': False, 'global_equity_stop_level': 0.0}

        # Threads
        self.worker = threading.Thread(target=self.worker_thread, daemon=True)
        self.data_sync_worker = threading.Thread(target=self.data_sync_worker_thread, daemon=True)

    def start(self):
        log_queue.put("核心服务已启动。")
        self.worker.start()
        self.data_sync_worker.start()

    def stop(self):
        stop_event.set()

    def update_app_state(self, state):
        """Allows the UI to push state changes to the service."""
        self.logged_in_accounts = state.get('logged_in_accounts', self.logged_in_accounts)
        self.pending_verification_config = state.get('pending_verification_config', self.pending_verification_config)
        self.per_slave_mapping = state.get('per_slave_mapping', self.per_slave_mapping)
        self.verified_passwords = state.get('verified_passwords', self.verified_passwords)
        log_queue.put("核心服务状态已从UI同步。")

    def worker_thread(self):
        log_queue.put("后台引擎已启动，等待指令...")
        while not stop_event.is_set():
            try:
                try:
                    task = task_queue.get(block=False)
                    action = task.get('action')

                    if action == 'UPDATE_STATE':
                        self.update_app_state(task.get('payload', {}))
                    elif action == 'CLOSE_ALL_ACCOUNTS_FORCEFULLY':
                        with mt5_lock:
                            log_queue.put("指令收到：一键清仓所有账户。")
                            all_accounts_to_clear = {}
                            for i in range(1, NUM_MASTERS + 1): all_accounts_to_clear[f'master{i}'] = self.app_config[f'master{i}']
                            for i in range(1, NUM_SLAVES + 1): all_accounts_to_clear[f'slave{i}'] = self.app_config[f'slave{i}']
                            for acc_id, config in all_accounts_to_clear.items():
                                self._stop_strategy_sync(acc_id)
                                self._close_all_trades_for_single_account(acc_id, config)
                    elif action == 'CLOSE_SINGLE_TRADE':
                        with mt5_lock:
                            account_id, ticket = task['account_id'], task['ticket']
                            log_queue.put(f"指令收到：平仓账户 {account_id} 的订单 {ticket}。")
                            config_to_use = self.app_config[account_id]
                            self._close_single_trade_for_account(account_id, config_to_use, int(ticket))
                    elif action == 'STOP_AND_CLOSE':
                        with mt5_lock:
                            account_id = task['account_id']
                            self._stop_strategy_sync(account_id)
                            self._close_all_trades_for_single_account(account_id, self.app_config[account_id])
                    elif action == 'MODIFY_SLTP':
                        with mt5_lock:
                            account_id, ticket, sl, tp = task['account_id'], task['ticket'], task['sl'], task['tp']
                            log_queue.put(f"指令收到：修改账户 {account_id} 订单 {ticket} 的SL/TP。")
                            config = self.app_config[account_id]
                            _, mt5_conn, _ = _connect_mt5(config, log_queue, f"修改SL/TP账户 {account_id}")
                            if mt5_conn:
                                try:
                                    request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "order": ticket, "sl": sl, "tp": tp}
                                    mt5_conn.order_send(request)
                                finally:
                                    mt5_conn.shutdown()
                    elif action == 'START_STRATEGY':
                        account_id, strategy_name, params = task['account_id'], task['strategy_name'], task['params']
                        if account_id in self.strategy_instances:
                            log_queue.put(f"账户 {account_id} 的策略已在运行中，请先停止。 ")
                            continue
                        log_queue.put(f"正在为 {account_id} 初始化策略 '{strategy_name}'...")
                        try:
                            acc_config = self.app_config[account_id]
                            acc_config['account_id'] = account_id
                            strategy_info = self.available_strategies.get(strategy_name)
                            if not strategy_info:
                                log_queue.put(f"错误：找不到名为 '{strategy_name}' 的策略。" )
                                continue
                            
                            final_params = self._prepare_strategy_params(acc_config, params, strategy_info)

                            runner = StrategyRunner(
                                strategy_class=strategy_info['class'],
                                config=acc_config,
                                log_queue=log_queue,
                                params=final_params
                            )
                            runner.start()
                            self.strategy_instances[account_id] = runner
                            log_queue.put(f"账户 {account_id} 的策略 '{strategy_name}' 已成功启动。" )
                            account_info_queue.put({'id': account_id, 'status': 'strategy_running'})
                        except Exception as e:
                            log_queue.put(f"启动策略失败 ({account_id}): {e}\n{traceback.format_exc()}")
                    elif action == 'STOP_STRATEGY':
                        account_id = task.get('account_id')
                        if account_id in self.strategy_instances:
                            log_queue.put(f"正在停止账户 {account_id} 的策略...")
                            self._stop_strategy_sync(account_id)
                            log_queue.put(f"账户 {account_id} 的策略已停止。" )
                            account_info_queue.put({'id': account_id, 'status': 'inactive'})
                        else:
                            log_queue.put(f"指令忽略：账户 {account_id} 未在运行策略。" )
                except Empty:
                    pass

                self._process_logged_in_accounts()

                # This logic should be part of a future RiskService
                if self.app_config.getboolean('DEFAULT', 'enable_global_equity_stop', fallback=False):
                    try:
                        stop_level = self.app_config.getfloat('DEFAULT', 'global_equity_stop_level', fallback=0.0)
                        total_equity = sum(e for e in self.equity_data.values() if e is not None)
                        active_accounts_count = sum(1 for e in self.equity_data.values() if e is not None)
                        if active_accounts_count > 0 and total_equity < stop_level:
                                log_queue.put(f"!!! 全局风控触发 !!! 所有账户总净值 {total_equity:,.2f} 低于阈值 {stop_level:,.2f}。" )
                                log_queue.put("正在自动执行[一键清仓所有账户]...")
                                task_queue.put({'action': 'CLOSE_ALL_ACCOUNTS_FORCEFULLY'})
                                # This should send a message back to UI to update the checkbox
                                # For now, we can't directly modify app_config or UI state here.
                    except (ValueError, KeyError):
                        pass

            except Exception as e:
                log_queue.put(f"后台线程主循环发生严重错误: {e}")
            finally:
                time.sleep(float(self.app_config.get('DEFAULT', 'check_interval', fallback='0.2')))

    def _get_account_details(self, mt5_instance, account_id, ping):
        info = mt5_instance.account_info()
        if not info:
            account_info_queue.put({'id': account_id, 'status': 'error', 'ping': ping})
            return

        positions = mt5_instance.positions_get() or []
        orders = mt5_instance.orders_get() or []
        
        self.equity_data[account_id] = info.equity

        account_info_queue.put({
            'id': account_id,
            'balance': info.balance,
            'equity': info.equity,
            'profit': info.profit,
            'credit': getattr(info, 'credit', 0.0),
            'swap': getattr(info, 'swap', 0.0),
            'margin_free': info.margin_free,
            'margin_level': info.margin_level,
            'total_positions': len(positions) + len(orders),
            'total_volume': sum(p.volume for p in positions),
            'ping': ping,
            'positions_data': list(positions) + list(orders)
        })

    def _calculate_volume(self, master_trade, master_info, slave_info, slave_config, mt5_conn, symbol):
        master_volume = master_trade.volume if hasattr(master_trade, 'volume') else master_trade.volume_initial
        volume_mode = slave_config.get('volume_mode', 'same_as_master')
        volume = master_volume

        if volume_mode == 'fixed_lot':
            try:
                volume = float(slave_config.get('fixed_lot_size', '0.01'))
            except (ValueError, TypeError):
                volume = 0.01 # Fallback
                
        elif volume_mode == 'equity_ratio':
            if master_info and master_info.equity > 0 and slave_info and slave_info.equity > 0:
                ratio = slave_info.equity / master_info.equity
                volume = master_volume * ratio

        symbol_info = mt5_conn.symbol_info(symbol)
        if symbol_info:
            lot_step = symbol_info.volume_step
            min_lot = symbol_info.volume_min
            max_lot = symbol_info.volume_max

            volume = max(min_lot, volume)
            volume = round(volume / lot_step) * lot_step
            volume = min(max_lot, volume)

        return volume

    def _copy_trades_for_slave(self, slave_id, slave_config, master_trades_dict, master_info):
        is_enabled = slave_config.getboolean('enabled', fallback=False)
        if not is_enabled:
            account_info_queue.put({'id': slave_id, 'status': 'disabled'})
            return

        ping, mt5_conn, err_code = _connect_mt5(slave_config, log_queue, f"从账户 {slave_id}")
        if not mt5_conn:
            self.connection_failures[slave_id] = self.connection_failures.get(slave_id, 0) + 1
            return
        try:
            self._get_account_details(mt5_conn, slave_id, ping)
            account_info_queue.put({'id': slave_id, 'status': 'copying'})
            
            slave_info = mt5_conn.account_info()
            self.connection_failures[slave_id] = 0
            if not slave_info: return

            copy_mode = slave_config.get('copy_mode', 'forward')
            slave_magic = int(slave_config.get('magic'))
            current_slave_map = self.per_slave_mapping.get(slave_id, {})
            default_rule, default_text = slave_config.get('default_symbol_rule', 'none'), slave_config.get('default_symbol_text', '')
            
            slave_trades = list(mt5_conn.positions_get() or []) + list(mt5_conn.orders_get() or [])
            slave_copied_trades = {}
            for t in slave_trades:
                if t.magic == slave_magic and t.comment and t.comment.startswith("F "):
                    try:
                        master_ticket_ref = int(t.comment.split(" ")[-1])
                        slave_copied_trades[master_ticket_ref] = t
                    except (ValueError, IndexError):
                        continue

            for master_ticket_ref, slave_trade in slave_copied_trades.items():
                if master_ticket_ref not in master_trades_dict:
                    log_queue.put(f"[{slave_id}] 检测到主单 {master_ticket_ref} 已关闭，正在平掉从单 {slave_trade.ticket}..." )
                    if slave_trade.type in {mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL}:
                        tick = mt5_conn.symbol_info_tick(slave_trade.symbol)
                        if not tick: continue
                        close_request = {
                            "action": mt5.TRADE_ACTION_DEAL, "position": slave_trade.ticket, "symbol": slave_trade.symbol,
                            "volume": slave_trade.volume, "type": mt5.ORDER_TYPE_SELL if slave_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                            "price": tick.bid if slave_trade.type == mt5.ORDER_TYPE_BUY else tick.ask, "deviation": 200, "magic": slave_magic,
                            "comment": f"Close F {master_ticket_ref}", "type_filling": mt5.ORDER_FILLING_IOC
                        }
                        mt5_conn.order_send(close_request)
                    else:
                        remove_request = {"action": mt5.TRADE_ACTION_REMOVE, "order": slave_trade.ticket}
                        mt5_conn.order_send(remove_request)

            for master_ticket_ref, slave_trade in slave_copied_trades.items():
                if master_ticket_ref in master_trades_dict:
                    master_trade = master_trades_dict[master_ticket_ref]
                    expected_sl, expected_tp = (master_trade.tp, master_trade.sl) if copy_mode == 'reverse' else (master_trade.sl, master_trade.tp)
                    sl_mismatch = abs(slave_trade.sl - expected_sl) > 1e-9
                    tp_mismatch = abs(slave_trade.tp - expected_tp) > 1e-9
                    if sl_mismatch or tp_mismatch:
                        log_queue.put(f"[{slave_id}] 检测到主单 {master_ticket_ref} 的SL/TP已修改，正在更新从单 {slave_trade.ticket}..." )
                        modify_request = {"action": mt5.TRADE_ACTION_SLTP, "position": slave_trade.ticket, "sl": expected_sl, "tp": expected_tp}
                        mt5_conn.order_send(modify_request)

            for m_ticket, master_trade in master_trades_dict.items():
                if m_ticket not in slave_copied_trades and master_trade.magic != slave_magic:
                    master_symbol = master_trade.symbol
                    slave_symbol = master_symbol
                    mapping_rule_tuple = current_slave_map.get(master_symbol)
                    if mapping_rule_tuple:
                        rule, text = mapping_rule_tuple
                        if rule == 'replace': slave_symbol = text
                    elif default_rule != 'none' and default_text:
                        if default_rule == 'suffix': slave_symbol += default_text
                        elif default_rule == 'prefix': slave_symbol = default_text + slave_symbol
                    if not mt5_conn.symbol_select(slave_symbol, True):
                        log_queue.put(f"[{slave_id}] 无法选择品种 {slave_symbol}，跳过订单 {m_ticket}")
                        continue
                    volume = self._calculate_volume(master_trade, master_info, slave_info, slave_config, mt5_conn, slave_symbol)
                    sl, tp = (master_trade.tp, master_trade.sl) if copy_mode == 'reverse' else (master_trade.sl, master_trade.tp)
                    type_map_reverse = {mt5.ORDER_TYPE_BUY: mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_SELL: mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT: mt5.ORDER_TYPE_SELL_STOP, mt5.ORDER_TYPE_SELL_LIMIT: mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP: mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP: mt5.ORDER_TYPE_BUY_LIMIT}
                    trade_type = type_map_reverse.get(master_trade.type) if copy_mode == 'reverse' else master_trade.type
                    if trade_type is None and copy_mode == 'reverse': continue
                    request = {"symbol": slave_symbol, "volume": volume, "type": trade_type, "sl": sl, "tp": tp, "magic": slave_magic, "comment": f"F {m_ticket}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC, "deviation": 200}
                    is_pending = not (master_trade.type in {mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL})
                    if is_pending:
                        request.update({"action": mt5.TRADE_ACTION_PENDING, "price": master_trade.price_open})
                    else:
                        tick = mt5_conn.symbol_info_tick(slave_symbol)
                        if not tick: continue
                        price = tick.ask if trade_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_LIMIT] else tick.bid
                        request.update({"action": mt5.TRADE_ACTION_DEAL, "price": price})
                    log_queue.put(f"[{slave_id}] 正在为 主单 {m_ticket} 创建从单..." )
                    mt5_conn.order_send(request)
        except Exception as e:
            log_queue.put(f"处理从账户 {slave_id} 时出错: {e}")
        finally:
            if mt5_conn: mt5_conn.shutdown()

    def _process_logged_in_accounts(self):
        MAX_CONN_FAILURES = 10
        logged_in_accounts_copy = self.logged_in_accounts.copy()
        if not logged_in_accounts_copy:
            return

        with mt5_lock:
            for acc_id in list(self.pending_verification_config.keys()):
                if acc_id in logged_in_accounts_copy:
                    temp_config = self.pending_verification_config[acc_id]
                    log_queue.put(f"正在使用新配置验证账户 {acc_id}..." )
                    _, mt5_conn, _ = _connect_mt5(temp_config, log_queue, f"验证账户 {acc_id}")
                    if mt5_conn:
                        log_queue.put(f"账户 {acc_id} 验证成功！新配置已应用。" )
                        for key, value in temp_config.items():
                            self.app_config.set(acc_id, key, str(value))
                        self.verified_passwords.add(acc_id)
                        self.connection_failures[acc_id] = 0
                        mt5_conn.shutdown()
                    else:
                        log_queue.put(f"账户 {acc_id} 验证失败。配置未更改。" )
                        self.connection_failures[acc_id] = MAX_CONN_FAILURES + 1
                    del self.pending_verification_config[acc_id]

            slaves_by_master = {}
            for i in range(1, NUM_SLAVES + 1):
                slave_id = f'slave{i}'
                if slave_id not in logged_in_accounts_copy or slave_id in self.strategy_instances: continue
                slave_config = self.app_config[slave_id]
                if not slave_config.getboolean('enabled', fallback=False): continue
                master_id_to_follow = slave_config.get('follow_master_id', 'master1')
                if master_id_to_follow not in slaves_by_master:
                    slaves_by_master[master_id_to_follow] = []
                slaves_by_master[master_id_to_follow].append((slave_id, slave_config))
            
            for master_id, slave_group in slaves_by_master.items():
                if master_id in logged_in_accounts_copy:
                    master_config = self.app_config[master_id]
                    master_config['account_id'] = master_id
                    if self.connection_failures.get(master_id, 0) >= MAX_CONN_FAILURES:
                        account_info_queue.put({'id': master_id, 'status': 'locked'})
                        continue
                    ping, mt5_master, err_code = _connect_mt5(master_config, log_queue, f"主账户 {master_id}")
                    if not mt5_master:
                        self.connection_failures[master_id] = self.connection_failures.get(master_id, 0) + 1
                        if err_code == 1045:
                            self.connection_failures[master_id] = MAX_CONN_FAILURES + 1
                        for s_id, _ in slave_group: account_info_queue.put({'id': s_id, 'status': 'inactive'})
                        continue
                    try:
                        self.connection_failures[master_id] = 0
                        self._get_account_details(mt5_master, master_id, ping)
                        account_info_queue.put({'id': master_id, 'status': 'connected'})
                        master_info = mt5_master.account_info()
                        if not master_info: continue
                        master_trades_dict = {t.ticket: t for t in list(mt5_master.positions_get() or []) + list(mt5_master.orders_get() or [])}
                        for slave_id, slave_config in slave_group:
                            slave_config['account_id'] = slave_id
                            if self.connection_failures.get(slave_id, 0) >= MAX_CONN_FAILURES:
                                account_info_queue.put({'id': slave_id, 'status': 'locked'})
                                continue
                            self._copy_trades_for_slave(slave_id, slave_config, master_trades_dict, master_info)
                    finally:
                        mt5_master.shutdown()

            for acc_id in logged_in_accounts_copy:
                is_strategy_running = acc_id in self.strategy_instances
                is_copying = any(acc_id == s_id for m_id in slaves_by_master for s_id, _ in slaves_by_master.get(m_id, []))
                if is_copying or is_strategy_running: continue
                if self.connection_failures.get(acc_id, 0) >= MAX_CONN_FAILURES:
                    account_info_queue.put({'id': acc_id, 'status': 'locked'})
                    continue
                config = self.app_config[acc_id]
                config['account_id'] = acc_id
                ping, mt5_conn, err_code = _connect_mt5(config, log_queue, f"账户 {acc_id}")
                if mt5_conn:
                    self.connection_failures[acc_id] = 0
                    self._get_account_details(mt5_conn, acc_id, ping)
                    mt5_conn.shutdown()
                    account_info_queue.put({'id': acc_id, 'status': 'connected'})
                else:
                    self.connection_failures[acc_id] = self.connection_failures.get(acc_id, 0) + 1
                    if err_code == 1045:
                        self.connection_failures[acc_id] = MAX_CONN_FAILURES + 1

    def data_sync_worker_thread(self):
        data_log_queue.put("[DataManager] 数据同步线程已启动")
        while not stop_event.is_set():
            try:
                try:
                    task = data_task_queue.get(block=False)
                    symbols = task.get('symbols', [])
                    timeframes = task.get('timeframes', [])
                    start_date = task.get('start_date')
                    end_date = task.get('end_date')
                    data_log_queue.put(f"[DataManager] 开始处理数据同步任务: {symbols} {timeframes}")
                    try:
                        master1_config = self.app_config['master1']
                        if not all(master1_config.get(k) for k in ['path', 'login', 'password', 'server']):
                            data_log_queue.put("[DataManager] 错误: 主账户1配置不完整，请先配置主账户1")
                            continue
                    except KeyError:
                        data_log_queue.put("[DataManager] 错误: 找不到主账户1配置，请先配置主账户1")
                        continue
                    data_log_queue.put("[DataManager] 正在连接MT5服务器...")
                    success = self.data_manager.sync_data(
                        symbols=symbols,
                        timeframes=timeframes,
                        mt5_config=master1_config,
                        log_queue=data_log_queue,
                        start_date_str=start_date,
                        end_date_str=end_date
                    )
                    if success:
                        data_log_queue.put("[DataManager] 数据同步任务完成")
                    else:
                        data_log_queue.put("[DataManager] 数据同步任务失败")
                except Empty:
                    pass
            except Exception as e:
                data_log_queue.put(f"[DataManager] 数据同步线程发生错误: {e}\n{traceback.format_exc()}")
            finally:
                time.sleep(0.1)

    def _stop_strategy_sync(self, account_id):
        if account_id in self.strategy_instances:
            strategy = self.strategy_instances.pop(account_id)
            strategy.stop_strategy()
            strategy.join(timeout=5)
            self.equity_data[account_id] = None

    def _prepare_strategy_params(self, acc_config, acc_specific_params, strategy_info):
        strategy_name = strategy_info['class'].strategy_name
        params_config = strategy_info.get('params_config', {})
        final_params = {}
        for key, config in params_config.items():
            if 'default' in config: final_params[key] = config['default']
        global_section = f"{strategy_name}_Global"
        if self.app_config.has_section(global_section):
            for key, config in params_config.items():
                if self.app_config.has_option(global_section, key):
                    final_params[key] = self.app_config.get(global_section, key)
        account_section = f"{acc_config['account_id']}_{strategy_name}"
        if self.app_config.has_section(account_section):
            for key, config in params_config.items():
                if self.app_config.has_option(account_section, key):
                    final_params[key] = self.app_config.get(account_section, key)
        return final_params

    def _close_all_trades_for_single_account(self, account_id, config):
        with mt5_lock:
            log_queue.put(f"--- 正在清空账户 {account_id} ---")
            _, mt5_conn, _ = _connect_mt5(config, log_queue, f"清仓账户 {account_id}")
            if not mt5_conn: return
            try:
                for pos in reversed(mt5_conn.positions_get() or []):
                    tick = mt5_conn.symbol_info_tick(pos.symbol)
                    if not tick: continue
                    price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                    request = {"action": mt5.TRADE_ACTION_DEAL, "position": pos.ticket, "symbol": pos.symbol, "volume": pos.volume, "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY, "price": price, "deviation": 200, "magic": pos.magic, "comment": "Closed by tool", "type_filling": mt5.ORDER_FILLING_IOC}
                    mt5_conn.order_send(request)
                for order in reversed(mt5_conn.orders_get() or []):
                    request = {"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket}
                    mt5_conn.order_send(request)
                log_queue.put(f"  > [{account_id}] 清空指令已发送。" )
            finally:
                mt5_conn.shutdown()

    def _close_single_trade_for_account(self, account_id, config, ticket):
        with mt5_lock:
            log_queue.put(f"--- 正在为账户 {account_id} 处理订单 {ticket} ---")
            _, mt5_conn, _ = _connect_mt5(config, log_queue, f"平仓账户 {account_id}")
            if not mt5_conn: return
            try:
                position = mt5_conn.positions_get(ticket=ticket)
                if position and len(position) > 0:
                    pos = position[0]
                    tick_info = mt5_conn.symbol_info_tick(pos.symbol)
                    if not tick_info: return
                    price = tick_info.bid if pos.type == mt5.ORDER_TYPE_BUY else tick_info.ask
                    request = {"action": mt5.TRADE_ACTION_DEAL, "position": pos.ticket, "symbol": pos.symbol, "volume": pos.volume, "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY, "price": price, "deviation": 200, "magic": pos.magic, "comment": "Closed by tool", "type_filling": mt5.ORDER_FILLING_IOC}
                    mt5_conn.order_send(request)
                    return

                order = mt5_conn.orders_get(ticket=ticket)
                if order and len(order) > 0:
                    request = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
                    mt5_conn.order_send(request)
                    return
            finally:
                mt5_conn.shutdown()

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import MetaTrader5 as mt5
import time
import threading
import configparser
import os
import shutil
import importlib.util
import sys
import webbrowser
import tkinter.font as tkFont
from queue import Queue, Empty
from datetime import datetime
from cryptography.fernet import Fernet

from manual import MANUAL_TEXT
from strategy_guide import GUIDE_TEXT
from constants import NUM_SLAVES, NUM_MASTERS, STRATEGIES_DIR, CONFIG_FILE, APP_DATA_DIR
from core_utils import BaseStrategy, encrypt_password, decrypt_password
from ui_utils import ScrolledFrame, ModifySLTPWindow, StrategyConfigWindow

# 尝试导入ttkthemes以实现更现代的Win11风格界面
try:
    from ttkthemes import ThemedTk
    use_themed_tk = True
except ImportError:
    use_themed_tk = False
    # 如果导入失败，创建一个假的ThemedTk类以保持代码兼容性
    class FakeThemedTk(tk.Tk):
        def __init__(self):
            super().__init__()
        def set_theme(self, theme_name):
            pass
    ThemedTk = FakeThemedTk

# --- 全局变量 ---
task_queue = Queue()
log_queue = Queue()
account_info_queue = Queue()
stop_event = threading.Event()

def _connect_mt5(config, log_queue, account_id_str):
    """
    辅助函数：连接到单个MT5账户。
    返回 (ping, mt5_instance, error_code)。
    """
    if not all(config.get(k) for k in ['path', 'login', 'password', 'server']):
        account_info_queue.put({'id': config['account_id'], 'status': 'config_incomplete'})
        return None, None, None

    start_time = time.perf_counter()
    initialized = mt5.initialize(
        path=config['path'],
        login=int(config['login']),
        password=config['password'],
        server=config['server'],
        timeout=10000
    )
    ping = (time.perf_counter() - start_time) * 1000

    if not initialized:
        error_code, error_desc = mt5.last_error()
        log_queue.put(f"连接 {account_id_str} 失败: {error_desc} (代码: {error_code})")
        account_info_queue.put({'id': config['account_id'], 'status': 'error', 'ping': -1})
        return None, None, error_code
    
    return ping, mt5, mt5.RES_S_OK

def _get_account_details(mt5_instance, account_id, ping):
    """辅助函数：获取账户详细信息"""
    info = mt5_instance.account_info()
    if not info:
        account_info_queue.put({'id': account_id, 'status': 'error', 'ping': ping})
        return

    positions = mt5_instance.positions_get() or []
    orders = mt5_instance.orders_get() or []
    
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

def _calculate_volume(master_trade, master_info, slave_info, slave_config, mt5_conn, symbol):
    """辅助函数：根据配置计算跟单手数"""
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

    # 规格化手数以符合品种要求
    symbol_info = mt5_conn.symbol_info(symbol)
    if symbol_info:
        lot_step = symbol_info.volume_step
        min_lot = symbol_info.volume_min
        max_lot = symbol_info.volume_max

        volume = max(min_lot, volume) # 确保不小于最小手数
        volume = round(volume / lot_step) * lot_step # 按步长规格化
        volume = min(max_lot, volume) # 确保不大于最大手数

    return volume

def _copy_trades_for_slave(self, slave_id, slave_config, master_trades_dict, master_info, per_slave_mapping, log_queue):
    """为单个从账户执行跟单逻辑"""
    is_enabled = slave_config.getboolean('enabled', fallback=False)
    if not is_enabled:
        account_info_queue.put({'id': slave_id, 'status': 'disabled'})
        return

    ping, mt5_conn, err_code = _connect_mt5(slave_config, log_queue, f"从账户 {slave_id}")
    if not mt5_conn:
        self.connection_failures[slave_id] = self.connection_failures.get(slave_id, 0) + 1
        return
    try:
        _get_account_details(mt5_conn, slave_id, ping)
        account_info_queue.put({'id': slave_id, 'status': 'copying'})
        
        slave_info = mt5_conn.account_info()
        self.connection_failures[slave_id] = 0
        if not slave_info: return

        copy_mode = slave_config.get('copy_mode', 'forward')
        slave_magic = int(slave_config.get('magic'))
        current_slave_map = per_slave_mapping.get(slave_id, {})
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

        # 1. 同步平仓: 主账户已平，从账户未平
        for master_ticket_ref, slave_trade in slave_copied_trades.items():
            if master_ticket_ref not in master_trades_dict:
                log_queue.put(f"[{slave_id}] 检测到主单 {master_ticket_ref} 已关闭，正在平掉从单 {slave_trade.ticket}...")
                if slave_trade.type in {mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL}: # 持仓
                    tick = mt5_conn.symbol_info_tick(slave_trade.symbol)
                    if tick:
                        close_request = {
                            "action": mt5.TRADE_ACTION_DEAL, "position": slave_trade.ticket, "symbol": slave_trade.symbol,
                            "volume": slave_trade.volume, "type": mt5.ORDER_TYPE_SELL if slave_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                            "price": tick.bid if slave_trade.type == mt5.ORDER_TYPE_BUY else tick.ask, "deviation": 200, "magic": slave_magic,
                            "comment": f"Close F {master_ticket_ref}", "type_filling": mt5.ORDER_FILLING_IOC
                        }
                        mt5_conn.order_send(close_request)
                else: # 挂单
                    remove_request = {"action": mt5.TRADE_ACTION_REMOVE, "order": slave_trade.ticket}
                    mt5_conn.order_send(remove_request)

        # 3. 同步修改止盈止损
        for master_ticket_ref, slave_trade in slave_copied_trades.items():
            if master_ticket_ref in master_trades_dict:
                master_trade = master_trades_dict[master_ticket_ref]

                if copy_mode == 'reverse':
                    expected_sl, expected_tp = master_trade.tp, master_trade.sl
                else:
                    expected_sl, expected_tp = master_trade.sl, master_trade.tp

                sl_mismatch = abs(slave_trade.sl - expected_sl) > 1e-9
                tp_mismatch = abs(slave_trade.tp - expected_tp) > 1e-9

                if sl_mismatch or tp_mismatch:
                    log_queue.put(f"[{slave_id}] 检测到主单 {master_ticket_ref} 的SL/TP已修改，正在更新从单 {slave_trade.ticket}...")
                    is_position = hasattr(slave_trade, 'profit')
                    
                    if is_position:
                        # For existing positions, use TRADE_ACTION_SLTP
                        # 对于持仓单，symbol是可选的，但为了清晰起见，我们保留它
                        modify_request = {
                            "action": mt5.TRADE_ACTION_SLTP,
                            "position": slave_trade.ticket,
                            "sl": expected_sl,
                            "tp": expected_tp,
                        }
                    else:
                        # For pending orders, use TRADE_ACTION_MODIFY
                        # 对于挂单，price 和 symbol 都是必填项
                        modify_request = {
                            "action": mt5.TRADE_ACTION_MODIFY,
                            "order": slave_trade.ticket,
                            "price": slave_trade.price_open, 
                            "sl": expected_sl,
                            "tp": expected_tp,
                            "symbol": slave_trade.symbol
                        }
                    
                    result = mt5_conn.order_send(modify_request)
                    if result and result.retcode != mt5.TRADE_RETCODE_DONE:
                        log_queue.put(f"[{slave_id}] 更新从单 {slave_trade.ticket} SL/TP 失败: {result.comment} (代码: {result.retcode})")

        # 2. 同步开仓: 主账户有，从账户没有
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

                volume = _calculate_volume(master_trade, master_info, slave_info, slave_config, mt5_conn, slave_symbol)
                
                sl, tp = (master_trade.tp, master_trade.sl) if copy_mode == 'reverse' else (master_trade.sl, master_trade.tp)
                type_map_reverse = {
                    mt5.ORDER_TYPE_BUY: mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_SELL: mt5.ORDER_TYPE_BUY,
                    mt5.ORDER_TYPE_BUY_LIMIT: mt5.ORDER_TYPE_SELL_STOP, mt5.ORDER_TYPE_SELL_LIMIT: mt5.ORDER_TYPE_BUY_STOP,
                    mt5.ORDER_TYPE_BUY_STOP: mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP: mt5.ORDER_TYPE_BUY_LIMIT
                }
                trade_type = type_map_reverse.get(master_trade.type) if copy_mode == 'reverse' else master_trade.type
                if trade_type is None and copy_mode == 'reverse': continue

                request = {
                    "symbol": slave_symbol, "volume": volume, "type": trade_type, "sl": sl, "tp": tp,
                    "magic": slave_magic, "comment": f"F {m_ticket}", "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC, "deviation": 200
                }

                is_pending = not (master_trade.type in {mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL})
                if is_pending:
                    request.update({"action": mt5.TRADE_ACTION_PENDING, "price": master_trade.price_open})
                else:
                    tick = mt5_conn.symbol_info_tick(slave_symbol)
                    if not tick: continue
                    price = tick.ask if trade_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_LIMIT] else tick.bid
                    request.update({"action": mt5.TRADE_ACTION_DEAL, "price": price})
                
                log_queue.put(f"[{slave_id}] 正在为 主单 {m_ticket} 创建从单...")
                mt5_conn.order_send(request)

    except Exception as e:
        log_queue.put(f"处理从账户 {slave_id} 时出错: {e}")
    finally:
        if mt5_conn: mt5_conn.shutdown()

class TradeCopierApp(ThemedTk):
    def __init__(self):
        super().__init__()
        if use_themed_tk: self.set_theme("arc")
        self.title("MT5 交易工具箱 (Python版)")
        self.geometry("1060x660")
        self.minsize(1060, 660)
        
        self.app_config = configparser.ConfigParser()
        self.last_known_good_config = configparser.ConfigParser()
        self.per_slave_mapping = {}
        self.master_vars_list = []
        self.slave_vars_list = []
        self.default_vars = {}
        self.summary_vars = {}
        self.equity_data = {}
        self.available_strategies = {}
        self.global_strategy_param_vars = {}
        self.current_slave_id_view = 'slave1'
        self.positions_tab_slave_selector_var = tk.StringVar()
        self.positions_tab_master_selector_var = tk.StringVar()
        self.selected_strategy_in_library = None
        self.strategy_instances = {}
        self.pending_verification_config = {}
        self.verified_passwords = set()
        self.connection_failures = {}
        self.logged_in_accounts = set()
        self.account_widgets_to_disable = {}

        self.volume_mode_map = {"与主账户相同": "same_as_master", "固定手数": "fixed_lot", "按净值比例": "equity_ratio"}
        self.volume_mode_map_reverse = {v: k for k, v in self.volume_mode_map.items()}
        self.master_id_map = {f"主账户 {i}": f"master{i}" for i in range(1, NUM_MASTERS + 1)}
        self.master_id_map_reverse = {v: k for k, v in self.master_id_map.items()}
        self.vcmd_lot = (self.register(self._validate_lot_entry), '%P')

        self.summary_vars = {
            'slave_total_balance': tk.StringVar(value="0.00"), 'slave_total_equity': tk.StringVar(value="0.00"),
            'slave_total_margin_free': tk.StringVar(value="0.00"), 'slave_total_profit': tk.StringVar(value="0.00"),
            'total_balance': tk.StringVar(value="0.00"), 'total_equity': tk.StringVar(value="0.00"),
            'total_margin_free': tk.StringVar(value="0.00"), 'total_profit': tk.StringVar(value="0.00"),
            'total_positions': tk.StringVar(value="0"),
            'master_total_balance': tk.StringVar(value="0.00"), 'master_total_equity': tk.StringVar(value="0.00"),
            'master_total_margin_free': tk.StringVar(value="0.00"), 'master_total_profit': tk.StringVar(value="0.00"),
            'master_total_positions': tk.StringVar(value="0"),
            'slave_total_positions': tk.StringVar(value="0"),
        }
        self.total_equity_var = tk.StringVar(value="汇总净值: --")
        self.total_profit_var = tk.StringVar(value="汇总盈亏: --")

        style = ttk.Style(self)
        style.map('Treeview', background=[('selected', '#0078D7')], foreground=[('selected', 'white')])
        style.configure("Treeview.Heading", font=('Segoe UI', 9, 'bold'))

        self.load_config()
        self.create_widgets()
        self.discover_strategies()
        self.refresh_all_strategy_uis()

        self.update_log()
        self.update_account_info()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.after(100, self._start_worker_thread)

    def worker_thread(self):
        log_queue.put("后台引擎已启动，等待指令...")
        while not stop_event.is_set():
            try:
                try:
                    task = task_queue.get(block=False)
                    action = task.get('action')

                    if action == 'CLOSE_ALL_ACCOUNTS_FORCEFULLY':
                        log_queue.put("指令收到：一键清仓所有账户。")
                        all_accounts_to_clear = {}
                        for i in range(1, NUM_MASTERS + 1): all_accounts_to_clear[f'master{i}'] = self.app_config[f'master{i}']
                        for i in range(1, NUM_SLAVES + 1): all_accounts_to_clear[f'slave{i}'] = self.app_config[f'slave{i}']
                        for acc_id, config in all_accounts_to_clear.items():
                            self._stop_strategy_sync(acc_id)
                            self._close_all_trades_for_single_account(acc_id, config)
                    elif action == 'CLOSE_SINGLE_TRADE':
                        account_id, ticket = task['account_id'], task['ticket']
                        log_queue.put(f"指令收到：平仓账户 {account_id} 的订单 {ticket}。")
                        config_to_use = self.app_config[account_id]
                        self._close_single_trade_for_account(account_id, config_to_use, int(ticket))
                    elif action == 'STOP_AND_CLOSE':
                        account_id = task['account_id']
                        self._stop_strategy_sync(account_id)
                        self._close_all_trades_for_single_account(account_id, self.app_config[account_id])
                    elif action == 'MODIFY_SLTP':
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
                            log_queue.put(f"账户 {account_id} 的策略已在运行中，请先停止。")
                            continue
                        log_queue.put(f"正在为 {account_id} 初始化策略 '{strategy_name}'...")
                        try:
                            acc_config = self.app_config[account_id]
                            acc_config['account_id'] = account_id
                            strategy_info = self.available_strategies.get(strategy_name)
                            if not strategy_info:
                                log_queue.put(f"错误：找不到名为 '{strategy_name}' 的策略。")
                                continue
                            final_params = self._prepare_strategy_params(acc_config, params, strategy_info)
                            strategy = strategy_info['class'](acc_config, log_queue, final_params)
                            strategy.start()
                            self.strategy_instances[account_id] = strategy
                            log_queue.put(f"账户 {account_id} 的策略 '{strategy_name}' 已成功启动。")
                            account_info_queue.put({'id': account_id, 'status': 'strategy_running'})
                        except Exception as e:
                            log_queue.put(f"启动策略失败 ({account_id}): {e}")
                    elif action == 'STOP_STRATEGY':
                        account_id = task.get('account_id')
                        if account_id in self.strategy_instances:
                            log_queue.put(f"正在停止账户 {account_id} 的策略...")
                            self._stop_strategy_sync(account_id)
                            log_queue.put(f"账户 {account_id} 的策略已停止。")
                            account_info_queue.put({'id': account_id, 'status': 'inactive'})
                        else:
                            log_queue.put(f"指令忽略：账户 {account_id} 未在运行策略。")
                except Empty:
                    pass

                self._process_logged_in_accounts()

                if self.default_vars.get('enable_global_equity_stop') and self.default_vars['enable_global_equity_stop'].get():
                    try:
                        stop_level = float(self.default_vars['global_equity_stop_level'].get())
                        total_equity = sum(e for e in self.equity_data.values() if e is not None)
                        active_accounts_count = sum(1 for e in self.equity_data.values() if e is not None)
                        if active_accounts_count > 0 and total_equity < stop_level:
                                log_queue.put(f"!!! 全局风控触发 !!! 所有账户总净值 {total_equity:,.2f} 低于阈值 {stop_level:,.2f}。")
                                log_queue.put("正在自动执行[一键清仓所有账户]...")
                                task_queue.put({'action': 'CLOSE_ALL_ACCOUNTS_FORCEFULLY'})
                                self.default_vars['enable_global_equity_stop'].set(False)
                                self.save_config()
                    except (ValueError, KeyError):
                        pass

            except Exception as e:
                log_queue.put(f"后台线程主循环发生严重错误: {e}")
            finally:
                time.sleep(float(self.app_config.get('DEFAULT', 'check_interval', fallback='0.2')))

    def _stop_strategy_sync(self, account_id):
        if account_id in self.strategy_instances:
            strategy = self.strategy_instances[account_id]
            strategy.stop_strategy()
            strategy.join(timeout=5)
            if account_id in self.strategy_instances:
                del self.strategy_instances[account_id]

    def _process_logged_in_accounts(self):
        MAX_CONN_FAILURES = 10

        # 只处理用户已登录的账户
        logged_in_accounts_copy = self.logged_in_accounts.copy()
        if not logged_in_accounts_copy:
            return

        # 1. 验证新配置
        for acc_id in list(self.pending_verification_config.keys()):
            if acc_id in logged_in_accounts_copy:
                temp_config = self.pending_verification_config[acc_id]
                log_queue.put(f"正在使用新配置验证账户 {acc_id}...")
                _, mt5_conn, _ = _connect_mt5(temp_config, log_queue, f"验证账户 {acc_id}")
                if mt5_conn:
                    log_queue.put(f"账户 {acc_id} 验证成功！新配置已应用。")
                    for key, value in temp_config.items():
                        self.app_config.set(acc_id, key, str(value))
                    self.verified_passwords.add(acc_id)
                    self.connection_failures[acc_id] = 0
                    mt5_conn.shutdown()
                else:
                    log_queue.put(f"账户 {acc_id} 验证失败。配置未更改。")
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
                    _get_account_details(mt5_master, master_id, ping)
                    account_info_queue.put({'id': master_id, 'status': 'connected'})
                    master_info = mt5_master.account_info()
                    if not master_info: continue
                    master_trades_dict = {t.ticket: t for t in list(mt5_master.positions_get() or []) + list(mt5_master.orders_get() or [])}
                    for slave_id, slave_config in slave_group:
                        slave_config['account_id'] = slave_id
                        if self.connection_failures.get(slave_id, 0) >= MAX_CONN_FAILURES:
                            account_info_queue.put({'id': slave_id, 'status': 'locked'})
                            continue
                        _copy_trades_for_slave(self, slave_id, slave_config, master_trades_dict, master_info, self.per_slave_mapping, log_queue)
                finally:
                    mt5_master.shutdown()

        # 3. 处理所有其他已登录的账户 (运行策略的，或仅登录监控的)
        for acc_id in logged_in_accounts_copy:
            is_strategy_running = acc_id in self.strategy_instances
            is_copying = any(acc_id == s_id for m_id in slaves_by_master for s_id, _ in slaves_by_master[m_id])

            if is_copying: continue # 跟单逻辑已处理

            if self.connection_failures.get(acc_id, 0) >= MAX_CONN_FAILURES:
                account_info_queue.put({'id': acc_id, 'status': 'locked'})
                continue

            config = self.app_config[acc_id]
            config['account_id'] = acc_id
            ping, mt5_conn, err_code = _connect_mt5(config, log_queue, f"账户 {acc_id}")

            if mt5_conn:
                self.connection_failures[acc_id] = 0
                _get_account_details(mt5_conn, acc_id, ping)
                mt5_conn.shutdown()
                if is_strategy_running:
                    if not self.strategy_instances[acc_id].is_alive():
                        log_queue.put(f"警告：账户 {acc_id} 的策略线程已意外终止。")
                        del self.strategy_instances[acc_id]
                        account_info_queue.put({'id': acc_id, 'status': 'error'})
                    else:
                        account_info_queue.put({'id': acc_id, 'status': 'strategy_running'})
                else:
                    account_info_queue.put({'id': acc_id, 'status': 'connected'})
            else:
                self.connection_failures[acc_id] = self.connection_failures.get(acc_id, 0) + 1
                if err_code == 1045:
                    self.connection_failures[acc_id] = MAX_CONN_FAILURES + 1
                    if is_strategy_running:
                        log_queue.put(f"!!! 严重错误: 账户 {acc_id} 授权失败，已停止该策略。")
                        task_queue.put({'action': 'STOP_STRATEGY', 'account_id': acc_id})

    def _close_all_trades_for_single_account(self, account_id, config):
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
            log_queue.put(f"  > [{account_id}] 清空指令已发送。")
        finally:
            mt5_conn.shutdown()

    def _close_single_trade_for_account(self, account_id, config, ticket):
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

    def _validate_lot_entry(self, P):
        if P == "" or P == ".": return True
        try:
            float(P)
            if '.' in P and len(P.split('.')[1]) > 2: return False
        except ValueError: return False
        return True

    def _on_lot_size_focus_out(self, var):
        try:
            val = float(var.get())
            if val < 0.01: val = 0.01
            var.set(f"{val:.2f}")
        except (ValueError, TypeError):
            var.set("0.01")

    def create_menu(self):
        self.menu_bar = tk.Menu(self)
        self.config(menu=self.menu_bar)
        file_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.menu_bar.add_cascade(label="文件(F)", menu=file_menu)
        file_menu.add_command(label="保存设置", command=self.save_config, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_closing)
        self.bind_all("<Control-s>", lambda event: self.save_config())

    def create_manual_tab(self, notebook):
        """创建使用说明标签页"""
        manual_tab = ttk.Frame(notebook, padding=15)
        notebook.add(manual_tab, text="使用说明")

        manual_tab.rowconfigure(0, weight=1)
        manual_tab.columnconfigure(0, weight=1)

        text_area = scrolledtext.ScrolledText(manual_tab, wrap=tk.WORD, font=("微软雅黑", 10), relief="flat", bg="#fdfdfd")
        text_area.grid(row=0, column=0, sticky="nsew")

        text_area.insert(tk.END, MANUAL_TEXT)
        text_area.config(state="disabled")

    def create_widgets(self):
        self.configure(bg="#f0f0f0")
        style = ttk.Style(self)
        style.configure('danger.TButton', foreground='#D32F2F', font=('微软雅黑', 9, 'bold'))
        style.configure("Bold.TLabel", font=("Segoe UI", 9, "bold"))
        main_paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        notebook = ttk.Notebook(main_paned)
        main_paned.add(notebook, weight=3)
        
        self.create_account_tab(notebook)
        self.create_settings_tab(notebook)
        self.create_strategy_tab(notebook)
        self.create_strategy_guide_tab(notebook) # 新增：创建策略开发说明标签页
        self.create_positions_tab(notebook)
        self.create_log_tab(notebook)
        self.create_manual_tab(notebook)
        
        status_frame = ttk.Frame(self)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 5))
        self.status_var = tk.StringVar(value="就绪 - 等待操作")
        ttk.Label(status_frame, textvariable=self.status_var, anchor='w').pack(fill=tk.X)

    def create_account_tab(self, notebook):
        account_tab = ttk.Frame(notebook)
        notebook.add(account_tab, text='账户概览与控制')
        account_tab.columnconfigure(0, weight=1)
        account_tab.rowconfigure(0, weight=1)
 
        top_container = ttk.PanedWindow(account_tab, orient=tk.HORIZONTAL)
        top_container.grid(row=0, column=0, sticky='nsew', pady=(0, 5))
 
        master_outer_frame = ttk.Frame(top_container)
        top_container.add(master_outer_frame, weight=4)
        master_outer_frame.columnconfigure(0, weight=1)
        master_outer_frame.rowconfigure(2, weight=1)

        self.master_notebook = ttk.Notebook(master_outer_frame)
        self.master_notebook.grid(row=0, column=0, sticky='ew')

        for i in range(1, NUM_MASTERS + 1):
            master_id = f'master{i}'
            master_tab = ttk.Frame(self.master_notebook, padding=5)
            self.master_notebook.add(master_tab, text=f"主账户 {i}")
            master_vars, master_frame = self.create_account_frame(master_tab, f"主账户 {i} 设置", self.app_config[master_id], master_id)
            master_frame.pack(fill="x", expand=False)
            self.master_vars_list.append(master_vars)

        slave_outer_frame = ttk.Frame(top_container)
        top_container.add(slave_outer_frame, weight=1)
        slave_outer_frame.columnconfigure(0, weight=1)
        slave_outer_frame.rowconfigure(3, weight=1)

        self.slave_notebook = ttk.Notebook(slave_outer_frame)
        self.slave_notebook.grid(row=0, column=0, sticky='new')
        self.slave_notebook.bind("<<NotebookTabChanged>>", self.on_slave_tab_changed)

        for i in range(1, NUM_SLAVES + 1):
            slave_id = f'slave{i}'
            slave_tab = ttk.Frame(self.slave_notebook, padding=5)
            self.slave_notebook.add(slave_tab, text=f"从账户 {i}")
            slave_vars, slave_frame = self.create_account_frame(slave_tab, f"从属账户 {i} 设置", self.app_config[slave_id], slave_id, is_slave=True)
            slave_frame.pack(fill="x", expand=False)
            self.slave_vars_list.append(slave_vars)

        slave_control_frame = ttk.Frame(slave_outer_frame, padding=(0, 5, 0, 0))
        slave_control_frame.grid(row=1, column=0, sticky='ew')
        slave_control_frame.columnconfigure(0, weight=1)

        self.save_button = ttk.Button(slave_control_frame, text="保存设置", command=self.save_config)
        self.save_button.grid(row=0, column=0, padx=(0, 5), ipady=4, sticky='ew')
 
        summary_frame_container = ttk.LabelFrame(master_outer_frame, text="所有账户净值")
        summary_frame_container.grid(row=1, column=0, sticky='ew', pady=5)
        self.create_summary_frame(summary_frame_container)

        master_spacer = ttk.Frame(master_outer_frame)
        master_spacer.grid(row=2, column=0, sticky='nsew')

        # Use a Text widget for rich text and hyperlinks
        style = ttk.Style()
        disclaimer_text_widget = tk.Text(
            slave_outer_frame,
            wrap=tk.WORD,
            font=("Segoe UI", 9),
            bg=style.lookup("TFrame", "background"), # Match window background
            relief="flat",
            bd=0,
            highlightthickness=0,
            height=3 # Set height to fit content
        )
        disclaimer_text_widget.grid(row=2, column=0, sticky='ew', pady=(10, 5), padx=5)

        # Define styles (tags)
        bold_font = tkFont.Font(family="Segoe UI", size=9, weight="bold")
        link_font = tkFont.Font(family="Segoe UI", size=9, weight="bold", underline=True)
        
        disclaimer_text_widget.tag_configure("bold", font=bold_font)
        disclaimer_text_widget.tag_configure("link", foreground="blue", font=link_font)

        # Insert text parts with their styles
        disclaimer_text_widget.insert(tk.END, "免责声明：本软件提供跟单和python自动化交易的功能，免费供个人学习测试使用，市场风险难料，软件功能不作担保，用户据此交易的风险及相关损失，均由用户自行承担。")
        disclaimer_text_widget.tag_add("bold", "1.0", "1.4")
        disclaimer_text_widget.tag_config("bold", font=("Arial", 10, "bold"))
        disclaimer_text_widget.insert(tk.END, "\n软件还有一个收费的web版，更多信息及返佣，请访问 → ", "bold""center")
        disclaimer_text_widget.insert(tk.END, "https://www.helei.info/", "link")

        # Bind events to the link tag
        disclaimer_text_widget.tag_bind("link", "<Enter>", lambda e: disclaimer_text_widget.config(cursor="hand2"))
        disclaimer_text_widget.tag_bind("link", "<Leave>", lambda e: disclaimer_text_widget.config(cursor=""))
        disclaimer_text_widget.tag_bind("link", "<Button-1>", lambda e: webbrowser.open_new("https://www.helei.info/"))

        # Disable editing
        disclaimer_text_widget.config(state="disabled")

        slave_spacer = ttk.Frame(slave_outer_frame)
        slave_spacer.grid(row=3, column=0, sticky='nsew')

    def create_account_frame(self, parent, title, config, account_id, is_slave=False):
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        v = {'account_id': account_id, 'positions_data': []}

        top_frame = ttk.Frame(frame)
        top_frame.pack(fill=tk.X, expand=True, pady=(0, 5))
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)

        v['widgets_to_disable'] = []
        login_frame = ttk.LabelFrame(top_frame, text="登录信息", padding=5)
        login_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        login_frame.columnconfigure(1, weight=1)

        fields = {"path": "路径", "login": "账号", "password": "密码", "server": "服务器"}
        if is_slave: fields["magic"] = "跟单魔术号"

        for i, (key, text) in enumerate(fields.items()):
            ttk.Label(login_frame, text=f"{text}:").grid(row=i, column=0, sticky="w", pady=1)
            value = config.get(key, '')
            if key == 'password': value = decrypt_password(value)
            v[key] = tk.StringVar(value=value)
            entry = ttk.Entry(login_frame, textvariable=v[key], show="*" if key == "password" else "")
            entry.grid(row=i, column=1, sticky="ew", padx=2, pady=1)
            v['widgets_to_disable'].append(entry)
            if key == "path":
                ttk.Button(login_frame, text="...", command=lambda var=v[key]: self.browse_path(var), width=3).grid(row=i, column=2)

        status_frame = ttk.LabelFrame(top_frame, text="账户状态", padding=5)
        status_frame.grid(row=0, column=1, sticky='nsew', padx=(5, 0))
        for i in range(2): status_frame.columnconfigure(i, weight=1)
        v['margin_free_var'] = tk.StringVar(value="可用: --")

        info_points = {'balance': '余额', 'equity': '净值', 'profit': '浮盈', 'margin_level': '比例', 'total_positions': '持仓', 'total_volume': '手数', 'credit': '赠金', 'swap': '隔夜利息'}
        for i, (name, text) in enumerate(info_points.items()):
            r, c = i // 2 + 1, (i % 2)
            v[f'{name}_var'] = tk.StringVar(value=f"{text}: --")
            style = "Bold.TLabel" if name == 'profit' else 'TLabel'
            label = ttk.Label(status_frame, textvariable=v[f'{name}_var'], style=style)
            label.grid(row=r, column=c, sticky='w', padx=2, pady=1)
            if name == 'profit': v['profit_widget'] = label

        v['status_var'] = tk.StringVar(value="状态: --")
        v['status_widget'] = ttk.Label(status_frame, textvariable=v['status_var'], style="Bold.TLabel")
        v['status_widget'].grid(row=0, column=0, columnspan=4, sticky='ew', pady=(0, 4))

        control_frame = ttk.LabelFrame(frame, text="交易控制", padding=5)
        control_frame.pack(fill=tk.X, expand=True, pady=(5,0))
        control_frame.columnconfigure(1, weight=1)

        strategy_frame = ttk.Frame(control_frame)
        strategy_frame.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0,5))
        strategy_frame.columnconfigure(1, weight=1)
        
        ttk.Label(strategy_frame, text="运行策略:").grid(row=0, column=0, sticky='w', padx=(0,5))
        
        v['login_btn'] = ttk.Button(strategy_frame, text="登录", command=lambda acc_id=account_id: self.toggle_login(acc_id))
        v['login_btn'].grid(row=0, column=2, padx=(5,0))

        v['strategy_var'] = tk.StringVar(value=config.get('strategy', ''))
        strategy_names = sorted(self.available_strategies.keys()) or ['(无可用策略)']
        v['strategy_selector'] = ttk.Combobox(strategy_frame, textvariable=v['strategy_var'], state="readonly", values=strategy_names)
        v['strategy_selector'].grid(row=0, column=1, sticky='ew')
        if v['strategy_var'].get() not in strategy_names: v['strategy_var'].set('')

        btn_frame = ttk.Frame(control_frame)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky='ew')
        
        v['strategy_start_btn'] = ttk.Button(btn_frame, text="启动策略", command=lambda acc_id=account_id: self.start_strategy_for_account(acc_id))
        v['strategy_start_btn'].pack(side='left', expand=True, fill='x', padx=2)
        v['strategy_stop_btn'] = ttk.Button(btn_frame, text="停止策略", command=lambda acc_id=account_id: self.stop_strategy_for_account(acc_id), state='disabled')
        v['strategy_stop_btn'].pack(side='left', expand=True, fill='x', padx=2)
        v['strategy_config_btn'] = ttk.Button(btn_frame, text="配置", command=lambda acc_id=account_id: self.configure_strategy_for_account(acc_id))
        v['strategy_config_btn'].pack(side='left', expand=True, fill='x', padx=2)
        v['close_all_btn'] = ttk.Button(btn_frame, text="一键清仓", style='danger.TButton', command=lambda acc_id=account_id: self.close_all_for_account(acc_id))
        v['close_all_btn'].pack(side='left', expand=True, fill='x', padx=2)

        if is_slave:
            ttk.Separator(control_frame).grid(row=2, column=0, columnspan=2, sticky='ew', pady=8)
            
            follow_master_frame = ttk.Frame(control_frame)
            follow_master_frame.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(0,5))
            
            ttk.Label(follow_master_frame, text="跟随主账户:").pack(side=tk.LEFT, padx=(0, 5))
            
            saved_master_id_en = config.get('follow_master_id', 'master1')
            saved_master_id_cn = self.master_id_map_reverse.get(saved_master_id_en, "主账户 1")
            v['follow_master_id'] = tk.StringVar(value=saved_master_id_en)
            v['follow_master_id_cn'] = tk.StringVar(value=saved_master_id_cn)
            master_display_names = sorted(list(self.master_id_map.keys()))
            
            follow_combo = ttk.Combobox(follow_master_frame, textvariable=v['follow_master_id_cn'], state='readonly', values=master_display_names, width=12)
            follow_combo.pack(side=tk.LEFT, padx=5); v['widgets_to_disable'].append(follow_combo)

            v['enabled'] = tk.BooleanVar(value=config.getboolean('enabled', fallback=False))
            v['start_follow_btn'] = ttk.Button(follow_master_frame, text="启动跟随", command=lambda acc_id=account_id, enable=True: self.toggle_slave_enabled(acc_id, enable))
            v['start_follow_btn'].pack(side="left", padx=(5,5))
            v['stop_follow_btn'] = ttk.Button(follow_master_frame, text="停止跟随", command=lambda acc_id=account_id, enable=False: self.toggle_slave_enabled(acc_id, enable))
            v['stop_follow_btn'].pack(side="left")
            self.update_follow_button_state(v)

            lot_frame = ttk.LabelFrame(control_frame, text="手数管理", padding=5)
            lot_frame.grid(row=4, column=0, columnspan=2, sticky='ew', pady=(5,0))
            lot_frame.columnconfigure(3, weight=1)

            saved_mode_en = config.get('volume_mode', 'same_as_master')
            saved_mode_cn = self.volume_mode_map_reverse.get(saved_mode_en, "与主账户相同")
            v['volume_mode'] = tk.StringVar(value=saved_mode_en)
            v['volume_mode_cn'] = tk.StringVar(value=saved_mode_cn)
            v['fixed_lot_size'] = tk.StringVar(value=config.get('fixed_lot_size', '0.01'))

            ttk.Label(lot_frame, text="模式:").grid(row=0, column=0, sticky='w', padx=(0,5))
            volume_mode_selector = ttk.Combobox(lot_frame, textvariable=v['volume_mode_cn'], state='readonly', values=list(self.volume_mode_map.keys()), width=12); 
            v['widgets_to_disable'].append(volume_mode_selector)
            volume_mode_selector.grid(row=0, column=1, sticky='w')

            fixed_lot_label = ttk.Label(lot_frame, text="固定手数:")
            fixed_lot_label.grid(row=0, column=2, sticky='w', padx=(10,5))
            v['widgets_to_disable'].append(fixed_lot_label)
            
            fixed_lot_entry = ttk.Entry(lot_frame, textvariable=v['fixed_lot_size'], width=10, validate='key', validatecommand=self.vcmd_lot)
            fixed_lot_entry.bind('<FocusOut>', lambda e, var=v['fixed_lot_size']: self._on_lot_size_focus_out(var))
            fixed_lot_entry.grid(row=0, column=3, sticky='w')

            def toggle_lot_entry(event=None):
                is_fixed = v['volume_mode_cn'].get() == '固定手数'
                state = 'normal' if is_fixed else 'disabled'
                fixed_lot_entry.config(state=state)
                fixed_lot_label.config(state=state)
                if is_fixed: self._on_lot_size_focus_out(v['fixed_lot_size'])

            volume_mode_selector.bind("<<ComboboxSelected>>", toggle_lot_entry)
            toggle_lot_entry()

            mode_rule_frame = ttk.LabelFrame(control_frame, text="模式与规则", padding=2)
            mode_rule_frame.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(5,0))
            
            v['copy_mode'] = tk.StringVar(value=config.get('copy_mode', 'forward'))
            copy_mode_frame = ttk.Frame(mode_rule_frame)
            copy_mode_frame.pack(side="left", padx=2, pady=2)
            l = ttk.Label(copy_mode_frame, text="跟单模式:"); l.pack(side="left", padx=(0,5)); v['widgets_to_disable'].append(l)
            rb1 = ttk.Radiobutton(copy_mode_frame, text="正向", variable=v['copy_mode'], value="forward"); rb1.pack(side="left"); v['widgets_to_disable'].append(rb1)
            rb2 = ttk.Radiobutton(copy_mode_frame, text="反向", variable=v['copy_mode'], value="reverse"); rb2.pack(side="left", padx=(0,5)); v['widgets_to_disable'].append(rb2)

            ttk.Separator(mode_rule_frame, orient=tk.VERTICAL).pack(side="left", fill='y', padx=10, pady=5)

            symbol_rule_frame = ttk.Frame(mode_rule_frame)
            symbol_rule_frame.pack(side="left", fill='x', expand=True, padx=2, pady=2)
            
            v['default_symbol_rule'] = tk.StringVar(value=config.get('default_symbol_rule', 'none'))
            v['default_symbol_text'] = tk.StringVar(value=config.get('default_symbol_text', ''))
            entry = ttk.Entry(symbol_rule_frame, textvariable=v['default_symbol_text'], state='disabled', width=8)
            v['widgets_to_disable'].append(entry)
            
            def create_toggle_closure(var, widget):
                def toggle(): widget.config(state='normal' if var.get() != 'none' else 'disabled')
                return toggle
            
            toggle_func = create_toggle_closure(v['default_symbol_rule'], entry)
            l2 = ttk.Label(symbol_rule_frame, text="品种规则:"); l2.pack(side="left", padx=(5,5)); v['widgets_to_disable'].append(l2)
            rb3 = ttk.Radiobutton(symbol_rule_frame, text="无", variable=v['default_symbol_rule'], value="none", command=toggle_func); rb3.pack(side='left'); v['widgets_to_disable'].append(rb3)
            rb4 = ttk.Radiobutton(symbol_rule_frame, text="后缀", variable=v['default_symbol_rule'], value="suffix", command=toggle_func); rb4.pack(side='left', padx=(5,0)); v['widgets_to_disable'].append(rb4)
            rb5 = ttk.Radiobutton(symbol_rule_frame, text="前缀", variable=v['default_symbol_rule'], value="prefix", command=toggle_func); rb5.pack(side='left', padx=(5,0)); v['widgets_to_disable'].append(rb5)
            entry.pack(side='left', fill='x', expand=True, padx=(2, 5))
            toggle_func()
        
        self.account_widgets_to_disable[account_id] = v['widgets_to_disable']

        return v, frame

    def create_summary_frame(self, parent):
        summary_grid = ttk.Frame(parent, padding=5)
        summary_grid.pack(fill=tk.X, expand=True)
        columns = ['账户', '余额', '净值', '浮动盈亏', '可用预付款', '预付款维持率']
        column_weights = [1, 2, 2, 2, 2, 2]
        columns = ['账户', '余额', '净值', '浮动盈亏', '可用预付款', '预付款维持率', '持仓数']
        column_weights = [1, 2, 2, 2, 2, 2, 1]

        for i, text in enumerate(columns):
            summary_grid.columnconfigure(i, weight=column_weights[i])
            ttk.Label(summary_grid, text=text, font=("Segoe UI", 9, "bold")).grid(row=0, column=i, sticky='w', padx=5)

        ttk.Separator(summary_grid).grid(row=1, column=0, columnspan=len(columns), sticky='ew', pady=4)

        all_account_vars = self.master_vars_list + self.slave_vars_list
        for i, acc_vars in enumerate(all_account_vars):
            row = i + 2
            acc_name = f"主账户 {i+1}" if i < NUM_MASTERS else f"从账户 {i - NUM_MASTERS + 1}"
            acc_vars['summary_balance_var'] = tk.StringVar(value="--")
            acc_vars['summary_equity_var'] = tk.StringVar(value="--")
            acc_vars['summary_margin_free_var'] = tk.StringVar(value="--")
            acc_vars['summary_margin_level_var'] = tk.StringVar(value="--")
            acc_vars['summary_profit_var'] = tk.StringVar(value="--")
            acc_vars['summary_total_positions_var'] = tk.StringVar(value="--")

            ttk.Label(summary_grid, text=acc_name).grid(row=row, column=0, sticky='w', padx=5)
            ttk.Label(summary_grid, textvariable=acc_vars['summary_balance_var']).grid(row=row, column=1, sticky='w', padx=5)
            ttk.Label(summary_grid, textvariable=acc_vars['summary_equity_var']).grid(row=row, column=2, sticky='w', padx=5)
            
            profit_label = ttk.Label(summary_grid, textvariable=acc_vars['summary_profit_var'])
            profit_label.grid(row=row, column=3, sticky='w', padx=5)
            acc_vars['summary_profit_widget'] = profit_label

            ttk.Label(summary_grid, textvariable=acc_vars['summary_margin_free_var']).grid(row=row, column=4, sticky='w', padx=5)
            ttk.Label(summary_grid, textvariable=acc_vars['summary_margin_level_var']).grid(row=row, column=5, sticky='w', padx=5)
            ttk.Label(summary_grid, textvariable=acc_vars['summary_total_positions_var']).grid(row=row, column=6, sticky='w', padx=5)

        summary_row = len(all_account_vars) + 2
        ttk.Separator(summary_grid).grid(row=summary_row, column=0, columnspan=len(columns), sticky='ew', pady=4)
        summary_row += 1
        ttk.Label(summary_grid, text="主账户总计", font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=0, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['master_total_balance'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=1, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['master_total_equity'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=2, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['master_total_profit'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=3, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['master_total_margin_free'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=4, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['master_total_positions'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=6, sticky='w', padx=5, pady=(5,0))

        summary_row += 1
        ttk.Label(summary_grid, text="从账户总计", font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=0, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['slave_total_balance'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=1, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['slave_total_equity'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=2, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['slave_total_profit'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=3, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['slave_total_margin_free'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=4, sticky='w', padx=5, pady=(5,0))
        ttk.Label(summary_grid, textvariable=self.summary_vars['slave_total_positions'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=6, sticky='w', padx=5, pady=(5,0))

        summary_row += 1
        ttk.Separator(summary_grid).grid(row=summary_row, column=0, columnspan=len(columns), sticky='ew', pady=4)
        
        summary_row += 1
        ttk.Label(summary_grid, text="所有账户总计", font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=0, sticky='w', padx=5)
        ttk.Label(summary_grid, textvariable=self.summary_vars['total_balance'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=1, sticky='w', padx=5)
        ttk.Label(summary_grid, textvariable=self.summary_vars['total_equity'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=2, sticky='w', padx=5)
        ttk.Label(summary_grid, textvariable=self.summary_vars['total_profit'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=3, sticky='w', padx=5)
        ttk.Label(summary_grid, textvariable=self.summary_vars['total_margin_free'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=4, sticky='w', padx=5)
        ttk.Label(summary_grid, textvariable=self.summary_vars['total_positions'], font=("Segoe UI", 9, "bold")).grid(row=summary_row, column=6, sticky='w', padx=5)

    def create_positions_tree(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        columns = {'ticket': ('订单号', 80), 'symbol': ('品种', 80), 'type': ('类型', 70), 'volume': ('手数', 60), 'price_open': ('价格', 80), 'sl': ('止损', 80), 'tp': ('止盈', 80), 'profit': ('浮盈/状态', 90), 'close': ('操作', 40)}
        tree = ttk.Treeview(parent, columns=tuple(columns.keys()), show='headings', height=5)
        tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=scrollbar.set)
        for col, (text, width) in columns.items():
            tree.heading(col, text=text, anchor='center')
            tree.column(col, width=width, anchor='center')
        tree.tag_configure('pending', foreground='blue')
        tree.tag_configure('profit', background='#E8F5E9', foreground='#1B5E20')
        tree.tag_configure('loss', background='#FFEBEE', foreground='#B71C1C')
        tree.bind("<Button-1>", self.on_treeview_click)
        return tree

    def create_positions_tab(self, notebook):
        positions_tab = ttk.Frame(notebook, padding=10)
        notebook.add(positions_tab, text='所有账户持仓')
        positions_tab.columnconfigure(0, weight=1)
        positions_tab.columnconfigure(1, weight=1)
        positions_tab.rowconfigure(0, weight=1)

        master_positions_frame = ttk.LabelFrame(positions_tab, text="主账户当前持仓")
        master_positions_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        master_positions_frame.rowconfigure(1, weight=1)
        master_positions_frame.columnconfigure(0, weight=1)

        master_selector_frame = ttk.Frame(master_positions_frame)
        master_selector_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5), padx=5)
        ttk.Label(master_selector_frame, text="选择账户:").pack(side=tk.LEFT)
        self.positions_tab_master_selector = ttk.Combobox(master_selector_frame, textvariable=self.positions_tab_master_selector_var, state="readonly", values=[f"主账户 {i}" for i in range(1, NUM_MASTERS + 1)])
        self.positions_tab_master_selector.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.positions_tab_master_selector.bind("<<ComboboxSelected>>", self.on_positions_tab_master_selected)
        self.positions_tab_master_selector.current(0)
        master_tree_container = ttk.Frame(master_positions_frame)
        master_tree_container.grid(row=1, column=0, sticky='nsew')
        self.master_positions_tree = self.create_positions_tree(master_tree_container)
 
        slave_positions_frame = ttk.LabelFrame(positions_tab, text="从账户当前持仓")
        slave_positions_frame.grid(row=0, column=1, sticky='nsew', padx=(5, 0))
        slave_positions_frame.rowconfigure(1, weight=1)
        slave_positions_frame.columnconfigure(0, weight=1)

        selector_frame = ttk.Frame(slave_positions_frame)
        selector_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5), padx=5)
        ttk.Label(selector_frame, text="选择账户:").pack(side=tk.LEFT)
        self.positions_tab_slave_selector = ttk.Combobox(selector_frame, textvariable=self.positions_tab_slave_selector_var, state="readonly", values=[f"从账户 {i}" for i in range(1, NUM_SLAVES + 1)])
        self.positions_tab_slave_selector.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.positions_tab_slave_selector.bind("<<ComboboxSelected>>", self.on_positions_tab_slave_selected)
        self.positions_tab_slave_selector.current(0)
        tree_container = ttk.Frame(slave_positions_frame)
        tree_container.grid(row=1, column=0, sticky='nsew')
        tree_container.rowconfigure(0, weight=1); tree_container.columnconfigure(0, weight=1)
        self.slave_positions_tree = self.create_positions_tree(tree_container)
        self.on_positions_tab_master_selected()

    def create_settings_tab(self, notebook):
        settings_tab = ttk.Frame(notebook, padding=10)
        notebook.add(settings_tab, text="全局 & 精确映射")
        self.create_general_settings_frame(settings_tab).pack(fill=tk.X, pady=(0, 10))
        self.create_symbol_mapping_frame(settings_tab).pack(fill=tk.BOTH, expand=True)

    def create_general_settings_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="全局检查", padding=10)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="检查间隔(秒):").grid(row=0, column=0, sticky="w", pady=2)
        self.default_vars['check_interval'] = tk.StringVar(value=self.app_config.get('DEFAULT', 'check_interval', fallback='0.2'))
        ttk.Entry(frame, textvariable=self.default_vars['check_interval']).grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        # 全局风控UI已根据要求移除
        self.default_vars['enable_global_equity_stop'] = tk.BooleanVar(value=False)
        self.default_vars['global_equity_stop_level'] = tk.StringVar(value='0.0')

        return frame

    def create_log_tab(self, notebook):
        log_tab = ttk.Frame(notebook, padding=10)
        notebook.add(log_tab, text="操作日志")
        log_tab.rowconfigure(0, weight=1); log_tab.columnconfigure(0, weight=1)
        log_frame = ttk.LabelFrame(log_tab, text="实时日志记录", padding=5)
        log_frame.pack(fill='both', expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state="disabled", font=("微软雅黑", 10), height=8)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def create_symbol_mapping_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="精确覆盖/例外规则 (优先级最高，注意大小写)", padding=10)
        frame.columnconfigure(0, weight=1); frame.rowconfigure(3, weight=1)
        selector_frame = ttk.Frame(frame)
        selector_frame.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        ttk.Label(selector_frame, text="配置目标账户:").pack(side="left")
        self.mapping_slave_selector = ttk.Combobox(selector_frame, state="readonly", values=[f"从属账户 {i}" for i in range(1, NUM_SLAVES + 1)])
        self.mapping_slave_selector.pack(side="left", padx=5)
        self.mapping_slave_selector.bind("<<ComboboxSelected>>", self.on_slave_selected_for_mapping); self.mapping_slave_selector.current(0)
        input_frame = ttk.Frame(frame)
        input_frame.grid(row=1, column=0, sticky='ew', pady=(0, 5))
        for i in [1, 3]: input_frame.columnconfigure(i, weight=1)
        ttk.Label(input_frame, text="主账户品种:").grid(row=0, column=0, padx=(0,5))
        self.master_symbol_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=self.master_symbol_var).grid(row=0, column=1, sticky='ew')
        ttk.Label(input_frame, text="从属账户品种:").grid(row=0, column=2, padx=(5,5))
        self.slave_symbol_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=self.slave_symbol_var).grid(row=0, column=3, sticky='ew')
        rule_frame = ttk.Frame(frame)
        rule_frame.grid(row=2, column=0, sticky='w', pady=5)
        self.mapping_rule_var = tk.StringVar(value="replace")
        ttk.Label(rule_frame, text="规则: 精确替换").pack(side="left", padx=5)
        ttk.Button(rule_frame, text="添加/更新", command=self.add_symbol_mapping).pack(side="left", padx=(20, 5))
        ttk.Button(rule_frame, text="删除选中", command=self.delete_symbol_mapping).pack(side="left")
        tree_frame = ttk.Frame(frame); tree_frame.grid(row=3, column=0, sticky='nsew')
        self.mapping_tree = ttk.Treeview(tree_frame, columns=('master', 'slave', 'rule'), show='headings', height=4)
        for col, text in [('master', '主账户品种'), ('slave', '最终品种'), ('rule', '映射规则')]: self.mapping_tree.heading(col, text=text)
        self.mapping_tree.column('rule', width=100, anchor='center')
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.mapping_tree.yview)
        self.mapping_tree.configure(yscrollcommand=scrollbar.set)
        self.mapping_tree.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")
        self.on_slave_selected_for_mapping()
        return frame
    
    def create_strategy_tab(self, notebook):
        strategy_tab = ttk.Frame(notebook, padding=10)
        notebook.add(strategy_tab, text="交易策略库")
        main_pane = ttk.PanedWindow(strategy_tab, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True)
        
        left_frame = ttk.LabelFrame(main_pane, text="策略库", padding=10)
        main_pane.add(left_frame, weight=0)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)
        
        list_container = ttk.Frame(left_frame)
        list_container.grid(row=0, column=0, sticky='nsew')
        list_container.rowconfigure(0, weight=1)
        list_container.columnconfigure(0, weight=1)

        self.strategy_listbox = tk.Listbox(list_container, font=("Segoe UI", 10))
        self.strategy_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.strategy_listbox.yview)
        list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.strategy_listbox.config(yscrollcommand=list_scrollbar.set)
        self.strategy_listbox.bind("<<ListboxSelect>>", self.on_strategy_selected_in_library)
        
        mng_btn_frame = ttk.Frame(left_frame)
        mng_btn_frame.grid(row=1, column=0, sticky='ew', pady=(10,0))
        ttk.Button(mng_btn_frame, text="导入", command=self.import_strategy).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(mng_btn_frame, text="删除", command=self.delete_strategy).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(mng_btn_frame, text="刷新", command=lambda: (self.discover_strategies(), self.refresh_all_strategy_uis())).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2) # 移除开发说明按钮
        
        right_frame = ttk.LabelFrame(main_pane, text="策略参数（全局默认）", padding=10)
        main_pane.add(right_frame, weight=1)
        right_frame.pack_propagate(False)

        details_pane = ttk.PanedWindow(right_frame, orient=tk.HORIZONTAL)
        details_pane.pack(fill=tk.BOTH, expand=True)

        self.strategy_desc_frame = ttk.LabelFrame(details_pane, text="策略说明", padding=5)
        details_pane.add(self.strategy_desc_frame, weight=3)
        
        # 使用ScrolledText控件来显示策略说明，以获得更好的自动换行和内边距效果
        self.strategy_desc_text = scrolledtext.ScrolledText(
            self.strategy_desc_frame, 
            wrap=tk.WORD, 
            font=("Segoe UI", 10), 
            relief="flat", 
            padx=5, 
            pady=5
        )
        self.strategy_desc_text.pack(fill=tk.BOTH, expand=True)
        self.strategy_desc_text.insert(tk.END, "请从左侧列表中选择一个策略。")
        self.strategy_desc_text.config(state="disabled")

        params_container = ttk.LabelFrame(details_pane, text="参数调整", padding=10)
        details_pane.add(params_container, weight=2)
        self.strategy_params_scrolled_frame = ScrolledFrame(params_container)
        self.strategy_params_scrolled_frame.pack(fill=tk.BOTH, expand=True)
        self.strategy_params_frame = self.strategy_params_scrolled_frame.interior
        self.strategy_params_frame.columnconfigure(1, weight=1)

    def create_strategy_guide_tab(self, notebook):
        """创建策略开发说明标签页"""
        guide_tab = ttk.Frame(notebook, padding=10)
        notebook.add(guide_tab, text="策略开发说明")

        guide_tab.rowconfigure(0, weight=1)
        guide_tab.columnconfigure(0, weight=1)

        text_area = scrolledtext.ScrolledText(guide_tab, wrap=tk.WORD, font=("微软雅黑", 10))
        text_area.grid(row=0, column=0, sticky="nsew")

        text_area.insert(tk.END, GUIDE_TEXT)
        text_area.config(state="disabled")

    def discover_strategies(self):
        self.available_strategies = {}
        if not os.path.isdir(STRATEGIES_DIR): os.makedirs(STRATEGIES_DIR)
        if STRATEGIES_DIR not in sys.path: sys.path.insert(0, STRATEGIES_DIR)

        for filename in os.listdir(STRATEGIES_DIR):
            if filename.endswith('.py') and not filename.startswith('_'):
                module_name = filename[:-3]
                filepath = os.path.abspath(os.path.join(STRATEGIES_DIR, filename))
                try:
                    # 统一处理模块加载和 BaseStrategy 注入
                    if module_name in sys.modules:
                        # 重新加载前确保 BaseStrategy 存在
                        sys.modules[module_name].BaseStrategy = BaseStrategy
                        module = importlib.reload(sys.modules[module_name])
                    else:
                        spec = importlib.util.spec_from_file_location(module_name, filepath)
                        module = importlib.util.module_from_spec(spec)
                        # 在执行模块代码前注入 BaseStrategy
                        module.BaseStrategy = BaseStrategy
                        sys.modules[module_name] = module
                        spec.loader.exec_module(module)

                    # 注入后，在模块中查找策略类
                    for item in dir(module):
                        obj = getattr(module, item)
                        if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                            strategy_name = getattr(obj, 'strategy_name', module_name)
                            self.available_strategies[strategy_name] = {
                                'class': obj, 'params_config': getattr(obj, 'strategy_params_config', {}).copy(),
                                'description': getattr(obj, 'strategy_description', '作者很懒，没有留下任何说明...').strip(),
                                'path': filepath,
                            }
                            log_queue.put(f"  -> 成功发现策略类: {strategy_name}")
                            break
                except Exception as e:
                    log_queue.put(f"[错误] 加载策略文件 '{filename}' 失败: {e}")

        if hasattr(self, 'strategy_listbox'):
            self.refresh_strategy_library_list()
            self.refresh_all_strategy_uis()
            selection = self.strategy_listbox.curselection()
            if selection: self.on_strategy_selected_in_library()
        log_queue.put("策略库扫描和加载完成。")

    def refresh_strategy_library_list(self):
        self.strategy_listbox.delete(0, tk.END)
        for name in sorted(self.available_strategies.keys()):
            self.strategy_listbox.insert(tk.END, name)

    def refresh_all_strategy_uis(self):
        strategy_names = sorted(self.available_strategies.keys()) or ['(无可用策略)']
        for v in self.master_vars_list + self.slave_vars_list:
            if 'strategy_selector' in v:
                current_val = v['strategy_var'].get()
                v['strategy_selector']['values'] = strategy_names
                if current_val not in strategy_names: v['strategy_var'].set('')

    def import_strategy(self):
        filepath = filedialog.askopenfilename(title="选择策略文件", filetypes=[("Python files", "*.py")], initialdir=os.path.abspath("."))
        if not filepath: return
        filename = os.path.basename(filepath)
        dest_path = os.path.join(STRATEGIES_DIR, filename)
        if os.path.exists(dest_path) and not messagebox.askyesno("确认覆盖", f"策略 '{filename}' 已存在，是否覆盖？"):
            return
        try:
            shutil.copy(filepath, dest_path)
            log_queue.put(f"策略 '{filename}' 已成功导入。")
            self.discover_strategies()
            self.refresh_all_strategy_uis()
        except Exception as e:
            messagebox.showerror("导入失败", f"无法复制文件: {e}")

    def delete_strategy(self):
        selection = self.strategy_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先在列表中选择一个要删除的策略。")
            return
        strategy_name = self.strategy_listbox.get(selection[0])
        if not messagebox.askyesno("确认删除", f"确定要删除策略 '{strategy_name}' 吗？\n这将从磁盘上移除对应的文件。"):
            return
        strategy_info = self.available_strategies.get(strategy_name)
        if strategy_info and os.path.exists(strategy_info['path']):
            try:
                os.remove(strategy_info['path'])
                log_queue.put(f"策略 '{strategy_name}' 已被删除。")
                self.discover_strategies()
                self.refresh_all_strategy_uis()
            except Exception as e:
                messagebox.showerror("删除失败", f"无法删除文件: {e}")

    def on_strategy_selected_in_library(self, event=None):
        selection = self.strategy_listbox.curselection()
        if not selection: return
        self.selected_strategy_in_library = self.strategy_listbox.get(selection[0])
        strategy_name = self.strategy_listbox.get(selection[0])
        strategy_info = self.available_strategies.get(strategy_name)
        if not strategy_info: return

        for widget in self.strategy_params_frame.winfo_children(): widget.destroy()
        self.global_strategy_param_vars.clear()

        self.strategy_desc_text.config(state="normal")
        self.strategy_desc_text.delete("1.0", tk.END)
        self.strategy_desc_text.insert("1.0", strategy_info.get('description', '作者很懒，没有留下任何说明...'))
        self.strategy_desc_text.config(state="disabled")
        params_config = strategy_info.get('params_config', {})
        if not params_config:
            ttk.Label(self.strategy_params_frame, text="该策略无全局可配置参数。").pack(pady=20, padx=10)
            return

        config_section_name = f"{strategy_name}_Global"
        if not self.app_config.has_section(config_section_name):
            self.app_config.add_section(config_section_name)

        for i, (param_key, config) in enumerate(params_config.items()):
            label_text = config.get('label', param_key)
            default_val = config.get('default', '')
            saved_val = self.app_config.get(config_section_name, param_key, fallback=str(default_val))
            ttk.Label(self.strategy_params_frame, text=f"{label_text}:").grid(row=i, column=0, sticky='w', padx=5, pady=5)
            var = tk.StringVar(value=saved_val)
            ttk.Entry(self.strategy_params_frame, textvariable=var).grid(row=i, column=1, sticky='ew', padx=5, pady=5)
            self.global_strategy_param_vars[param_key] = var
        
        ttk.Button(self.strategy_params_frame, text="保存全局默认值", command=self.save_global_strategy_params).grid(row=len(params_config), column=0, columnspan=2, sticky='ew', pady=(15,0), padx=5)

    def save_global_strategy_params(self):
        if not self.selected_strategy_in_library:
            messagebox.showwarning("提示", "请先从列表中选择一个策略进行配置。")
            return
        strategy_name = self.selected_strategy_in_library
        config_section_name = f"{strategy_name}_Global"
        for key, var in self.global_strategy_param_vars.items():
            self.app_config.set(config_section_name, key, var.get())
        self.write_config_to_file()
        log_queue.put(f"已保存策略 '{strategy_name}' 的全局默认参数。")

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

    def on_slave_selected_for_mapping(self, event=None):
        idx = self.mapping_slave_selector.current()
        if idx < 0: return
        self.refresh_mapping_tree(self.per_slave_mapping.get(f'slave{idx + 1}', {}))

    def refresh_mapping_tree(self, mapping_dict):
        self.mapping_tree.delete(*self.mapping_tree.get_children())
        for master, (rule, text) in sorted(mapping_dict.items()):
            rule_text = {"replace": "精确替换", "suffix": "添加后缀", "prefix": "添加前缀"}.get(rule, "未知")
            final_symbol = text if rule == 'replace' else (master + text if rule == 'suffix' else text + master)
            self.mapping_tree.insert('', 'end', values=(master, final_symbol, rule_text))

    def add_symbol_mapping(self):
        idx = self.mapping_slave_selector.current()
        if idx < 0: return
        slave_id = f'slave{idx + 1}'
        master, text = self.master_symbol_var.get().strip(), self.slave_symbol_var.get().strip()
        if not master or not text: messagebox.showwarning("输入错误", "主账户和从属账户品种均不能为空。"); return
        self.per_slave_mapping.setdefault(slave_id, {})[master] = (self.mapping_rule_var.get(), text)
        self.refresh_mapping_tree(self.per_slave_mapping[slave_id])
        self.master_symbol_var.set(''); self.slave_symbol_var.set('')
        self.log_message(f"已为 {slave_id} 添加映射: {master} -> {text}")

    def delete_symbol_mapping(self):
        idx, sel_item = self.mapping_slave_selector.current(), self.mapping_tree.selection()
        if idx < 0 or not sel_item: messagebox.showwarning("操作提示", "请先选择要删除的规则。"); return
        slave_id = f'slave{idx + 1}'
        master_symbol = self.mapping_tree.item(sel_item[0])['values'][0]
        if slave_id in self.per_slave_mapping and master_symbol in self.per_slave_mapping[slave_id]:
            del self.per_slave_mapping[slave_id][master_symbol]
            self.refresh_mapping_tree(self.per_slave_mapping[slave_id])
            self.log_message(f"已为 {slave_id} 删除 {master_symbol} 的映射")

    def load_config(self):
        self.app_config.read(CONFIG_FILE, encoding='utf-8')
        self.last_known_good_config.read_dict(self.app_config)
        
        for i in range(1, NUM_MASTERS + 1):
            sec_id = f'master{i}'
            if not self.app_config.has_section(sec_id): self.app_config.add_section(sec_id)
            if not self.last_known_good_config.has_section(sec_id): self.last_known_good_config.add_section(sec_id)
        
        for i in range(1, NUM_SLAVES + 1):
            sec_id = f'slave{i}'
            if not self.app_config.has_section(sec_id): self.app_config.add_section(sec_id)
            if not self.last_known_good_config.has_section(sec_id): self.last_known_good_config.add_section(sec_id)
            map_str = self.app_config.get(sec_id, 'symbol_map', fallback='')
            if map_str: self.per_slave_mapping[sec_id] = {k.strip(): (v.split(':', 1) if ':' in v else ('replace', v.strip())) for p in map_str.split(',') if '->' in p for k, v in [p.split('->', 1)]}
        
        for section in self.app_config.sections():
            if self.app_config.has_option(section, 'password'):
                encrypted_pass = self.app_config.get(section, 'password')
                self.app_config.set(section, 'password', decrypt_password(encrypted_pass))
                if 'master' in section or 'slave' in section:
                    self.verified_passwords.add(section)

    def save_final_config(self):
        """在程序关闭时调用，加密并保存所有密码。"""
        try:
            final_config = configparser.ConfigParser()
            final_config.read_dict(self.app_config)

            for section in final_config.sections():
                if final_config.has_option(section, 'password'):
                    plain_password = final_config.get(section, 'password')
                    if section in self.verified_passwords:
                        final_config.set(section, 'password', encrypt_password(plain_password))
                    else:
                        fallback_pass = self.last_known_good_config.get(section, 'password', fallback='')
                        final_config.set(section, 'password', fallback_pass)
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                final_config.write(f)
            log_queue.put("最终配置已加密并保存。")
        except Exception as e:
            log_queue.put(f"保存最终配置时出错: {e}")

    def on_closing(self):
        is_any_running = any(v.get('strategy_stop_btn')['state'] == 'normal' for v in (self.master_vars_list + self.slave_vars_list) if v) or \
                         any(v.get('stop_follow_btn')['state'] == 'normal' for v in self.slave_vars_list if v)
        if is_any_running and not messagebox.askyesno("确认关闭", "有策略或跟单正在运行，确定要关闭程序吗？"):
            return
        
        log_queue.put("正在关闭程序...")
        self.save_final_config()
        stop_event.set()
        self.destroy()

    def save_config(self):
        try:
            settings_to_save = {
                'master': ['path', 'login', 'password', 'server', 'strategy'],
                'slave': ['enabled', 'follow_master_id', 'path', 'login', 'password', 'server', 'magic', 'copy_mode', 
                          'default_symbol_rule', 'default_symbol_text', 'volume_mode', 'fixed_lot_size'],
            }
            for i, mvars in enumerate(self.master_vars_list):
                master_id = f'master{i+1}'
                for key in settings_to_save['master']:
                    if key in mvars and hasattr(mvars[key], 'get'):
                        value_to_save = str(mvars[key].get())
                        if key == 'password':
                            if self.app_config.get(master_id, 'password', fallback='') != value_to_save:
                                self.pending_verification_config[master_id] = {k:v for k,v in self.app_config.items(master_id)}
                                self.pending_verification_config[master_id]['password'] = value_to_save
                                self.verified_passwords.discard(master_id)
                                log_queue.put(f"检测到账户 {master_id} 密码已更改，将进行后台验证。")
                        self.app_config.set(master_id, key, value_to_save)
            
            for i, svars in enumerate(self.slave_vars_list):
                slave_id = f'slave{i+1}'
                for key in [k for k in settings_to_save['slave'] + ['strategy'] if k in svars]:
                    if key == 'volume_mode':
                        mode_cn = svars.get('volume_mode_cn').get()
                        mode_en = self.volume_mode_map.get(mode_cn, 'same_as_master')
                        self.app_config.set(slave_id, 'volume_mode', mode_en)
                        svars['volume_mode'].set(mode_en)
                    elif key == 'follow_master_id':
                        master_cn = svars.get('follow_master_id_cn').get()
                        master_en = self.master_id_map.get(master_cn, 'master1')
                        self.app_config.set(slave_id, 'follow_master_id', master_en)
                        if 'follow_master_id' in svars: svars['follow_master_id'].set(master_en)
                    elif key == 'password':
                        value_to_save = str(svars[key].get())
                        if self.app_config.get(slave_id, 'password', fallback='') != value_to_save:
                            self.pending_verification_config[slave_id] = {k:v for k,v in self.app_config.items(slave_id)}
                            self.pending_verification_config[slave_id]['password'] = value_to_save
                            self.verified_passwords.discard(slave_id)
                            log_queue.put(f"检测到账户 {slave_id} 密码已更改，将进行后台验证。")
                        self.app_config.set(slave_id, key, value_to_save)
                    elif key in svars: self.app_config.set(slave_id, key, str(svars[key].get()))
                map_items = [f"{k}->{v[0]}:{v[1]}" for k, v in self.per_slave_mapping.get(slave_id, {}).items()]
                self.app_config.set(slave_id, 'symbol_map', ",".join(map_items))

            for field, var in self.default_vars.items(): self.app_config.set('DEFAULT', field, str(var.get()))
            
            self.connection_failures.clear()
            self.write_config_to_file()
            log_queue.put("配置已在内存中更新，后台将尝试使用新配置连接...")

        except Exception as e:
            messagebox.showerror("错误", f"保存配置时出错: {e}")

    def write_config_to_file(self):
        try:
            config_to_write = configparser.ConfigParser()
            config_to_write.read_dict(self.app_config)
            for section in self.verified_passwords:
                if config_to_write.has_option(section, 'password'):
                    config_to_write.set(section, 'password', encrypt_password(config_to_write.get(section, 'password')))
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f: config_to_write.write(f)
            self.log_message("配置已成功保存。")
        except Exception as e:
            messagebox.showerror("错误", f"保存配置失败: {e}")
            
    def browse_path(self, var):
        path = filedialog.askopenfilename(title="选择MT5终端", filetypes=[("MT5", "terminal64.exe"), ("所有文件", "*")])
        if path: var.set(path.replace("/", "\\"))

    def force_close_all_accounts(self):
        if not self.logged_in_accounts:
            messagebox.showinfo("提示", "没有已登录的账户可供清仓。")
            return
        if messagebox.askyesno("最高权限操作确认", "此操作将强制停止所有正在运行的策略，并清空所有已配置账户的仓位与挂单，是否继续？"):
            task_queue.put({'action': 'CLOSE_ALL_ACCOUNTS_FORCEFULLY'})
        
    def close_all_for_account(self, account_id):
        if messagebox.askyesno("确认", f"确定要清空账户 {account_id} 的所有持仓和挂单吗？\n如果该账户有策略或跟单在运行，将会被先停止。"):
            if 'slave' in account_id:
                idx = int(account_id.replace('slave', '')) - 1
                vars_dict = self.slave_vars_list[idx]
                if vars_dict['enabled'].get():
                    self.log_message(f"检测到账户 {account_id} 正在跟单，将先停止跟单...")
                    self.toggle_slave_enabled(account_id, False)
            task_queue.put({'action': 'STOP_AND_CLOSE', 'account_id': account_id})

    def toggle_login(self, account_id):
        vars_dict = self._get_vars_by_id(account_id)
        if account_id in self.logged_in_accounts: # 注销逻辑
            if 'slave' in account_id and vars_dict['enabled'].get():
                self.toggle_slave_enabled(account_id, False)
            if account_id in self.strategy_instances:
                self.stop_strategy_for_account(account_id)
            
            self.logged_in_accounts.discard(account_id)
            self.equity_data[account_id] = None
            account_info_queue.put({'id': account_id, 'status': 'logged_out'})
            log_queue.put(f"账户 {account_id} 已注销。")
            
            # 启用输入框
            for widget in self.account_widgets_to_disable.get(account_id, []):
                if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Radiobutton, ttk.Checkbutton, ttk.Button)):
                    widget.config(state='normal')

        else: # 登录逻辑
            self.save_config() # 登录前先保存当前配置
            self.logged_in_accounts.add(account_id)
            log_queue.put(f"账户 {account_id} 已登录，后台将开始处理。")
            
            # 禁用输入框
            for widget in self.account_widgets_to_disable.get(account_id, []):
                if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Radiobutton, ttk.Checkbutton, ttk.Button)):
                    widget.config(state='disabled')

        self.update_login_button_state(vars_dict, account_id in self.logged_in_accounts)

    def toggle_slave_enabled(self, account_id, enable, from_ui=True):
        idx = int(account_id.replace('slave', '')) - 1
        vars_dict = self.slave_vars_list[idx]
        vars_dict['enabled'].set(enable)
        self.update_follow_button_state(vars_dict)
        if from_ui:
            status = "启用" if enable else "停用"
            self.log_message(f"账户 {account_id} 跟随功能已{status}。")
            self.save_config()

    def update_follow_button_state(self, vars_dict):
        is_enabled = vars_dict['enabled'].get()
        vars_dict['start_follow_btn'].config(state='disabled' if is_enabled else 'normal')
        vars_dict['stop_follow_btn'].config(state='normal' if is_enabled else 'disabled')

    def update_login_button_state(self, vars_dict, is_logged_in):
        if is_logged_in:
            vars_dict['login_btn'].config(text="注销", style="danger.TButton")
        else:
            vars_dict['login_btn'].config(text="登录", style="TButton")
            self.clear_account_info_display(vars_dict)

    def start_strategy_for_account(self, account_id):
        vars_dict = self._get_vars_by_id(account_id)
        strategy_name = vars_dict['strategy_var'].get()
        if not strategy_name or strategy_name == '(无可用策略)':
            messagebox.showwarning("提示", "请先为该账户选择一个有效的策略。")
            return
        if account_id not in self.logged_in_accounts:
            messagebox.showwarning("提示", "请先登录该账户，再启动策略。")
            return
        task_queue.put({'action': 'START_STRATEGY', 'account_id': account_id, 'strategy_name': strategy_name, 'params': {}})

    def stop_strategy_for_account(self, account_id):
        task_queue.put({'action': 'STOP_STRATEGY', 'account_id': account_id})

    def configure_strategy_for_account(self, account_id):
        vars_dict = self._get_vars_by_id(account_id)
        strategy_name = vars_dict['strategy_var'].get()
        if not strategy_name or strategy_name == '(无可用策略)':
            messagebox.showwarning("提示", "请先为该账户选择一个有效的策略。")
            return
        strategy_info = self.available_strategies.get(strategy_name)
        if not strategy_info or not strategy_info.get('params_config'):
            messagebox.showinfo("提示", f"策略 '{strategy_name}' 无可配置的特定参数。")
            return
        StrategyConfigWindow(self, self.app_config, log_queue, account_id, strategy_name, strategy_info['params_config'])

    def _get_vars_by_id(self, account_id):
        idx = int(account_id.replace('master' if 'master' in account_id else 'slave', '')) - 1
        return self.master_vars_list[idx] if 'master' in account_id else self.slave_vars_list[idx]

    def update_log(self):
        try:
            while True: self.log_message(log_queue.get_nowait())
        except Empty: pass
        self.after(250, self.update_log)

    def log_message(self, message):
        ts = time.strftime('%H:%M:%S')
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, f"{ts} - {message}\n")
        self.log_text.see(tk.END); self.log_text.config(state="disabled")
        self.status_var.set(f"{ts} - {message[:70]}")
    
    def update_account_info(self):
        try:
            while True:
                info = account_info_queue.get_nowait()
                account_id = info.get('id')
                if not account_id: continue
                vars_dict = self._get_vars_by_id(account_id)
                
                if 'balance' in info:
                    vars_dict['balance_var'].set(f"余额: {info['balance']:,.2f}")
                    vars_dict['summary_balance_var'].set(f"{info.get('balance', 0):,.2f}")
                if 'equity' in info:
                    vars_dict['equity_var'].set(f"净值: {info['equity']:,.2f}")
                    vars_dict['summary_equity_var'].set(f"{info.get('equity', 0):,.2f}")
                if 'profit' in info:
                    profit = info['profit']
                    color = '#4CAF50' if profit >= 0 else '#F44336'
                    vars_dict['profit_var'].set(f"浮盈: {profit:,.2f}")
                    vars_dict['profit_widget'].config(foreground=color)
                    vars_dict['summary_profit_var'].set(f"{profit:,.2f}")
                    vars_dict['summary_profit_widget'].config(foreground=color)
                if 'margin_free' in info: vars_dict['summary_margin_free_var'].set(f"{info.get('margin_free', 0):,.2f}")
                if 'margin_level' in info:
                    level = info['margin_level']
                    vars_dict['margin_level_var'].set(f"比例: {level:,.2f}%" if level < 100000 else "比例: ∞")
                    vars_dict['summary_margin_level_var'].set(f"{info.get('margin_level', 0):,.2f}%")
                if 'total_positions' in info: vars_dict['total_positions_var'].set(f"持仓: {info['total_positions']}")
                if 'total_volume' in info: vars_dict['total_volume_var'].set(f"手数: {info['total_volume']:.2f}")
                if 'credit' in info: vars_dict['credit_var'].set(f"赠金: {info['credit']:,.2f}")
                if 'credit' in info: 
                    vars_dict['credit_var'].set(f"赠金: {info['credit']:,.2f}")
                if 'total_positions' in info:
                    vars_dict['summary_total_positions_var'].set(f"{info['total_positions']}")
                if 'swap' in info: vars_dict['swap_var'].set(f"隔夜利息: {info['swap']:,.2f}")

                self.update_login_button_state(vars_dict, True)
                if 'positions_data' in info:
                    vars_dict['positions_data'] = info['positions_data']
                    selected_slave_idx = self.positions_tab_slave_selector.current()
                    if selected_slave_idx != -1 and account_id == f'slave{selected_slave_idx + 1}':
                        self._update_positions_tree(self.slave_positions_tree, info['positions_data'])
                    elif account_id.startswith('master'):
                        selected_master_idx = self.positions_tab_master_selector.current()
                        if selected_master_idx != -1 and account_id == f'master{selected_master_idx + 1}':
                            self._update_positions_tree(self.master_positions_tree, info['positions_data'])
                
                status = info.get('status', 'unknown')
                status_map = {
                    'copying': ('状态: 正常跟单', '#4CAF50'), 'connected': ('状态: 已连接', '#2196F3'), 
                    'error': ('状态: 连接失败', '#F44336'), 'disabled': ('状态: 已禁用', '#FF9800'), 
                    'inactive': ('状态: 已连接', '#2196F3'), 'logged_out': ('状态: 未登录', 'grey'),
                    'strategy_running': ('状态: 策略运行中', '#9C27B0'),
                    'locked': ('状态: 连接锁定', '#607D8B'), 'config_incomplete': ('状态: 配置不完整', '#FFC107')
                }
                text, color = status_map.get(status, ('状态: --', 'grey'))
                vars_dict['status_var'].set(text)
                vars_dict['status_widget'].config(foreground=color)
                
                is_strategy_running = status == 'strategy_running'
                vars_dict['strategy_start_btn'].config(state='disabled' if is_strategy_running else 'normal')
                vars_dict['strategy_stop_btn'].config(state='normal' if is_strategy_running else 'disabled')
                vars_dict['strategy_selector'].config(state='disabled' if is_strategy_running else 'readonly')
                if 'start_follow_btn' in vars_dict:
                    vars_dict['start_follow_btn'].config(state='disabled' if is_strategy_running or vars_dict['enabled'].get() else 'normal')
                    vars_dict['stop_follow_btn'].config(state='disabled' if is_strategy_running or not vars_dict['enabled'].get() else 'normal')
                
                if status == 'logged_out':
                    self.clear_account_info_display(vars_dict)
                
                self.update_login_button_state(vars_dict, account_id in self.logged_in_accounts)

                self.equity_data[account_id] = info.get('equity') if status in ['connected', 'copying', 'strategy_running'] else None
                
        except (Empty, IndexError): pass
        
        totals = {'master': {'balance': 0, 'equity': 0, 'profit': 0, 'margin_free': 0},
                  'slave': {'balance': 0, 'equity': 0, 'profit': 0, 'margin_free': 0}}
                  'slave': {'balance': 0, 'equity': 0, 'profit': 0, 'margin_free': 0, 'total_positions': 0}}
        totals['master']['total_positions'] = 0
        
        for i, acc_vars in enumerate(self.master_vars_list + self.slave_vars_list):
            if acc_vars['account_id'] in self.logged_in_accounts:
                try:
                    acc_type = 'master' if i < NUM_MASTERS else 'slave'
                    totals[acc_type]['balance'] += float(acc_vars['summary_balance_var'].get().replace(',', ''))
                    totals[acc_type]['equity'] += float(acc_vars['summary_equity_var'].get().replace(',', ''))
                    totals[acc_type]['profit'] += float(acc_vars['summary_profit_var'].get().replace(',', ''))
                    totals[acc_type]['margin_free'] += float(acc_vars['summary_margin_free_var'].get().replace(',', ''))
                    totals[acc_type]['balance'] += float(acc_vars.get('summary_balance_var').get().replace(',', ''))
                    totals[acc_type]['equity'] += float(acc_vars.get('summary_equity_var').get().replace(',', ''))
                    totals[acc_type]['profit'] += float(acc_vars.get('summary_profit_var').get().replace(',', ''))
                    totals[acc_type]['margin_free'] += float(acc_vars.get('summary_margin_free_var').get().replace(',', ''))
                    totals[acc_type]['total_positions'] += int(acc_vars.get('summary_total_positions_var').get())
                except (ValueError, KeyError): continue

        for acc_type in ['master', 'slave']:
            self.summary_vars[f'{acc_type}_total_balance'].set(f"{totals[acc_type]['balance']:,.2f}")
            self.summary_vars[f'{acc_type}_total_equity'].set(f"{totals[acc_type]['equity']:,.2f}")
            self.summary_vars[f'{acc_type}_total_profit'].set(f"{totals[acc_type]['profit']:,.2f}")
            self.summary_vars[f'{acc_type}_total_margin_free'].set(f"{totals[acc_type]['margin_free']:,.2f}")
            self.summary_vars[f'{acc_type}_total_positions'].set(f"{totals[acc_type]['total_positions']}")

        total_balance = totals['master']['balance'] + totals['slave']['balance']
        total_equity = totals['master']['equity'] + totals['slave']['equity']
        total_profit = totals['master']['profit'] + totals['slave']['profit']
        total_margin_free = totals['master']['margin_free'] + totals['slave']['margin_free']
        total_positions = totals['master']['total_positions'] + totals['slave']['total_positions']

        self.summary_vars['total_balance'].set(f"{total_balance:,.2f}")
        self.summary_vars['total_equity'].set(f"{total_equity:,.2f}")
        self.summary_vars['total_profit'].set(f"{total_profit:,.2f}")
        self.summary_vars['total_margin_free'].set(f"{total_margin_free:,.2f}")
        self.summary_vars['total_positions'].set(f"{total_positions}")

        self.after(500, self.update_account_info)

    def clear_account_info_display(self, vars_dict):
        info_points = ['balance', 'equity', 'profit', 'margin_level', 'total_positions', 'total_volume', 'credit', 'swap']
        info_texts = {'balance': '余额', 'equity': '净值', 'profit': '浮盈', 'margin_level': '比例', 'total_positions': '持仓', 'total_volume': '手数', 'credit': '赠金', 'swap': '隔夜利息'}
        for name in info_points:
            if f'{name}_var' in vars_dict:
                vars_dict[f'{name}_var'].set(f"{info_texts[name]}: --")
        vars_dict['profit_widget'].config(foreground='black')
        
        summary_points = ['summary_balance_var', 'summary_equity_var', 'summary_margin_free_var', 'summary_margin_level_var', 'summary_profit_var']
        summary_points = ['summary_balance_var', 'summary_equity_var', 'summary_margin_free_var', 'summary_margin_level_var', 'summary_profit_var', 'summary_total_positions_var']
        for name in summary_points:
            if name in vars_dict:
                vars_dict[name].set("--")
        if 'summary_profit_widget' in vars_dict:
            vars_dict['summary_profit_widget'].config(foreground='black')

    def _start_worker_thread(self):
        self.worker = threading.Thread(target=self.worker_thread, daemon=True)
        self.worker.start()

    def _update_positions_tree(self, tree, positions_data):
        tree.delete(*tree.get_children())
        for trade in sorted(positions_data, key=lambda x: x.ticket, reverse=True):
            is_position = hasattr(trade, 'profit')
            if is_position:
                trade_type = "Buy" if trade.type == mt5.ORDER_TYPE_BUY else "Sell"
                profit_or_status = f"{trade.profit:,.2f}"
                tag = 'profit' if trade.profit >= 0 else 'loss'
                volume = trade.volume
            else:
                type_map = {2:"Buy Stop", 3:"Sell Stop", 4:"Buy Limit", 5:"Sell Limit"}
                trade_type = type_map.get(trade.type, "Pending")
                profit_or_status = "挂单中"
                tag = 'pending'
                volume = trade.volume_initial
            values = (trade.ticket, trade.symbol, trade_type, f"{volume:.2f}", f"{trade.price_open:.5f}", f"{trade.sl:.5f}", f"{trade.tp:.5f}", profit_or_status, "❌")
            tree.insert('', 'end', values=values, tags=(tag,))

    def on_slave_tab_changed(self, event=None):
        try:
            selected_tab_index = self.slave_notebook.index(self.slave_notebook.select())
            vars_dict = self.slave_vars_list[selected_tab_index]
            self._update_positions_tree(self.slave_positions_tree, vars_dict.get('positions_data', []))
        except (tk.TclError, IndexError):
            if hasattr(self, 'slave_positions_tree'):
                self.slave_positions_tree.delete(*self.slave_positions_tree.get_children())

    def on_positions_tab_slave_selected(self, event=None):
        try:
            selected_tab_index = self.positions_tab_slave_selector.current()
            if selected_tab_index == -1: return
            vars_dict = self.slave_vars_list[selected_tab_index]
            self._update_positions_tree(self.slave_positions_tree, vars_dict.get('positions_data', []))
        except (tk.TclError, IndexError):
            if hasattr(self, 'slave_positions_tree'):
                self.slave_positions_tree.delete(*self.slave_positions_tree.get_children())

    def on_positions_tab_master_selected(self, event=None):
        try:
            selected_tab_index = self.positions_tab_master_selector.current()
            if selected_tab_index == -1: return
            vars_dict = self.master_vars_list[selected_tab_index]
            self._update_positions_tree(self.master_positions_tree, vars_dict.get('positions_data', []))
        except (tk.TclError, IndexError):
            if hasattr(self, 'master_positions_tree'):
                self.master_positions_tree.delete(*self.master_positions_tree.get_children())

    def on_treeview_click(self, event):
        tree = event.widget
        if tree.identify("region", event.x, event.y) != "cell": return
        column_id = tree.identify_column(event.x)
        if column_id != f"#{len(tree['columns'])}": return
        
        row_id = tree.identify_row(event.y)
        item = tree.item(row_id)
        if not item or not item['values']: return

        account_id = None
        if tree is self.slave_positions_tree:
            account_id = f'slave{self.positions_tab_slave_selector.current() + 1}'
        elif tree is self.master_positions_tree:
            account_id = f'master{self.positions_tab_master_selector.current() + 1}'
        if not account_id: return

        ticket, symbol = item['values'][0], item['values'][1]
        if messagebox.askyesno("确认平仓", f"确定要平仓账户 {account_id} 的订单吗？\n\n订单号: {ticket}\n品种: {symbol}"):
            self.log_message(f"正在为账户 {account_id} 发送平仓指令 (订单: {ticket})...")
            task_queue.put({'action': 'CLOSE_SINGLE_TRADE', 'account_id': account_id, 'ticket': ticket})

if __name__ == "__main__":
    if not os.path.exists(STRATEGIES_DIR):
        os.makedirs(STRATEGIES_DIR)
    app = TradeCopierApp()
    app.mainloop()

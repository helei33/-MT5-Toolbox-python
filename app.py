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
import pandas as pd
import traceback # <-- 新增导入
from backtest_engine import Backtester
from data_manager import DataManager

from manual import MANUAL_TEXT
from strategy_guide import GUIDE_TEXT
from constants import NUM_SLAVES, NUM_MASTERS, STRATEGIES_DIR, CONFIG_FILE, APP_DATA_DIR
from core_utils import BaseStrategy, encrypt_password, decrypt_password
from ui_utils import ScrolledFrame, ModifySLTPWindow, StrategyConfigWindow
from mt5_utils import _connect_mt5

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
data_task_queue = Queue()  # 数据同步任务队列
data_log_queue = Queue()   # 数据同步日志队列
backtest_result_queue = Queue()  # 回测结果队列
stop_event = threading.Event()



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

class BacktestWindow(tk.Toplevel):
    def __init__(self, master, app_instance, strategy_name, strategy_info):
        super().__init__(master)
        self.title(f"策略回测 - {strategy_name}")
        self.geometry("700x600")
        self.transient(master)
        self.grab_set()

        self.app = app_instance
        self.log_queue = app_instance.log_queue
        self.strategy_name = strategy_name
        self.strategy_info = strategy_info
        self.params_config = strategy_info.get('params_config', {})

        # --- 1. 创建参数框 ---
        params_frame = ttk.LabelFrame(self, text="回测参数", padding=10)
        params_frame.pack(fill=tk.X, padx=10, pady=10)
        params_frame.columnconfigure(1, weight=1)
        params_frame.columnconfigure(3, weight=1)

        # 辅助函数，用于从策略默认配置中获取值
        def get_default(key):
            return self.params_config.get(key, {}).get('default', '')

        # 定义 StringVars
        self.symbol_var = tk.StringVar(value=get_default('symbol'))
        self.timeframe_var = tk.StringVar(value=get_default('timeframe'))
        self.start_date_var = tk.StringVar(value="2023-01-01")
        self.end_date_var = tk.StringVar(value=datetime.now().strftime('%Y-%m-%d'))
        self.cash_var = tk.StringVar(value="10000")

        # 布局UI
        ttk.Label(params_frame, text="交易品种:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Entry(params_frame, textvariable=self.symbol_var).grid(row=0, column=1, sticky=tk.EW, padx=5, pady=2)
        
        ttk.Label(params_frame, text="初始资金:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=2)
        ttk.Entry(params_frame, textvariable=self.cash_var).grid(row=0, column=3, sticky=tk.EW, padx=5, pady=2)

        ttk.Label(params_frame, text="K线周期:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Entry(params_frame, textvariable=self.timeframe_var).grid(row=1, column=1, sticky=tk.EW, padx=5, pady=2)

        ttk.Label(params_frame, text="开始日期 (Y-m-d):").grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Entry(params_frame, textvariable=self.start_date_var).grid(row=2, column=1, sticky=tk.EW, padx=5, pady=2)

        ttk.Label(params_frame, text="结束日期 (Y-m-d):").grid(row=2, column=2, sticky=tk.W, padx=5, pady=2)
        ttk.Entry(params_frame, textvariable=self.end_date_var).grid(row=2, column=3, sticky=tk.EW, padx=5, pady=2)

        # --- 2. 创建控制按钮 ---
        control_frame = ttk.Frame(self)
        control_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        control_frame.columnconfigure(0, weight=1)
        control_frame.columnconfigure(1, weight=1)

        self.download_btn = ttk.Button(control_frame, text="1. 下载数据 (使用主账户1)", command=self.start_download_thread)
        self.download_btn.grid(row=0, column=0, sticky=tk.EW, padx=(0, 5), ipady=5)
        
        self.run_btn = ttk.Button(control_frame, text="2. 开始回测", command=self.start_backtest_thread)
        self.run_btn.grid(row=0, column=1, sticky=tk.EW, padx=(5, 0), ipady=5)

        # --- 3. 创建报告区域 ---
        report_frame = ttk.LabelFrame(self, text="回测报告", padding=10)
        report_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        report_frame.rowconfigure(0, weight=1)
        report_frame.columnconfigure(0, weight=1)

        self.report_text = scrolledtext.ScrolledText(report_frame, wrap=tk.WORD, state="disabled", font=("微软雅黑", 10))
        self.report_text.grid(row=0, column=0, sticky="nsew")

    def _log_to_report(self, message):
        """安全地在ScrolledText中记录日志"""
        def task():
            self.report_text.config(state="normal")
            self.report_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
            self.report_text.see(tk.END)
            self.report_text.config(state="disabled")
        self.app.after(0, task) # 确保在主线程中更新UI

    def start_download_thread(self):
        self.download_btn.config(state="disabled", text="正在下载...")
        self._log_to_report("开始下载数据任务...")
        threading.Thread(target=self._download_thread_target, daemon=True).start()

    def _download_thread_target(self):
        try:
            symbol = self.symbol_var.get()
            tf = self.timeframe_var.get()
            start = self.start_date_var.get()
            end = self.end_date_var.get()

            if not all([symbol, tf, start, end]):
                self._log_to_report("错误：所有参数均不能为空。")
                return

            mt5_config = self.app.app_config['master1']
            if not all(mt5_config.get(k) for k in ['path', 'login', 'password', 'server']):
                self._log_to_report("错误：主账户1 (Master 1) 配置不完整，无法用于下载数据。")
                return

            self._log_to_report(f"使用主账户1连接MT5以下载 {symbol} ({tf}) 从 {start} 到 {end} 的数据...")
            
            data_manager = DataManager()
            success = data_manager.sync_data(
                symbols=[symbol], 
                timeframes=[tf], 
                mt5_config=mt5_config, 
                log_queue=self.log_queue, # 使用主APP的日志队列
                start_date_str=start, 
                end_date_str=end
            )
            
            if success:
                self._log_to_report(f"数据同步完成。")
            else:
                self._log_to_report(f"数据同步失败，请检查主APP日志。")

        except Exception as e:
            self._log_to_report(f"下载线程出错: {e}\n{traceback.format_exc()}")
        finally:
            self.app.after(0, lambda: self.download_btn.config(state="normal", text="1. 下载数据 (使用主账户1)"))

    def start_backtest_thread(self):
        self.run_btn.config(state="disabled", text="正在回测...")
        self.report_text.config(state="normal")
        self.report_text.delete("1.0", tk.END)
        self.report_text.config(state="disabled")
        self._log_to_report(f"开始策略 [{self.strategy_name}] 的回测...")
        threading.Thread(target=self._backtest_thread_target, daemon=True).start()

    def _backtest_thread_target(self):
        try:
            # 1. 获取UI参数
            symbol = self.symbol_var.get()
            tf = self.timeframe_var.get()
            start = self.start_date_var.get()
            end = self.end_date_var.get()
            try:
                cash = float(self.cash_var.get())
            except ValueError:
                self._log_to_report("错误：初始资金必须是一个数字。")
                return

            # 2. 从DataManager获取数据
            self._log_to_report(f"正在从本地数据库加载 {symbol} ({tf}) 数据...")
            data_manager = DataManager()
            data = data_manager.get_data(symbol, tf, start, end)

            if data is None or data.empty:
                self._log_to_report(f"错误：本地未找到所需数据。请先点击'下载数据'按钮。")
                return

            self._log_to_report(f"成功加载 {len(data)} 条K线数据。")

            # 3. 准备策略参数 (关键步骤)
            # 加载策略的全局默认参数
            raw_params = {}
            global_section = f"{self.strategy_name}_Global"
            
            for key, config in self.params_config.items():
                raw_params[key] = self.app.app_config.get(
                    global_section, 
                    key, 
                    fallback=config.get('default')
                )

            # 使用回测UI的值覆盖 'symbol' 和 'timeframe'
            self._log_to_report(f"使用回测参数覆盖策略默认值: Symbol='{symbol}', Timeframe='{tf}'")
            raw_params['symbol'] = symbol
            raw_params['timeframe'] = tf
            
            # 4. 运行回测
            self._log_to_report("正在实例化回测引擎并准备策略...")
            dummy_config = {'account_id': 'backtest'} # 回测策略不需要真实的账户配置
            
            backtester = Backtester(
                strategy_info=self.strategy_info,
                full_data=data,
                raw_params=raw_params, # <-- 传递原始参数
                config=dummy_config,
                log_queue=self.log_queue, # 使用主APP的日志队列
                start_cash=cash
            )

            self._log_to_report("回测引擎启动...")
            report = backtester.run() # 运行回测

            # 5. 显示报告
            self._log_to_report("回测完成！")
            self._log_to_report("\n" + "="*30 + "\n")
            self._log_to_report(report)

        except Exception as e:
            self._log_to_report(f"回测线程出错: {e}\n{traceback.format_exc()}")
        finally:
            self.app.after(0, lambda: self.run_btn.config(state="normal", text="2. 开始回测"))

class TradeCopierApp(ThemedTk):
    def __init__(self):
        super().__init__()
        if use_themed_tk: self.set_theme("arc")
        self.title("MT5 交易工具箱 (Python版1.0)")
        self.geometry("1070x660")
        self.minsize(1070, 660)
        
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

        # 初始化数据管理器
        self.data_manager = DataManager()
        
        # 数据同步相关变量
        self.data_sync_thread = None
        self.data_sync_in_progress = False
        self.data_sync_progress_var = tk.StringVar(value="就绪")

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

    def data_sync_worker_thread(self):
        """数据同步处理线程，处理data_task_queue中的任务"""
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
                    
                    # 使用主账户1的配置进行数据同步
                    try:
                        master1_config = self.app_config['master1']
                        if not all(master1_config.get(k) for k in ['path', 'login', 'password', 'server']):
                            data_log_queue.put("[DataManager] 错误: 主账户1配置不完整，请先配置主账户1")
                            continue
                    except KeyError:
                        data_log_queue.put("[DataManager] 错误: 找不到主账户1配置，请先配置主账户1")
                        continue
                    
                    # 调用数据管理器的同步方法
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
                import traceback
                data_log_queue.put(f"[DataManager] 数据同步线程发生错误: {e}\n{traceback.format_exc()}")
            finally:
                time.sleep(0.1)  # 短暂休眠

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
        self.create_data_center_tab(notebook) # 新增：创建数据中心标签页
        self.create_backtest_tab(notebook) # 新增：创建策略回测标签页
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
        ttk.Button(mng_btn_frame, text="刷新", command=lambda: (self.discover_strategies(), self.refresh_all_strategy_uis())).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        
        # --- *** 新增回测按钮 *** ---
        ttk.Button(mng_btn_frame, text="回测策略", command=self.open_backtest_window).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        # --- *** 结束修改 *** ---
        
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

    def create_data_center_tab(self, notebook):
        """创建数据中心标签页"""
        data_tab = ttk.Frame(notebook, padding=10)
        notebook.add(data_tab, text="数据中心")
        
        data_tab.columnconfigure(0, weight=1)
        data_tab.rowconfigure(1, weight=1)
        
        # 顶部控制面板
        control_frame = ttk.LabelFrame(data_tab, text="数据同步控制", padding=10)
        control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        control_frame.columnconfigure(1, weight=1)
        
        # 交易品种选择
        symbol_frame = ttk.Frame(control_frame)
        symbol_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(symbol_frame, text="交易品种:").pack(side=tk.LEFT)
        
        self.data_symbol_vars = {}
        symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "EURJPY", "GBPJPY"]
        for i, symbol in enumerate(symbols):
            var = tk.BooleanVar()
            self.data_symbol_vars[symbol] = var
            ttk.Checkbutton(symbol_frame, text=symbol, variable=var).pack(side=tk.LEFT, padx=(10, 0))
        
        # 时间周期选择
        timeframe_frame = ttk.Frame(control_frame)
        timeframe_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(timeframe_frame, text="时间周期:").pack(side=tk.LEFT)
        
        self.data_timeframe_vars = {}
        timeframes = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
        for i, tf in enumerate(timeframes):
            var = tk.BooleanVar()
            self.data_timeframe_vars[tf] = var
            ttk.Checkbutton(timeframe_frame, text=tf, variable=var).pack(side=tk.LEFT, padx=(10, 0))
        
        # 时间范围选择
        time_range_frame = ttk.Frame(control_frame)
        time_range_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        time_range_frame.columnconfigure(1, weight=1)
        time_range_frame.columnconfigure(3, weight=1)
        
        ttk.Label(time_range_frame, text="开始日期:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.data_start_date_var = tk.StringVar(value="2023-01-01")
        self.data_start_date_entry = ttk.Entry(time_range_frame, textvariable=self.data_start_date_var, width=12)
        self.data_start_date_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        
        ttk.Label(time_range_frame, text="结束日期:").grid(row=0, column=2, sticky="w", padx=(0, 5))
        self.data_end_date_var = tk.StringVar(value="2024-01-01")
        self.data_end_date_entry = ttk.Entry(time_range_frame, textvariable=self.data_end_date_var, width=12)
        self.data_end_date_entry.grid(row=0, column=3, sticky="ew")
        
        # 快速选择按钮
        quick_select_frame = ttk.Frame(control_frame)
        quick_select_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Label(quick_select_frame, text="快速选择:").pack(side=tk.LEFT)
        ttk.Button(quick_select_frame, text="最近1年", command=lambda: self.set_data_quick_date_range(365)).pack(side=tk.LEFT, padx=(10, 5))
        ttk.Button(quick_select_frame, text="最近6个月", command=lambda: self.set_data_quick_date_range(180)).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(quick_select_frame, text="最近3个月", command=lambda: self.set_data_quick_date_range(90)).pack(side=tk.LEFT, padx=(0, 5))
        
        # 控制按钮
        button_frame = ttk.Frame(control_frame)
        button_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        
        self.start_sync_button = ttk.Button(button_frame, text="开始同步数据", command=self.start_data_sync)
        self.start_sync_button.pack(side=tk.LEFT, padx=(0, 10))
        
        self.stop_sync_button = ttk.Button(button_frame, text="停止同步", command=self.stop_data_sync, state="disabled")
        self.stop_sync_button.pack(side=tk.LEFT, padx=(0, 10))
        
        self.auto_sync_button = ttk.Button(button_frame, text="一键同步回测数据", command=self.auto_sync_backtest_data)
        self.auto_sync_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # 进度显示
        progress_frame = ttk.Frame(control_frame)
        progress_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        progress_frame.columnconfigure(1, weight=1)
        
        ttk.Label(progress_frame, text="状态:").grid(row=0, column=0, sticky="w")
        self.data_sync_status_label = ttk.Label(progress_frame, textvariable=self.data_sync_progress_var)
        self.data_sync_status_label.grid(row=0, column=1, sticky="w", padx=(10, 0))
        
        # 添加确定性进度条
        self.data_sync_progress_bar = ttk.Progressbar(progress_frame, mode='determinate', length=300)
        self.data_sync_progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        
        # 进度百分比标签
        self.data_sync_progress_label = ttk.Label(progress_frame, text="0%")
        self.data_sync_progress_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        
        # 配置状态标签
        self.data_sync_config_label = ttk.Label(progress_frame, text="", foreground="red")
        self.data_sync_config_label.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        
        # 数据仓库信息
        info_frame = ttk.LabelFrame(data_tab, text="本地数据仓库", padding=10)
        info_frame.grid(row=1, column=0, sticky="nsew")
        info_frame.rowconfigure(1, weight=1)
        info_frame.columnconfigure(0, weight=1)
        
        # 刷新按钮和数据需求提示
        refresh_frame = ttk.Frame(info_frame)
        refresh_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        refresh_frame.columnconfigure(1, weight=1)
        
        ttk.Button(refresh_frame, text="刷新数据列表", command=self.refresh_data_list).grid(row=0, column=0, sticky="w")
        
        # 数据需求提示
        self.data_requirement_label = ttk.Label(refresh_frame, text="", foreground="blue", font=("微软雅黑", 9))
        self.data_requirement_label.grid(row=0, column=1, sticky="e", padx=(10, 0))
        
        # 数据列表
        list_frame = ttk.Frame(info_frame)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        
        # 创建Treeview显示数据
        columns = ("交易品种", "时间周期", "数据条数", "开始日期", "结束日期")
        self.data_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)
        
        for col in columns:
            self.data_tree.heading(col, text=col)
            self.data_tree.column(col, width=120)
        
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.data_tree.yview)
        self.data_tree.configure(yscrollcommand=scrollbar.set)
        
        self.data_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        
        # 初始化数据列表（延迟到所有组件创建完成后）
        self.after(100, self.refresh_data_list)
        # 更新数据需求提示
        self.after(200, self.update_data_requirement_hint)

    def create_backtest_tab(self, notebook):
        """创建策略回测标签页"""
        backtest_tab = ttk.Frame(notebook, padding=10)
        notebook.add(backtest_tab, text="策略回测")
        backtest_tab.rowconfigure(1, weight=1)
        backtest_tab.columnconfigure(0, weight=1)
        
        # 回测配置区域
        config_frame = ttk.LabelFrame(backtest_tab, text="回测配置", padding=10)
        config_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        config_frame.columnconfigure(1, weight=1)
        
        # 策略选择
        ttk.Label(config_frame, text="选择策略:").grid(row=0, column=0, sticky="w", pady=5)
        self.backtest_strategy_var = tk.StringVar()
        self.backtest_strategy_selector = ttk.Combobox(config_frame, textvariable=self.backtest_strategy_var, state="readonly")
        self.backtest_strategy_selector.grid(row=0, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        # 交易品种选择
        ttk.Label(config_frame, text="交易品种:").grid(row=1, column=0, sticky="w", pady=5)
        self.backtest_symbol_var = tk.StringVar()
        symbol_options = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF"]
        ttk.Combobox(config_frame, textvariable=self.backtest_symbol_var, values=symbol_options, state="readonly").grid(row=1, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        # 时间周期选择
        ttk.Label(config_frame, text="时间周期:").grid(row=2, column=0, sticky="w", pady=5)
        self.backtest_tf_var = tk.StringVar()
        timeframe_options = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
        ttk.Combobox(config_frame, textvariable=self.backtest_tf_var, values=timeframe_options, state="readonly").grid(row=2, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        # 日期范围
        date_frame = ttk.Frame(config_frame)
        date_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=5)
        date_frame.columnconfigure(1, weight=1)
        date_frame.columnconfigure(3, weight=1)
        
        ttk.Label(date_frame, text="开始日期:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.backtest_start_var = tk.StringVar(value="2023-01-01")
        ttk.Entry(date_frame, textvariable=self.backtest_start_var, width=12).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        
        ttk.Label(date_frame, text="结束日期:").grid(row=0, column=2, sticky="w", padx=(0, 5))
        self.backtest_end_var = tk.StringVar(value="2024-01-01")
        ttk.Entry(date_frame, textvariable=self.backtest_end_var, width=12).grid(row=0, column=3, sticky="ew")

        # 快速选择按钮
        quick_select_frame = ttk.Frame(config_frame)
        quick_select_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 5))
        ttk.Label(quick_select_frame, text="快捷选择:").pack(side=tk.LEFT)
        ttk.Button(quick_select_frame, text="一天", command=lambda: self.set_backtest_quick_date_range(1)).pack(side=tk.LEFT, padx=(10, 5))
        ttk.Button(quick_select_frame, text="三天", command=lambda: self.set_backtest_quick_date_range(3)).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(quick_select_frame, text="一周", command=lambda: self.set_backtest_quick_date_range(7)).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(quick_select_frame, text="一个月", command=lambda: self.set_backtest_quick_date_range(30)).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(quick_select_frame, text="三个月", command=lambda: self.set_backtest_quick_date_range(90)).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(quick_select_frame, text="半年", command=lambda: self.set_backtest_quick_date_range(180)).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(quick_select_frame, text="一年", command=lambda: self.set_backtest_quick_date_range(365)).pack(side=tk.LEFT, padx=(0, 5))
        
        # 初始资金和杠杆
        ttk.Label(config_frame, text="初始资金 ($):").grid(row=5, column=0, sticky="w", pady=5)
        self.backtest_initial_cash_var = tk.StringVar(value="10000")
        ttk.Entry(config_frame, textvariable=self.backtest_initial_cash_var).grid(row=5, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        ttk.Label(config_frame, text="杠杆倍数:").grid(row=6, column=0, sticky="w", pady=5)
        self.backtest_leverage_var = tk.StringVar(value="100")
        leverage_values = ["10", "20", "50", "100", "200", "500"]
        ttk.Combobox(config_frame, textvariable=self.backtest_leverage_var, values=leverage_values, state="readonly").grid(row=6, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        # 控制按钮
        button_frame = ttk.Frame(config_frame)
        button_frame.grid(row=7, column=0, columnspan=2, pady=(10, 0))
        
        self.start_backtest_btn = ttk.Button(button_frame, text="开始回测", command=self.start_backtest)
        self.start_backtest_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        self.quick_backtest_btn = ttk.Button(button_frame, text="一键智能回测", command=self.quick_backtest)
        self.quick_backtest_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.stop_backtest_btn = ttk.Button(button_frame, text="停止回测", command=self.stop_backtest, state="disabled")
        self.stop_backtest_btn.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(button_frame, text="清空结果", command=self.clear_backtest_results).pack(side=tk.LEFT)
        
        # 回测结果区域
        result_frame = ttk.LabelFrame(backtest_tab, text="回测结果", padding=10)
        result_frame.grid(row=1, column=0, sticky="nsew")
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)
        
        # 结果文本框
        self.backtest_result_text = scrolledtext.ScrolledText(result_frame, height=20, wrap=tk.WORD)
        self.backtest_result_text.grid(row=0, column=0, sticky="nsew")

        # 回测进度与状态
        progress_frame = ttk.Frame(backtest_tab)
        progress_frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        progress_frame.columnconfigure(1, weight=1)
        ttk.Label(progress_frame, text="进度:").grid(row=0, column=0, sticky="w")
        self.backtest_progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.backtest_progress.grid(row=0, column=1, sticky="ew", padx=(8,0))
        self.backtest_status_var = tk.StringVar(value="就绪")
        ttk.Label(progress_frame, textvariable=self.backtest_status_var).grid(row=0, column=2, sticky="e", padx=(8,0))
        
        # 回测进度百分比标签
        self.backtest_progress_label = ttk.Label(progress_frame, text="0%")
        self.backtest_progress_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        
        # 初始化策略列表
        self.after(100, self.update_backtest_strategy_list)
        
        # 检查数据同步配置状态
        self.after(200, self.check_data_sync_config)
        
        # 设置默认值
        if symbol_options:
            self.backtest_symbol_var.set(symbol_options[0])
        if timeframe_options:
            self.backtest_tf_var.set(timeframe_options[0])

    def update_backtest_strategy_list(self):
        """更新回测策略列表"""
        try:
            strategy_names = sorted(self.available_strategies.keys()) if self.available_strategies else ['(无可用策略)']
            self.backtest_strategy_selector['values'] = strategy_names
            if strategy_names and strategy_names[0] != '(无可用策略)':
                self.backtest_strategy_selector.current(0)
        except Exception as e:
            self.log_message(f"更新回测策略列表时出错: {e}")

    def start_backtest(self):
        """开始回测"""
        try:
            # 获取回测参数
            strategy_name = self.backtest_strategy_var.get()
            symbol = self.backtest_symbol_var.get()
            timeframe_str = self.backtest_tf_var.get()
            start_date_str = self.backtest_start_var.get()
            end_date_str = self.backtest_end_var.get()
            initial_cash = self.backtest_initial_cash_var.get()
            leverage = self.backtest_leverage_var.get()
            
            # 验证参数
            if not strategy_name or strategy_name == '(无可用策略)':
                messagebox.showwarning("警告", "请选择一个策略")
                return
            
            if not symbol:
                messagebox.showwarning("警告", "请选择交易品种")
                return
                
            if not timeframe_str:
                messagebox.showwarning("警告", "请选择时间周期")
                return
                
            if not start_date_str or not end_date_str:
                messagebox.showwarning("警告", "请设置开始和结束日期")
                return
            
            # 验证日期格式
            try:
                from datetime import datetime
                datetime.strptime(start_date_str, "%Y-%m-%d")
                datetime.strptime(end_date_str, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("错误", "日期格式不正确，请使用 YYYY-MM-DD 格式")
                return
            
            # 验证数值参数
            try:
                initial_cash = float(initial_cash)
                leverage = int(leverage)
                if initial_cash <= 0 or leverage <= 0:
                    raise ValueError("初始资金和杠杆倍数必须大于0")
            except ValueError as e:
                messagebox.showerror("错误", f"回测参数无效: {e}")
                return
            
            # 禁用按钮
            self.start_backtest_btn.config(state="disabled")
            self.update_backtest_results("正在检查所需数据...\n", overwrite=True)
            
            # 检查数据是否存在
            data_exists = self.check_backtest_data(symbol, timeframe_str, start_date_str, end_date_str)
            
            if not data_exists:
                # 自动发起同步，下载完成后自动开始回测
                self.update_backtest_results(f"数据缺失，正在下载 {symbol} {timeframe_str} ({start_date_str}~{end_date_str})，请稍等...\n", overwrite=True)
                self.backtest_status_var.set("数据下载中...")
                if hasattr(self, 'backtest_progress'):
                    # 数据下载时使用确定性模式
                    self.backtest_progress.config(mode='determinate')
                    self.backtest_progress['value'] = 0
                    self.backtest_progress_label.config(text="0%")
                self.stop_backtest_btn.config(state="disabled")
                # 创建同步任务
                sync_task = {
                    'symbols': [symbol],
                    'timeframes': [timeframe_str],
                    'start_date': start_date_str,
                    'end_date': end_date_str
                }
                data_task_queue.put(sync_task)
                # 开线程等待数据到位，然后自动开始回测
                threading.Thread(target=self._wait_data_ready_and_start_backtest, 
                               args=(strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage), 
                               daemon=True).start()
                return
            
            self.update_backtest_results("数据检查通过，正在准备回测环境...\n", overwrite=True)
            
            self.log_message(f"开始回测策略 {strategy_name} on {symbol} {timeframe_str} (初始资金: ${initial_cash}, 杠杆: {leverage}x)...")
            
            # 创建可中断事件
            self.backtest_stop_event = threading.Event()
            self.backtest_pause_event = threading.Event()
            self.stop_backtest_btn.config(state="normal")
            if hasattr(self, 'backtest_progress'):
                # 回测时使用不确定模式
                self.backtest_progress.config(mode='indeterminate')
                self.backtest_progress.start(40)
                self.backtest_progress_label.config(text="回测进行中...")
            self.backtest_status_var.set("回测进行中...")
            # 启动回测线程
            thread = threading.Thread(target=self._run_backtest_task, args=(strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage))
            thread.daemon = True
            thread.start()
            
        except Exception as e:
            self.log_message(f"启动回测时出错: {e}")
            self.start_backtest_btn.config(state="normal")

    def _run_backtest_task(self, strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage):
        """运行回测任务"""
        try:
            # 获取策略信息
            if strategy_name not in self.available_strategies:
                self.after(0, lambda: self.update_backtest_results(f"错误: 找不到策略 {strategy_name}", overwrite=True))
                return
            
            strategy_info = self.available_strategies[strategy_name]
            
            # 获取策略的默认参数配置
            default_params = strategy_info.get('params_config', {})
            
            # 构建完整的参数，使用策略默认值
            params = {}
            for param_name, param_config in default_params.items():
                if param_name == 'symbol':
                    params[param_name] = symbol
                elif param_name == 'timeframe':
                    params[param_name] = timeframe_str
                else:
                    params[param_name] = param_config.get('default', 0)
            
            # 确保必要的参数存在
            if 'trade_volume' not in params:
                params['trade_volume'] = 0.01
            if 'magic_number' not in params:
                params['magic_number'] = 13579
            if 'stop_loss_pips' not in params:
                params['stop_loss_pips'] = 50
            if 'take_profit_pips' not in params:
                params['take_profit_pips'] = 100
            
            self.after(0, lambda: self.update_backtest_results("正在从本地数据仓库获取数据...", overwrite=True))
            
            # 从数据管理器获取数据
            full_data = self.data_manager.get_data(symbol, timeframe_str, start_date_str, end_date_str)
            
            if full_data is None or full_data.empty:
                error_msg = f"在本地数据仓库中没有找到 {symbol} {timeframe_str} 在 {start_date_str} 到 {end_date_str} 期间的数据。\n请先在数据中心同步相关数据。"
                self.after(0, lambda: self.update_backtest_results(error_msg, overwrite=True))
                return
            
            self.after(0, lambda: self.update_backtest_results(f"成功获取 {len(full_data)} 条K线数据，正在初始化回测引擎...", overwrite=True))
            
            # 创建回测器
            backtester = Backtester(
                strategy_info=strategy_info,
                full_data=full_data,
                params=params,
                config=self.app_config,
                log_queue=log_queue,
                start_cash=float(initial_cash),
                leverage=int(leverage),
                stop_event=getattr(self, 'backtest_stop_event', None),
                pause_event=getattr(self, 'backtest_pause_event', None)
            )
            
            # 运行回测
            results = backtester.run()
            self.after(0, lambda: self.update_backtest_results(results, overwrite=True))
            
        except Exception as e:
            error_msg = f"回测过程中发生错误: {e}\n{traceback.format_exc()}"
            self.after(0, lambda: self.update_backtest_results(error_msg, overwrite=True))
        finally:
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
            self.after(0, lambda: self.stop_backtest_btn.config(state="disabled"))
            if hasattr(self, 'backtest_progress'):
                self.after(0, lambda: self.backtest_progress.stop())
            self.after(0, lambda: self.backtest_status_var.set("就绪"))

    def _wait_data_ready_enable_start(self, symbol, timeframe_str, start_date_str, end_date_str):
        try:
            start_ts = time.time()
            timeout = 600
            while time.time() - start_ts < timeout:
                if self.check_backtest_data(symbol, timeframe_str, start_date_str, end_date_str):
                    self.after(0, lambda: self.update_backtest_results("数据下载完成，请点击'开始回测'继续。\n", overwrite=False))
                    self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
                    break
                time.sleep(1.5)
            else:
                self.after(0, lambda: self.update_backtest_results("数据下载超时，请稍后重试。\n", overwrite=False))
        finally:
            if hasattr(self, 'backtest_progress'):
                self.after(0, lambda: self.backtest_progress.stop())
            self.after(0, lambda: self.backtest_status_var.set("就绪"))

    def _wait_data_ready_and_start_backtest(self, strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage):
        """等待数据下载完成，然后自动开始回测"""
        try:
            start_ts = time.time()
            timeout = 600  # 10分钟超时
            
            self.after(0, lambda: self.update_backtest_results("正在等待数据下载完成...\n", overwrite=False))
            
            while time.time() - start_ts < timeout:
                if self.check_backtest_data(symbol, timeframe_str, start_date_str, end_date_str):
                    self.after(0, lambda: self.update_backtest_results("✓ 数据下载完成，正在启动回测...\n", overwrite=False))
                    
                    # 数据下载完成，自动开始回测
                    self.after(0, lambda: self._run_backtest_task(strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage))
                    return
                    
                time.sleep(2)  # 每2秒检查一次
            
            # 超时处理
            self.after(0, lambda: self.update_backtest_results("✗ 数据下载超时，请检查网络连接或手动同步数据。\n", overwrite=False))
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
            
        except Exception as e:
            self.after(0, lambda: self.update_backtest_results(f"等待数据时发生错误: {e}\n", overwrite=False))
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
        finally:
            if hasattr(self, 'backtest_progress'):
                self.after(0, lambda: self.backtest_progress.stop())
            self.after(0, lambda: self.backtest_status_var.set("就绪"))

    def stop_backtest(self):
        try:
            if hasattr(self, 'backtest_stop_event') and self.backtest_stop_event:
                self.backtest_stop_event.set()
                self.update_backtest_results("正在停止回测...\n", overwrite=False)
        except Exception:
            pass

    def update_backtest_results(self, message, overwrite=False):
        """更新回测结果"""
        try:
            if overwrite:
                self.backtest_result_text.delete(1.0, tk.END)
            self.backtest_result_text.insert(tk.END, message + "\n")
            self.backtest_result_text.see(tk.END)
        except Exception as e:
            self.log_message(f"更新回测结果时出错: {e}")

    def clear_backtest_results(self):
        """清空回测结果"""
        try:
            self.backtest_result_text.delete(1.0, tk.END)
        except Exception as e:
            self.log_message(f"清空回测结果时出错: {e}")

    def quick_backtest(self):
        """一键智能回测 - 自动处理所有数据同步和回测流程"""
        try:
            # 获取回测参数
            strategy_name = self.backtest_strategy_var.get()
            symbol = self.backtest_symbol_var.get()
            timeframe_str = self.backtest_tf_var.get()
            start_date_str = self.backtest_start_var.get()
            end_date_str = self.backtest_end_var.get()
            initial_cash = self.backtest_initial_cash_var.get()
            leverage = self.backtest_leverage_var.get()
            
            # 验证参数
            if not strategy_name or strategy_name == '(无可用策略)':
                messagebox.showwarning("警告", "请选择一个策略")
                return
            
            if not symbol or not timeframe_str or not start_date_str or not end_date_str:
                messagebox.showwarning("警告", "请设置完整的回测参数")
                return
            
            # 验证数值参数
            try:
                initial_cash = float(initial_cash)
                leverage = int(leverage)
                if initial_cash <= 0 or leverage <= 0:
                    raise ValueError("初始资金和杠杆倍数必须大于0")
            except ValueError as e:
                messagebox.showerror("错误", f"回测参数无效: {e}")
                return
            
            # 禁用按钮
            self.start_backtest_btn.config(state="disabled")
            self.quick_backtest_btn.config(state="disabled")
            
            # 清空结果并显示开始信息
            self.update_backtest_results("=== 一键智能回测开始 ===\n", overwrite=True)
            self.update_backtest_results(f"策略: {strategy_name}\n品种: {symbol}\n周期: {timeframe_str}\n时间范围: {start_date_str} 到 {end_date_str}\n", overwrite=False)
            
            # 启动智能回测线程
            thread = threading.Thread(
                target=self._run_smart_backtest, 
                args=(strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage)
            )
            thread.daemon = True
            thread.start()
            
        except Exception as e:
            self.log_message(f"启动一键智能回测时出错: {e}")
            self.start_backtest_btn.config(state="normal")
            self.quick_backtest_btn.config(state="normal")

    def _run_smart_backtest(self, strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage):
        """智能回测执行 - 自动处理数据同步和回测"""
        try:
            # 步骤1: 检查数据是否存在
            self.after(0, lambda: self.update_backtest_results("步骤1: 检查本地数据...\n", overwrite=False))
            
            data_exists = self.check_backtest_data(symbol, timeframe_str, start_date_str, end_date_str)
            
            if not data_exists:
                # 步骤2: 自动同步数据
                self.after(0, lambda: self.update_backtest_results("步骤2: 数据缺失，正在自动同步...\n", overwrite=False))
                
                # 创建数据同步任务
                sync_task = {
                    'symbols': [symbol],
                    'timeframes': [timeframe_str],
                    'start_date': start_date_str,
                    'end_date': end_date_str
                }
                data_task_queue.put(sync_task)
                
                # 等待数据同步完成
                self.after(0, lambda: self.update_backtest_results("正在下载历史数据，请稍候...\n", overwrite=False))
                
                sync_start_time = time.time()
                max_wait_time = 300  # 最多等待5分钟
                
                while time.time() - sync_start_time < max_wait_time:
                    if self.check_backtest_data(symbol, timeframe_str, start_date_str, end_date_str):
                        self.after(0, lambda: self.update_backtest_results("✓ 数据同步完成\n", overwrite=False))
                        break
                    time.sleep(2)
                else:
                    # 超时
                    self.after(0, lambda: self.update_backtest_results("✗ 数据同步超时，请检查网络连接\n", overwrite=False))
                    self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
                    self.after(0, lambda: self.quick_backtest_btn.config(state="normal"))
                    return
            else:
                self.after(0, lambda: self.update_backtest_results("✓ 本地数据已就绪\n", overwrite=False))
            
            # 步骤3: 执行回测
            self.after(0, lambda: self.update_backtest_results("步骤3: 开始执行回测...\n", overwrite=False))
            
            # 获取数据
            full_data = self.data_manager.get_data(symbol, timeframe_str, start_date_str, end_date_str)
            
            if full_data is None or full_data.empty:
                self.after(0, lambda: self.update_backtest_results("✗ 无法获取数据，回测失败\n", overwrite=False))
                return
            
            self.after(0, lambda: self.update_backtest_results(f"✓ 成功获取 {len(full_data)} 条K线数据\n", overwrite=False))
            
            # 获取策略信息
            if strategy_name not in self.available_strategies:
                self.after(0, lambda: self.update_backtest_results(f"✗ 错误: 找不到策略 {strategy_name}\n", overwrite=False))
                return
            
            strategy_info = self.available_strategies[strategy_name]
            
            # 获取策略的默认参数配置
            default_params = strategy_info.get('params_config', {})
            
            # 构建完整的参数，使用策略默认值
            params = {}
            for param_name, param_config in default_params.items():
                if param_name == 'symbol':
                    params[param_name] = symbol
                elif param_name == 'timeframe':
                    params[param_name] = timeframe_str
                else:
                    params[param_name] = param_config.get('default', 0)
            
            # 确保必要的参数存在
            if 'trade_volume' not in params:
                params['trade_volume'] = 0.01
            if 'magic_number' not in params:
                params['magic_number'] = 13579
            if 'stop_loss_pips' not in params:
                params['stop_loss_pips'] = 50
            if 'take_profit_pips' not in params:
                params['take_profit_pips'] = 100
            
            # 创建回测器
            self.after(0, lambda: self.update_backtest_results("正在初始化回测引擎...\n", overwrite=False))
            
            backtester = Backtester(
                strategy_info=strategy_info,
                full_data=full_data,
                params=params,
                config=self.app_config,
                log_queue=log_queue,
                start_cash=float(initial_cash),
                leverage=int(leverage)
            )
            
            # 运行回测
            self.after(0, lambda: self.update_backtest_results("正在执行策略回测...\n", overwrite=False))
            results = backtester.run()
            
            # 显示结果
            self.after(0, lambda: self.update_backtest_results("=== 回测完成 ===\n", overwrite=False))
            self.after(0, lambda: self.update_backtest_results(results, overwrite=False))
            
        except Exception as e:
            error_msg = f"智能回测过程中发生错误: {e}\n{traceback.format_exc()}"
            self.after(0, lambda: self.update_backtest_results(f"✗ {error_msg}\n", overwrite=False))
        finally:
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
            self.after(0, lambda: self.quick_backtest_btn.config(state="normal"))

    def update_data_requirement_hint(self):
        """更新数据需求提示"""
        try:
            # 检查回测标签页的参数
            if hasattr(self, 'backtest_symbol_var') and hasattr(self, 'backtest_tf_var'):
                symbol = self.backtest_symbol_var.get()
                timeframe = self.backtest_tf_var.get()
                start_date = self.backtest_start_var.get() if hasattr(self, 'backtest_start_var') else ""
                end_date = self.backtest_end_var.get() if hasattr(self, 'backtest_end_var') else ""
                
                if symbol and timeframe and start_date and end_date and symbol != '(无可用策略)':
                    # 检查数据是否存在
                    data_exists = self.check_backtest_data(symbol, timeframe, start_date, end_date)
                    
                    if data_exists:
                        self.data_requirement_label.config(text="✓ 回测数据已就绪", foreground="green")
                    else:
                        self.data_requirement_label.config(text="⚠ 回测数据缺失，需要同步", foreground="red")
                else:
                    self.data_requirement_label.config(text="", foreground="blue")
        except Exception as e:
            self.data_requirement_label.config(text="", foreground="blue")

    def set_quick_date_range(self, days, start_var, end_var):
        """为指定的变量设置快速日期范围"""
        from datetime import datetime, timedelta
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        start_var.set(start_date.strftime("%Y-%m-%d"))
        end_var.set(end_date.strftime("%Y-%m-%d"))

    def set_backtest_quick_date_range(self, days):
        """为回测设置快速日期范围"""
        from datetime import datetime, timedelta
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        self.backtest_start_var.set(start_date.strftime("%Y-%m-%d"))
        self.backtest_end_var.set(end_date.strftime("%Y-%m-%d"))

    def set_data_quick_date_range(self, days):
        """为数据中心设置快速日期范围"""
        from datetime import datetime, timedelta
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        self.data_start_date_var.set(start_date.strftime("%Y-%m-%d"))
        self.data_end_date_var.set(end_date.strftime("%Y-%m-%d"))

    def check_data_sync_config(self):
        """检查数据同步配置状态"""
        try:
            master1_config = self.app_config['master1']
            if all(master1_config.get(k) for k in ['path', 'login', 'password', 'server']):
                self.data_sync_config_label.config(text="✓ 主账户1配置完整", foreground="green")
                return True
            else:
                self.data_sync_config_label.config(text="✗ 主账户1配置不完整", foreground="red")
                return False
        except KeyError:
            self.data_sync_config_label.config(text="✗ 未找到主账户1配置", foreground="red")
            return False

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
                                'module_name': module_name,
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

    # --- *** 新增方法：打开回测窗口 *** ---
    def open_backtest_window(self):
        selection = self.strategy_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先从左侧列表中选择一个策略进行回测。")
            return
        
        strategy_name = self.strategy_listbox.get(selection[0])
        strategy_info = self.available_strategies.get(strategy_name)
        if not strategy_info:
            messagebox.showerror("错误", f"找不到策略 '{strategy_name}' 的信息。")
            return
            
        # 创建并显示回测窗口
        BacktestWindow(self, self, strategy_name, strategy_info)
    # --- *** 结束新增方法 *** ---

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
        
        # 处理数据同步日志
        try:
            while True: 
                message = data_log_queue.get_nowait()
                self.log_message(message)
                # 更新数据同步状态
                if "[DataManager]" in message:
                    self.data_sync_progress_var.set(message)
                    # 解析进度消息并更新进度条
                    self._parse_and_update_progress(message)
        except Empty: pass
        
        self.after(250, self.update_log)

    def _parse_and_update_progress(self, message):
        """解析数据同步进度消息并更新进度条"""
        try:
            # 解析 "已下载 X/Y" 格式的消息
            if "已下载" in message and "/" in message:
                import re
                # 使用正则表达式提取数字
                match = re.search(r'已下载\s+(\d+)/(\d+)', message)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    
                    if total > 0:
                        percentage = (current / total) * 100
                        # 更新数据中心进度条
                        self.data_sync_progress_bar['value'] = percentage
                        self.data_sync_progress_label.config(text=f"{percentage:.1f}%")
                        
                        # 如果回测正在进行数据下载，也更新回测进度条
                        if hasattr(self, 'backtest_progress') and self.backtest_status_var.get() == "数据下载中...":
                            self.backtest_progress['value'] = percentage
                            self.backtest_progress_label.config(text=f"{percentage:.1f}%")
                    else:
                        self.data_sync_progress_bar['value'] = 0
                        self.data_sync_progress_label.config(text="0%")
                        
            # 处理开始和完成状态
            elif "开始数据同步任务" in message:
                self.data_sync_progress_bar['value'] = 0
                self.data_sync_progress_label.config(text="0%")
                # 如果回测正在进行数据下载，也重置回测进度条
                if hasattr(self, 'backtest_progress') and self.backtest_status_var.get() == "数据下载中...":
                    self.backtest_progress['value'] = 0
                    self.backtest_progress_label.config(text="0%")
            elif "所有数据同步任务完成" in message:
                self.data_sync_progress_bar['value'] = 100
                self.data_sync_progress_label.config(text="100%")
                # 如果回测正在进行数据下载，也更新回测进度条
                if hasattr(self, 'backtest_progress') and self.backtest_status_var.get() == "数据下载中...":
                    self.backtest_progress['value'] = 100
                    self.backtest_progress_label.config(text="100%")
            elif "数据同步任务失败" in message or "错误" in message:
                self.data_sync_progress_bar['value'] = 0
                self.data_sync_progress_label.config(text="失败")
                # 如果回测正在进行数据下载，也更新回测进度条
                if hasattr(self, 'backtest_progress') and self.backtest_status_var.get() == "数据下载中...":
                    self.backtest_progress['value'] = 0
                    self.backtest_progress_label.config(text="失败")
                
        except Exception as e:
            # 如果解析失败，不影响其他功能
            pass

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
                  'slave': {'balance': 0, 'equity': 0, 'profit': 0, 'margin_free': 0, 'total_positions': 0}}
        totals['master']['total_positions'] = 0
        
        for i, acc_vars in enumerate(self.master_vars_list + self.slave_vars_list):
            if acc_vars['account_id'] in self.logged_in_accounts:
                try:
                    acc_type = 'master' if i < NUM_MASTERS else 'slave'
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
        
        summary_points = ['summary_balance_var', 'summary_equity_var', 'summary_margin_free_var', 'summary_margin_level_var', 'summary_profit_var', 'summary_total_positions_var']
        for name in summary_points:
            if name in vars_dict:
                vars_dict[name].set("--")
        if 'summary_profit_widget' in vars_dict:
            vars_dict['summary_profit_widget'].config(foreground='black')

    def _start_worker_thread(self):
        self.worker = threading.Thread(target=self.worker_thread, daemon=True)
        self.worker.start()
        
        # 启动数据同步处理线程
        self.data_sync_worker = threading.Thread(target=self.data_sync_worker_thread, daemon=True)
        self.data_sync_worker.start()

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

    def start_data_sync(self):
        """开始数据同步"""
        try:
            # 获取选中的交易品种
            selected_symbols = [symbol for symbol, var in self.data_symbol_vars.items() if var.get()]
            if not selected_symbols:
                messagebox.showwarning("警告", "请至少选择一个交易品种")
                return
            
            # 获取选中的时间周期
            selected_timeframes = [tf for tf, var in self.data_timeframe_vars.items() if var.get()]
            if not selected_timeframes:
                messagebox.showwarning("警告", "请至少选择一个时间周期")
                return
            
            # 获取日期范围
            start_date_str = self.data_start_date_var.get()
            end_date_str = self.data_end_date_var.get()
            
            if not start_date_str or not end_date_str:
                messagebox.showwarning("警告", "请设置开始和结束日期")
                return
            
            # 验证日期格式
            try:
                from datetime import datetime
                datetime.strptime(start_date_str, "%Y-%m-%d")
                datetime.strptime(end_date_str, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("错误", "日期格式不正确，请使用 YYYY-MM-DD 格式")
                return
            
            # 检查配置状态
            if not self.check_data_sync_config():
                messagebox.showerror("错误", "主账户1配置不完整，请先配置主账户1后再进行数据同步")
                return
            
            # 禁用按钮，启用停止按钮
            self.start_sync_button.config(state="disabled")
            self.stop_sync_button.config(state="normal")
            self.data_sync_in_progress = True
            self.data_sync_progress_var.set("正在准备同步...")
            
            # 重置进度条
            self.data_sync_progress_bar['value'] = 0
            self.data_sync_progress_label.config(text="0%")
            
            # 创建同步任务
            sync_task = {
                'symbols': selected_symbols,
                'timeframes': selected_timeframes,
                'start_date': start_date_str,
                'end_date': end_date_str
            }
            
            # 将任务放入队列
            data_task_queue.put(sync_task)
            
            self.log_message(f"开始同步数据: {selected_symbols} {selected_timeframes} ({start_date_str} 到 {end_date_str})")
            
        except Exception as e:
            self.log_message(f"启动数据同步时出错: {e}")
            self.start_sync_button.config(state="normal")
            self.stop_sync_button.config(state="disabled")
            self.data_sync_in_progress = False

    def stop_data_sync(self):
        """停止数据同步"""
        try:
            self.data_sync_in_progress = False
            self.start_sync_button.config(state="normal")
            self.stop_sync_button.config(state="disabled")
            self.data_sync_progress_var.set("已停止")
            self.log_message("数据同步已停止")
        except Exception as e:
            self.log_message(f"停止数据同步时出错: {e}")

    def set_quick_date_range(self, days):
        """设置快速日期范围"""
        try:
            from datetime import datetime, timedelta
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            self.data_start_date_var.set(start_date.strftime("%Y-%m-%d"))
            self.data_end_date_var.set(end_date.strftime("%Y-%m-%d"))
        except Exception as e:
            self.log_message(f"设置日期范围时出错: {e}")

    def refresh_data_list(self):
        """刷新数据列表"""
        try:
            if hasattr(self, 'data_tree'):
                # 清空现有数据
                for item in self.data_tree.get_children():
                    self.data_tree.delete(item)
                
                # 获取本地数据列表
                data_list = self.data_manager.get_local_data_list()
                
                # 填充数据到Treeview
                for data_info in data_list:
                    self.data_tree.insert('', 'end', values=(
                        data_info['symbol'],
                        data_info['timeframe'],
                        data_info['count'],
                        data_info['start_date'],
                        data_info['end_date']
                    ))
                
                if hasattr(self, 'log_text'):
                    self.log_message(f"已刷新数据列表，共 {len(data_list)} 个数据集")
        except Exception as e:
            if hasattr(self, 'log_text'):
                self.log_message(f"刷新数据列表时出错: {e}")

    def auto_sync_backtest_data(self):
        """一键同步回测数据"""
        try:
            # 检查回测参数是否已设置
            if not hasattr(self, 'backtest_symbol_var') or not hasattr(self, 'backtest_tf_var'):
                messagebox.showwarning("警告", "请先在策略回测标签页设置回测参数")
                return
            
            symbol = self.backtest_symbol_var.get()
            timeframe = self.backtest_tf_var.get()
            start_date = self.backtest_start_var.get() if hasattr(self, 'backtest_start_var') else ""
            end_date = self.backtest_end_var.get() if hasattr(self, 'backtest_end_var') else ""
            
            if not symbol or not timeframe or not start_date or end_date or symbol == '(无可用策略)':
                messagebox.showwarning("警告", "请先在策略回测标签页设置完整的回测参数")
                return
            
            # 检查数据是否已存在
            if self.check_backtest_data(symbol, timeframe, start_date, end_date):
                messagebox.showinfo("提示", f"回测所需的数据 {symbol} {timeframe} 已存在，无需同步。")
                return
            
            # 自动填充参数
            if symbol in self.data_symbol_vars:
                self.data_symbol_vars[symbol].set(True)
            if timeframe in self.data_timeframe_vars:
                self.data_timeframe_vars[timeframe].set(True)
            self.data_start_date_var.set(start_date)
            self.data_end_date_var.set(end_date)
            
            # 开始同步
            self.start_data_sync()
            messagebox.showinfo("提示", f"已自动开始同步 {symbol} {timeframe} 的数据，请等待同步完成。")
            
        except Exception as e:
            messagebox.showerror("错误", f"一键同步失败: {e}")

    def check_backtest_data(self, symbol, timeframe_str, start_date_str, end_date_str):
        """检查回测数据是否存在"""
        try:
            data = self.data_manager.get_data(symbol, timeframe_str, start_date_str, end_date_str)
            return data is not None and not data.empty
        except Exception as e:
            self.log_message(f"检查数据时出错: {e}")
            return False

    def switch_to_data_center_tab(self, symbol, timeframe_str, start_date_str, end_date_str):
        """切换到数据中心标签页并预填参数"""
        try:
            # 找到数据中心标签页
            for i in range(self.notebook.index("end")):
                tab_text = self.notebook.tab(i, "text")
                if "数据中心" in tab_text:
                    self.notebook.select(i)
                    break
            
            # 预填参数
            if hasattr(self, 'data_symbol_vars') and symbol in self.data_symbol_vars:
                self.data_symbol_vars[symbol].set(True)
            if hasattr(self, 'data_timeframe_vars') and timeframe_str in self.data_timeframe_vars:
                self.data_timeframe_vars[timeframe_str].set(True)
            if hasattr(self, 'data_start_date_var'):
                self.data_start_date_var.set(start_date_str)
            if hasattr(self, 'data_end_date_var'):
                self.data_end_date_var.set(end_date_str)
            
            self.log_message(f"已跳转到数据中心，请点击'开始同步数据'按钮")
            
        except Exception as e:
            self.log_message(f"切换标签页时出错: {e}")

    def _auto_sync_data_for_backtest(self, symbol, timeframe_str, start_date_str, end_date_str, strategy_name, initial_cash, leverage):
        """自动同步回测所需的数据"""
        try:
            self.after(0, lambda: self.update_backtest_results("正在连接MT5服务器...\n", overwrite=False))
            
            # 创建数据同步任务
            sync_task = {
                'symbols': [symbol],
                'timeframes': [timeframe_str],
                'start_date': start_date_str,
                'end_date': end_date_str
            }
            
            # 将任务放入队列
            data_task_queue.put(sync_task)
            
            # 等待数据同步完成
            self.after(0, lambda: self.update_backtest_results("正在下载历史数据，请稍候...\n", overwrite=False))
            
            # 监控数据同步进度
            sync_start_time = time.time()
            max_wait_time = 300  # 最多等待5分钟
            
            while time.time() - sync_start_time < max_wait_time:
                # 检查数据是否已同步完成
                if self.check_backtest_data(symbol, timeframe_str, start_date_str, end_date_str):
                    self.after(0, lambda: self.update_backtest_results("数据同步完成，开始回测...\n", overwrite=False))
                    # 数据同步完成，启动回测
                    self.after(0, lambda: self._run_backtest_task(strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage))
                    return
                
                time.sleep(2)  # 每2秒检查一次
            
            # 超时处理
            self.after(0, lambda: self.update_backtest_results("数据同步超时，请检查网络连接或手动同步数据。\n", overwrite=False))
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))
            
        except Exception as e:
            error_msg = f"自动数据同步失败: {e}\n请手动在数据中心同步数据后重试。"
            self.after(0, lambda: self.update_backtest_results(error_msg, overwrite=False))
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))

    def _run_backtest_with_auto_sync(self, strategy_name, symbol, timeframe_str, start_date_str, end_date_str, initial_cash, leverage):
        """带自动数据同步的回测执行"""
        try:
            # 首先尝试获取数据
            full_data = self.data_manager.get_data(symbol, timeframe_str, start_date_str, end_date_str)
            
            if full_data is None or full_data.empty:
                # 数据不存在，自动同步
                self.after(0, lambda: self.update_backtest_results("数据不存在，正在自动同步...\n", overwrite=False))
                
                # 创建同步任务
                sync_task = {
                    'symbols': [symbol],
                    'timeframes': [timeframe_str],
                    'start_date': start_date_str,
                    'end_date': end_date_str
                }
                data_task_queue.put(sync_task)
                
                # 等待同步完成并重试
                time.sleep(5)  # 等待5秒让同步开始
                full_data = self.data_manager.get_data(symbol, timeframe_str, start_date_str, end_date_str)
                
                if full_data is None or full_data.empty:
                    error_msg = f"无法获取 {symbol} {timeframe_str} 数据，请检查网络连接或手动同步数据。"
                    self.after(0, lambda: self.update_backtest_results(error_msg, overwrite=True))
                    return
            
            # 数据获取成功，继续回测
            self.after(0, lambda: self.update_backtest_results(f"成功获取 {len(full_data)} 条K线数据，正在初始化回测引擎...\n", overwrite=False))
            
            # 获取策略信息
            if strategy_name not in self.available_strategies:
                self.after(0, lambda: self.update_backtest_results(f"错误: 找不到策略 {strategy_name}", overwrite=True))
                return
            
            strategy_info = self.available_strategies[strategy_name]
            
            # 获取策略的默认参数配置
            default_params = strategy_info.get('params_config', {})
            
            # 构建完整的参数，使用策略默认值
            params = {}
            for param_name, param_config in default_params.items():
                if param_name == 'symbol':
                    params[param_name] = symbol
                elif param_name == 'timeframe':
                    params[param_name] = timeframe_str
                else:
                    params[param_name] = param_config.get('default', 0)
            
            # 确保必要的参数存在
            if 'trade_volume' not in params:
                params['trade_volume'] = 0.01
            if 'magic_number' not in params:
                params['magic_number'] = 13579
            if 'stop_loss_pips' not in params:
                params['stop_loss_pips'] = 50
            if 'take_profit_pips' not in params:
                params['take_profit_pips'] = 100
            
            # 创建回测器
            backtester = Backtester(
                strategy_info=strategy_info,
                full_data=full_data,
                params=params,
                config=self.app_config,
                log_queue=log_queue,
                start_cash=float(initial_cash),
                leverage=int(leverage)
            )
            
            # 运行回测
            results = backtester.run()
            self.after(0, lambda: self.update_backtest_results(results, overwrite=True))
            
        except Exception as e:
            error_msg = f"回测过程中发生错误: {e}\n{traceback.format_exc()}"
            self.after(0, lambda: self.update_backtest_results(error_msg, overwrite=True))
        finally:
            self.after(0, lambda: self.start_backtest_btn.config(state="normal"))


if __name__ == "__main__":
    if not os.path.exists(STRATEGIES_DIR):
        os.makedirs(STRATEGIES_DIR)
    app = TradeCopierApp()
    app.mainloop()

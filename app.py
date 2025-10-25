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
from backtest_engine import EventDrivenBacktester
from data_manager import DataManager

from manual import MANUAL_TEXT
from strategy_guide import GUIDE_TEXT
from constants import NUM_SLAVES, NUM_MASTERS, STRATEGIES_DIR, CONFIG_FILE, APP_DATA_DIR
from core_utils import encrypt_password, decrypt_password
from strategy import Strategy
from live_gateway import LiveTradingGateway
from events import MarketEvent
from ui_utils import ScrolledFrame, ModifySLTPWindow, StrategyConfigWindow
from mt5_utils import _connect_mt5
from services.strategy_service import StrategyRunner

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

from services.core_service import (
    task_queue,
    log_queue,
    account_info_queue,
    data_task_queue,
    data_log_queue,
    backtest_result_queue,
    stop_event,
    mt5_lock
)


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
                self._log_to_report("错误：所有参数均不能为空。" )
                return

            mt5_config = self.app.app_config['master1']
            if not all(mt5_config.get(k) for k in ['path', 'login', 'password', 'server']):
                self._log_to_report("错误：主账户1 (Master 1) 配置不完整，无法用于下载数据。" )
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
                self._log_to_report(f"数据同步失败，请检查主APP日志。" )

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
                self._log_to_report("错误：初始资金必须是一个数字。" )
                return

            # 2. 从DataManager获取数据
            self._log_to_report(f"正在从本地数据库加载 {symbol} ({tf}) 数据...")
            data_manager = DataManager()
            data = data_manager.get_data(symbol, tf, start, end)

            if data is None or data.empty:
                self._log_to_report(f"错误：本地未找到所需数据。请先点击'下载数据'按钮。" )
                return

            self._log_to_report(f"成功加载 {len(data)} 条K线数据。" )

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
        
        # UI-specific state
        self.master_vars_list = []
        self.slave_vars_list = []
        self.default_vars = {}
        self.summary_vars = {}
        self.available_strategies = {}
        self.global_strategy_param_vars = {}
        self.positions_tab_slave_selector_var = tk.StringVar()
        self.positions_tab_master_selector_var = tk.StringVar()
        self.selected_strategy_in_library = None
        self.account_widgets_to_disable = {}
        self.backtest_strategy_params = {}
        self.equity_data = {} # Still needed for UI display

        # State that needs to be synced with CoreService
        self.logged_in_accounts = set()
        self.pending_verification_config = {}
        self.per_slave_mapping = {}
        self.verified_passwords = set()

        # Initialize services and managers
        self.data_manager = DataManager()
        # Pass initial state to the service
        self.core_service = CoreService(self.app_config, self.available_strategies, self.data_manager)

        # Data sync UI variables
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

    def _validate_lot_entry(self, P):
        """验证手数输入框，只允许输入有效的浮点数"""
        if P == "":
            return True
        try:
            float(P)
            return True
        except ValueError:
            return False

    def _sync_state_to_service():
        """Sends the current UI state to the core service."""
        state_payload = {
            'logged_in_accounts': self.logged_in_accounts,
            'pending_verification_config': self.pending_verification_config,
            'per_slave_mapping': self.per_slave_mapping,
            'verified_passwords': self.verified_passwords,
        }
        task_queue.put({'action': 'UPDATE_STATE', 'payload': state_payload})

    def _start_worker_thread(self):
        """Starts the core service background threads."""
        self.core_service.start()
        log_queue.put("UI请求启动核心服务...")

    def worker_thread(self):
        log_queue.put("后台引擎已启动，等待指令...")
        while not stop_event.is_set():
            try:
                try:
                    task = task_queue.get(block=False)
                    action = task.get('action')

                    if action == 'CLOSE_ALL_ACCOUNTS_FORCEFULLY':
                        with mt5_lock:
                            log_queue.put("指令收到：一键清仓所有账户。" )
                            all_accounts_to_clear = {}
                            for i in range(1, NUM_MASTERS + 1): all_accounts_to_clear[f'master{i}'] = self.app_config[f'master{i}']
                            for i in range(1, NUM_SLAVES + 1): all_accounts_to_clear[f'slave{i}'] = self.app_config[f'slave{i}']
                            for acc_id, config in all_accounts_to_clear.items():
                                self._stop_strategy_sync(acc_id)
                                self._close_all_trades_for_single_account(acc_id, config)
                    elif action == 'CLOSE_SINGLE_TRADE':
                        with mt5_lock:
                            account_id, ticket = task['account_id'], task['ticket']
                            log_queue.put(f"指令收到：平仓账户 {account_id} 的订单 {ticket}。" )
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
                            log_queue.put(f"指令收到：修改账户 {account_id} 订单 {ticket} 的SL/TP。" )
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
                            log_queue.put(f"账户 {account_id} 的策略已在运行中，请先停止。 " )
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

                if self.default_vars.get('enable_global_equity_stop') and self.default_vars['enable_global_equity_stop'].get():
                    try:
                        stop_level = float(self.default_vars['global_equity_stop_level'].get())
                        total_equity = sum(e for e in self.equity_data.values() if e is not None)
                        active_accounts_count = sum(1 for e in self.equity_data.values() if e is not None)
                        if active_accounts_count > 0 and total_equity < stop_level:
                                log_queue.put(f"!!! 全局风控触发 !!! 所有账户总净值 {total_equity:,.2f} 低于阈值 {stop_level:,.2f}。" )
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
            strategy = self.strategy_instances.pop(account_id)
            strategy.stop_strategy()
            strategy.join(timeout=5)

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

        with mt5_lock:
            # 1. 验证新配置
            for acc_id in list(self.pending_verification_config.keys()):
                if acc_id in logged_in_accounts_copy:
                    temp_config = self.pending_verification_config[acc_id]
                    log_queue.put(f"正在使用新配置验证账户 {acc_id}...")
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
                    _get_account_details(mt5_conn, acc_id, ping)
                    mt5_conn.shutdown()
                    account_info_queue.put({'id': acc_id, 'status': 'connected'})
                else:
                    self.connection_failures[acc_id] = self.connection_failures.get(acc_id, 0) + 1
                    if err_code == 1045:
                        self.connection_failures[acc_id] = MAX_CONN_FAILURES + 1

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
# --- app.py (修改后，精简版) ---
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext
from queue import Queue
from config.logging_config import setup_logging, QueueHandler
from services.core_service import CoreService
import logging
from typing import Dict, Optional

# (删除了所有业务逻辑的import，如 MetaTrader5, StrategyRunner 等)
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


class TradeCopierApp(ThemedTk):
    def __init__(self, root=None): # Modified to accept root=None for standalone
        if root is None:
            super().__init__()
            root = self
        else:
            # This path is not taken in normal execution but good for embedding
            super().__init__(root)

        self.root = root
        if use_themed_tk: self.set_theme("arc")
        self.root.title("MT5工具箱")
        self.root.geometry("1070x660")


        # 1. 初始化通信队列
        self.log_queue = Queue()
        self.task_queue = Queue()        # UI -> CoreService
        self.account_update_queue = Queue() # CoreService -> UI
        
        # 2. 设置日志
        self.logger = setup_logging(self.log_queue)

        # 3. 创建核心服务
        self.core_service = CoreService(
            self.log_queue, 
            self.task_queue, 
            self.account_update_queue
        )
        
        # 4. 启动核心服务 (唯一的后台线程)
        self.core_service.start()
        self.logger.info("应用启动，核心服务已创建。")

        # 5. 创建UI控件
        self.create_widgets()

        # 6. 启动UI队列轮询
        self.root.after(100, self.process_log_queue)
        self.root.after(100, self.process_account_update_queue)

        # 7. 设置关闭协议
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # 8. (注意！) 删除了所有状态变量
        # (为了简化，保留了部分UI状态变量)
        self.master_account_var = tk.StringVar(root)
        self.slave_accounts_vars: Dict[int, tk.BooleanVar] = {}
        self.strategy_param_entries: Dict[str, tk.Entry] = {}



    def create_widgets(self):
        # --- Main Layout ---
        main_paned_window = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        main_paned_window.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        top_pane = ttk.Frame(main_paned_window)
        main_paned_window.add(top_pane, weight=2)

        bottom_pane = ttk.Frame(main_paned_window)
        main_paned_window.add(bottom_pane, weight=1)

        top_paned_window = ttk.PanedWindow(top_pane, orient=tk.HORIZONTAL)
        top_paned_window.pack(fill=tk.BOTH, expand=True)
        
        accounts_frame = ttk.LabelFrame(top_paned_window, text="账户信息")
        top_paned_window.add(accounts_frame, weight=3)

        controls_frame = ttk.LabelFrame(top_paned_window, text="控制面板")
        top_paned_window.add(controls_frame, weight=2)

        # --- Accounts Treeview ---
        self.account_tree = ttk.Treeview(accounts_frame, columns=("ID", "Name", "Balance", "Equity", "Profit", "Status"), show='headings')
        for col in self.account_tree['columns']:
            self.account_tree.heading(col, text=col)
            self.account_tree.column(col, width=100)
        self.account_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.account_tree.bind("<Button-3>", self.show_account_context_menu)

        # Configure tags for master/slave visuals
        self.account_tree.tag_configure('master', background='#cce5ff') # Light Blue
        self.account_tree.tag_configure('slave', background='#d4edda')  # Light Green

        # --- Controls ---
        # Login
        login_frame = ttk.LabelFrame(controls_frame, text="登录/注销")
        login_frame.pack(fill=tk.X, padx=5, pady=5)
        
        ttk.Label(login_frame, text="账户ID:").grid(row=0, column=0, sticky=tk.W, padx=2, pady=2)
        self.account_id_entry = ttk.Entry(login_frame)
        self.account_id_entry.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        
        ttk.Label(login_frame, text="密码:").grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        self.password_entry = ttk.Entry(login_frame, show="*")
        self.password_entry.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(login_frame, text="服务器:").grid(row=2, column=0, sticky=tk.W, padx=2, pady=2)
        self.server_entry = ttk.Entry(login_frame)
        self.server_entry.grid(row=2, column=1, sticky=tk.EW, padx=2, pady=2)
        
        login_button = ttk.Button(login_frame, text="登录", command=self.handle_login)
        login_button.grid(row=3, column=0, pady=5, padx=2)
        
        logout_button = ttk.Button(login_frame, text="注销选中账户", command=self.handle_logout)
        logout_button.grid(row=3, column=1, pady=5, padx=2)

        # Copier
        copier_frame = ttk.LabelFrame(controls_frame, text="跟单设置")
        copier_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(copier_frame, text="手数乘数:").grid(row=0, column=0, padx=5, pady=2, sticky=tk.W)
        self.lots_entry = ttk.Entry(copier_frame)
        self.lots_entry.grid(row=0, column=1, padx=5, pady=2)
        self.lots_entry.insert(0, "1.0")
        self.reverse_var = tk.BooleanVar()
        ttk.Checkbutton(copier_frame, text="反向跟单", variable=self.reverse_var).grid(row=1, column=0, padx=5, pady=2)
        update_copier_button = ttk.Button(copier_frame, text="更新设置", command=self.handle_update_copier_settings)
        update_copier_button.grid(row=1, column=1, padx=5, pady=2)

        # Strategy
        strategy_frame = ttk.LabelFrame(controls_frame, text="策略控制")
        strategy_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        strategy_frame.columnconfigure(1, weight=1)

        ttk.Label(strategy_frame, text="选择策略:").grid(row=0, column=0, padx=5, pady=2, sticky=tk.W)
        self.strategy_combobox = ttk.Combobox(strategy_frame, state="readonly")
        self.strategy_combobox.grid(row=0, column=1, padx=5, pady=2, sticky=tk.EW)
        self.strategy_combobox.bind('<<ComboboxSelected>>', self.on_strategy_selected)

        self.strategy_params_frame = ttk.Frame(strategy_frame)
        self.strategy_params_frame.grid(row=1, column=0, columnspan=2, sticky=tk.EW, padx=5, pady=5)

        button_frame = ttk.Frame(strategy_frame)
        button_frame.grid(row=2, column=0, columnspan=2)

        start_strategy_button = ttk.Button(button_frame, text="启动策略", command=self.handle_start_strategy)
        start_strategy_button.pack(side=tk.LEFT, padx=5)
        
        stop_strategy_button = ttk.Button(button_frame, text="停止策略", command=self.handle_stop_strategy)
        stop_strategy_button.pack(side=tk.LEFT, padx=5)

        # --- Bottom Pane: Logs ---
        log_frame = ttk.LabelFrame(bottom_pane, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state="disabled", height=10)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def show_account_context_menu(self, event):
        """显示账户列表的右键菜单"""
        selection = self.account_tree.identify_row(event.y)
        if not selection:
            return

        self.account_tree.selection_set(selection) # 选中右键点击的行
        account_id = self._get_selected_account_id()
        if not account_id:
            return

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"设为 主账户 (Master)", command=self.set_as_master)
        menu.add_command(label=f"切换 从账户 (Slave) 状态", command=self.toggle_slave)
        
        menu.post(event.x_root, event.y_root)

    def set_as_master(self):
        account_id = self._get_selected_account_id()
        if account_id:
            self.task_queue.put({
                'action': 'SET_MASTER',
                'payload': {'account_id': account_id}
            })
            self.logger.info(f"已发送指令：设置账户 {account_id} 为主账户。")

    def toggle_slave(self):
        account_id = self._get_selected_account_id()
        if account_id:
            self.task_queue.put({
                'action': 'TOGGLE_SLAVE',
                'payload': {'account_id': account_id}
            })
            self.logger.info(f"已发送指令：切换账户 {account_id} 的从账户状态。")

    def _get_selected_account_id(self) -> Optional[int]:
        """辅助方法：从Treeview获取当前选中的账户ID"""
        try:
            selected_item = self.account_tree.selection()[0]
            account_id = int(self.account_tree.item(selected_item, 'values')[0])
            return account_id
        except IndexError:
            messagebox.showerror("错误", "请先在上方列表中选择一个账户。")
            return None
        except (ValueError, TypeError):
            messagebox.showerror("错误", "无效的账户ID。")
            return None

    def handle_login(self):
        """处理登录按钮点击"""
        try:
            account_id = int(self.account_id_entry.get())
            password = self.password_entry.get()
            server = self.server_entry.get()
            
            if not all([account_id, password, server]):
                messagebox.showerror("错误", "账户、密码和服务器均不能为空。")
                return

            # (关键) 只发送任务，不执行逻辑
            self.task_queue.put({
                'action': 'LOGIN',
                'payload': {
                    'account_id': account_id,
                    'password': password,
                    'server': server
                }
            })
        except ValueError:
            messagebox.showerror("错误", "账户ID必须是数字。")

    def handle_logout(self):
        """处理注销按钮点击"""
        account_id = self._get_selected_account_id()
        if account_id:
            # (关键) 只发送任务
            self.task_queue.put({
                'action': 'LOGOUT',
                'payload': {'account_id': account_id}
            })
            
    def handle_update_copier_settings(self):
        try:
            lots = float(self.lots_entry.get())
            reverse = self.reverse_var.get()
            self.task_queue.put({
                'action': 'UPDATE_COPIER_SETTINGS',
                'payload': {
                    'lots_multiplier': lots,
                    'reverse_copy': reverse
                }
            })
            self.logger.info("已发送跟单设置更新请求。")
        except ValueError:
            messagebox.showerror("错误", "手数必须是数字。")
            
    def on_strategy_selected(self, event=None):
        """当用户在下拉菜单中选择一个新策略时调用。"""
        strategy_name = self.strategy_combobox.get()
        if strategy_name:
            self.task_queue.put({
                'action': 'GET_STRATEGY_PARAMS',
                'payload': {'strategy_name': strategy_name}
            })
            
    def handle_start_strategy(self):
        account_id = self._get_selected_account_id()
        if not account_id:
            return
            
        strategy_name = self.strategy_combobox.get()
        if not strategy_name:
            messagebox.showerror("错误", "请选择一个策略。")
            return

        # 从动态生成的UI控件中收集参数
        strategy_params = {}
        for param_name, entry_widget in self.strategy_param_entries.items():
            strategy_params[param_name] = entry_widget.get()
        
        self.task_queue.put({
            'action': 'START_STRATEGY',
            'payload': {
                'account_id': account_id,
                'strategy_name': strategy_name,
                'strategy_params': strategy_params
            }
        })
        self.logger.info(f"发送启动策略 {strategy_name} 请求，参数: {strategy_params}")
        
    def handle_stop_strategy(self):
        # (这部分逻辑需要UI支持来选择要停止的策略)
        # 假设我们停止所选账户的所选策略
        account_id = self._get_selected_account_id()
        if not account_id:
            return
        strategy_name = self.strategy_combobox.get()
        if not strategy_name:
            messagebox.showerror("错误", "请选择一个策略。")
            return
        
        self.task_queue.put({
            'action': 'STOP_STRATEGY',
            'payload': {
                'account_id': account_id,
                'strategy_name': strategy_name
            }
        })

    # --- --- (B) UI 队列消费者 (只从队列收数据并更新UI) ---

    def process_log_queue(self):
        """从log_queue读取日志并更新到UI"""
        while not self.log_queue.empty():
            record = self.log_queue.get_nowait()
            log_entry = f"[{record.name}] [{record.levelname}] {record.getMessage()}\n"
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, log_entry)
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        self.root.after(100, self.process_log_queue)

    def process_account_update_queue(self):
        """从account_update_queue读取更新并更新UI"""
        while not self.account_update_queue.empty():
            update = self.account_update_queue.get_nowait()
            action = update.get('action')
            payload = update.get('payload', {})

            if action == 'LOGIN' or action == 'UPDATE':
                account_id = payload.get('account_id')
                if not account_id: continue
                
                account_id_str = str(account_id)
                details = payload.get('details', {})
                values = (
                    details.get('login', ''),
                    details.get('name', ''),
                    f"{details.get('balance', 0.0):.2f}",
                    f"{details.get('equity', 0.0):.2f}",
                    f"{details.get('profit', 0.0):.2f}",
                    "Connected"
                )
                if self.account_tree.exists(account_id_str):
                    self.account_tree.item(account_id_str, values=values)
                else:
                    self.account_tree.insert('', 'end', iid=account_id_str, values=values)

            elif action == 'LOGOUT':
                account_id = payload.get('account_id')
                if account_id and self.account_tree.exists(str(account_id)):
                    self.account_tree.delete(str(account_id))

            elif action == 'COPIER_STATUS_UPDATE':
                master_id = payload.get('master')
                slave_ids = payload.get('slaves', [])
                self.update_copier_visuals(master_id, slave_ids)

            elif action == 'STRATEGY_LIST_UPDATE':
                strategies = payload.get('strategies', [])
                self.strategy_combobox['values'] = strategies
                if strategies:
                    self.strategy_combobox.set(strategies[0])
                    self.on_strategy_selected() # 自动获取第一个策略的参数

            elif action == 'STRATEGY_PARAMS_UPDATE':
                self.update_strategy_params_ui(payload.get('params_config', {}))
                
        self.root.after(100, self.process_account_update_queue)

    def update_copier_visuals(self, master_id: Optional[int], slave_ids: list):
        """根据最新的主从状态更新Treeview的视觉样式。"""
        for item_id in self.account_tree.get_children():
            # Clear existing tags first
            self.account_tree.item(item_id, tags=())
            
            try:
                current_id = int(self.account_tree.item(item_id, 'values')[0])
                if current_id == master_id:
                    self.account_tree.item(item_id, tags=('master',))
                elif current_id in slave_ids:
                    self.account_tree.item(item_id, tags=('slave',))
            except (ValueError, IndexError):
                continue # Skip if row is malformed

    def update_strategy_params_ui(self, params_config: dict):
        """根据策略参数配置动态创建UI控件。"""
        # 1. 清空旧的控件
        for widget in self.strategy_params_frame.winfo_children():
            widget.destroy()
        self.strategy_param_entries.clear()

        # 2. 创建新的控件
        row = 0
        for name, config in params_config.items():
            label_text = config.get('label', name)
            default_value = config.get('default', '')

            lbl = ttk.Label(self.strategy_params_frame, text=f"{label_text}:")
            lbl.grid(row=row, column=0, sticky=tk.W, padx=2, pady=2)

            entry = ttk.Entry(self.strategy_params_frame)
            entry.grid(row=row, column=1, sticky=tk.EW, padx=2, pady=2)
            entry.insert(0, str(default_value))
            
            self.strategy_param_entries[name] = entry
            row += 1
        self.strategy_params_frame.columnconfigure(1, weight=1)

    # --- --- (C) 应用程序生命周期 ---

    def on_closing(self):
        """(关键) 简化后的关闭逻辑"""
        if messagebox.askokcancel("退出", "你确定要退出吗?"):
            self.logger.info("应用程序正在关闭...")
            # 1. (关键) 只需告诉CoreService停止
            # CoreService的stop()方法会负责协调所有子服务的关闭
            self.core_service.stop()
            
            # 2. 销毁UI
            self.root.destroy()
            
    # --- --- (D) (注意！) 所有旧的业务逻辑方法已全部删除 ---

if __name__ == '__main__':
    app = TradeCopierApp()
    app.mainloop()

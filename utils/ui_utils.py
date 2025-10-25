import tkinter as tk
from tkinter import ttk, messagebox

# --- 滚动框架辅助类 ---
class ScrolledFrame(ttk.Frame):
    """一个带垂直滚动条的Tkinter框架，用于容纳动态内容"""
    def __init__(self, parent, *args, **kw):
        super().__init__(parent, *args, **kw)
        
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(self, bd=0, highlightthickness=0, yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar.config(command=self.canvas.yview)

        self.interior = ttk.Frame(self.canvas)
        self.interior_id = self.canvas.create_window(0, 0, window=self.interior, anchor=tk.NW)

        self.interior.bind('<Configure>', self._on_interior_configure)
        self.canvas.bind('<Configure>', self._on_canvas_configure)

    def _on_interior_configure(self, event):
        self.canvas.config(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        if self.interior.winfo_reqwidth() != self.canvas.winfo_width():
            self.canvas.itemconfigure(self.interior_id, width=self.canvas.winfo_width())

# --- 修改SL/TP弹出窗口 ---
class ModifySLTPWindow(tk.Toplevel):
    def __init__(self, parent, task_queue, account_id, ticket, symbol, current_sl, current_tp):
        super().__init__(parent)
        self.transient(parent)
        self.grab_set()
        self.title(f"修改止损止盈 (订单: {ticket})")
        
        self.task_queue = task_queue
        self.account_id = account_id
        self.ticket = ticket

        main_frame = ttk.Frame(self, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(main_frame, text=f"品种: {symbol}").pack(pady=5)
        
        sl_frame = ttk.Frame(main_frame)
        sl_frame.pack(fill=tk.X, pady=5)
        ttk.Label(sl_frame, text="新止损 (SL):", width=15).pack(side=tk.LEFT)
        self.sl_var = tk.StringVar(value=f"{current_sl:.5f}")
        ttk.Entry(sl_frame, textvariable=self.sl_var).pack(side=tk.LEFT, expand=True, fill=tk.X)

        tp_frame = ttk.Frame(main_frame)
        tp_frame.pack(fill=tk.X, pady=5)
        ttk.Label(tp_frame, text="新止盈 (TP):", width=15).pack(side=tk.LEFT)
        self.tp_var = tk.StringVar(value=f"{current_tp:.5f}")
        ttk.Entry(tp_frame, textvariable=self.tp_var).pack(side=tk.LEFT, expand=True, fill=tk.X)

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        ttk.Button(btn_frame, text="确认修改", command=self.save).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=10)
        
        self.wait_window(self)

    def save(self):
        new_sl = float(self.sl_var.get())
        new_tp = float(self.tp_var.get())
        self.task_queue.put({'action': 'MODIFY_SLTP', 'account_id': self.account_id, 'ticket': self.ticket, 'sl': new_sl, 'tp': new_tp})
        self.destroy()

# --- 策略配置弹出窗口 ---
class StrategyConfigWindow(tk.Toplevel):
    def __init__(self, parent, app_config, log_queue, account_id, strategy_name, params_config):
        super().__init__(parent)
        self.transient(parent)
        self.grab_set()
        self.title(f"配置策略: {strategy_name} ({account_id})")
        self.geometry("450x400")

        self.app_config = app_config
        self.log_queue = log_queue
        self.account_id = account_id
        self.strategy_name = strategy_name
        self.params_config = params_config
        self.param_vars = {}

        self.config_section_name = f"{account_id}_{strategy_name}"
        if not self.app_config.has_section(self.config_section_name):
            self.app_config.add_section(self.config_section_name)

        main_frame = ttk.Frame(self, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        params_scrolled_frame = ScrolledFrame(main_frame)
        params_scrolled_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))
        params_frame = params_scrolled_frame.interior
        params_frame.columnconfigure(1, weight=1)

        self.create_param_widgets(params_frame)
        self.create_buttons(main_frame)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.wait_window(self)

    def create_param_widgets(self, parent):
        row = 0
        for param_key, config in self.params_config.items():
            label_text = config.get('label', param_key)
            default_val = config.get('default', '')
            
            global_section = f"{self.strategy_name}_Global"
            global_val = self.app_config.get(global_section, param_key, fallback=str(default_val))
            saved_val = self.app_config.get(self.config_section_name, param_key, fallback=global_val)

            param_type = config.get('type', 'str')

            ttk.Label(parent, text=f"{label_text}:").grid(row=row, column=0, sticky='w', padx=5, pady=8)
            
            if param_type == 'bool':
                var = tk.BooleanVar(value=str(saved_val).lower() in ('true', '1', 'yes'))
                widget = ttk.Checkbutton(parent, variable=var)
                widget.grid(row=row, column=1, sticky='w', padx=5, pady=8)
            else: # 默认处理 str, int, float
                var = tk.StringVar(value=saved_val)
                widget = ttk.Entry(parent, textvariable=var)
                widget.grid(row=row, column=1, sticky='ew', padx=5, pady=8)

            self.param_vars[param_key] = var
            row += 1

    def create_buttons(self, parent):
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btn_frame, text="保存", command=self.save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.cancel).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="恢复默认值", command=self.restore_defaults).pack(side=tk.LEFT, padx=5)

    def save(self):
        for key, var in self.param_vars.items():
            self.app_config.set(self.config_section_name, key, str(var.get()))
        self.log_queue.put(f"已保存账户 {self.account_id} 的策略 '{self.strategy_name}' 特定参数。")
        self.destroy()

    def cancel(self):
        self.destroy()

    def restore_defaults(self):
        """恢复所有参数到策略文件定义的默认值"""
        if not messagebox.askyesno("确认", "确定要将所有参数恢复为默认值吗？\n此操作不会立即保存。"):
            return
            
        for param_key, config in self.params_config.items():
            if param_key in self.param_vars:
                default_val = config.get('default', '')
                self.param_vars[param_key].set(default_val)
        self.log_queue.put(f"已在界面上恢复 {self.strategy_name} 的默认参数。")
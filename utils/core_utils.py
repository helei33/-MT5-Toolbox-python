import threading
import os
import MetaTrader5 as mt5
from cryptography.fernet import Fernet
from constants import KEY_FILE

# --- 策略基类定义 ---
class BaseStrategy(threading.Thread):
    """
    所有策略插件的基类。
    主程序会自动将这个类注入到每个策略模块中。
    它提供了完整的MT5连接管理、高级交易API和事件驱动的执行模型。
    """
    strategy_name = "Base Strategy"
    strategy_params_config = {}
    strategy_description = "这是一个基础策略模板，没有具体功能。"

    def __init__(self, config, log_queue, params):
        super().__init__(daemon=True)
        # 基础组件
        self.config = config
        self.log_queue = log_queue
        self._stop_event = threading.Event()
        
        # MT5连接实例
        self.mt5 = mt5
        self.connected = False

        # 自动类型转换后的参数
        self.params = self._parse_params(params)

    def _parse_params(self, raw_params):
        """根据 strategy_params_config 自动转换参数类型"""
        parsed = {}
        for key, config in self.strategy_params_config.items():
            raw_val = raw_params.get(key)
            if raw_val is None:
                parsed[key] = config.get('default')
                continue

            param_type = config.get('type', 'str')
            try:
                if param_type == 'int':
                    parsed[key] = int(raw_val)
                elif param_type == 'float':
                    parsed[key] = float(raw_val)
                elif param_type == 'bool':
                    parsed[key] = str(raw_val).lower() in ('true', '1', 'yes')
                else:
                    parsed[key] = str(raw_val)
            except (ValueError, TypeError):
                self.log(f"警告：参数 '{key}' 值 '{raw_val}' 无法转换为 '{param_type}' 类型，将使用默认值。")
                parsed[key] = config.get('default')
        return parsed

    def _connect(self):
        """内部连接方法"""
        if not self.mt5.initialize(
            path=self.config['path'],
            login=int(self.config['login']),
            password=self.config['password'],
            server=self.config['server'],
            timeout=10000
        ):
            self.log(f"连接失败: {self.mt5.last_error()}")
            self.connected = False
            return False
        self.log("MT5连接成功。")
        self.connected = True
        return True

    def run(self):
        """策略主循环，管理连接和事件调用"""
        if not self._connect():
            self.log("初始化连接失败，策略退出。")
            return

        # 调用策略初始化钩子
        if not self.on_init():
            self.log("on_init() 返回 False，策略终止。")
            self.mt5.shutdown()
            return

        while self.is_running():
            # 检查连接是否仍然有效
            if not self.mt5.terminal_info():
                self.log("MT5连接丢失，尝试重连...")
                self.connected = False
                if not self._connect():
                    self.log("重连失败，等待下次尝试...")
                    threading.Event().wait(5) # 等待5秒
                    continue
            
            # 调用on_tick钩子
            self.on_tick()
            
            # 控制循环频率，避免CPU占用过高
            # 子类可以通过设置 self.tick_interval 来调整
            threading.Event().wait(getattr(self, 'tick_interval', 1.0))

        # 调用策略退出钩子
        self.on_deinit()
        self.mt5.shutdown()
        self.log("策略已安全停止，连接已关闭。")

    # --- 策略开发者需要实现的钩子方法 ---
    def on_init(self):
        """策略初始化时调用。如果返回False，策略将不会启动。"""
        self.log("策略正在初始化...")
        return True

    def on_tick(self):
        """策略主逻辑，由run()方法循环调用。"""
        # 开发者在此实现每个tick的逻辑
        # self.log("On Tick...")
        pass

    def on_deinit(self):
        """策略停止时调用，用于清理资源。"""
        self.log("策略正在反初始化...")

    # --- 高级API和实用工具 ---
    def log(self, message):
        """向主程序日志队列发送带策略名称前缀的消息"""
        self.log_queue.put(f"[{self.strategy_name}@{self.config['account_id']}] {message}")

    def close_position(self, ticket, volume, symbol, order_type, comment=""):
        """
        便捷的平仓方法。
        :param ticket: int, 要平仓的持仓订单号。
        :param volume: float, 要平仓的手数。
        :param symbol: str, 交易品种。
        :param order_type: int, 原始订单类型 (ORDER_TYPE_BUY 或 ORDER_TYPE_SELL)。
        :param comment: str, 平仓备注。
        """
        if not self.connected:
            self.log(f"平仓失败 (Ticket: {ticket}): 未连接到MT5。")
            return None

        close_order_type = self.mt5.ORDER_TYPE_SELL if order_type == self.mt5.ORDER_TYPE_BUY else self.mt5.ORDER_TYPE_BUY
        price = self.mt5.symbol_info_tick(symbol).bid if order_type == self.mt5.ORDER_TYPE_BUY else self.mt5.symbol_info_tick(symbol).ask

        request = {
            "action": self.mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": symbol,
            "volume": float(volume), "type": close_order_type, "price": price,
            "deviation": 20, "magic": self.params.get('magic_number', 0), "comment": comment,
            "type_filling": self.mt5.ORDER_FILLING_IOC, "type_time": self.mt5.ORDER_TIME_GTC,
        }
        return self.mt5.order_send(request)

    def get_positions(self, symbol=None):
        """
        获取当前账户的持仓。
        :param symbol: str, 可选。如果提供，则只返回指定品种的持仓。
        :return: tuple of Position objects, or an empty tuple if none.
        """
        if not self.connected:
            self.log("获取持仓失败：未连接到MT5。")
            return ()
        
        positions = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return positions if positions else ()

    def buy(self, symbol, volume, sl=0.0, tp=0.0, magic=0, comment=""):
        """便捷的市价买入方法"""
        return self._trade_request(symbol, volume, self.mt5.ORDER_TYPE_BUY, sl, tp, magic, comment)

    def sell(self, symbol, volume, sl=0.0, tp=0.0, magic=0, comment=""):
        """便捷的市价卖出方法"""
        return self._trade_request(symbol, volume, self.mt5.ORDER_TYPE_SELL, sl, tp, magic, comment)

    def _trade_request(self, symbol, volume, order_type, sl, tp, magic, comment):
        if not self.connected:
            self.log("交易失败：未连接到MT5。")
            return None
        
        price = self.mt5.symbol_info_tick(symbol).ask if order_type == self.mt5.ORDER_TYPE_BUY else self.mt5.symbol_info_tick(symbol).bid
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": int(magic),
            "comment": comment,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
            "type_time": self.mt5.ORDER_TIME_GTC,
        }
        result = self.mt5.order_send(request)
        if result.retcode != self.mt5.TRADE_RETCODE_DONE:
            self.log(f"订单发送失败: {result.comment} (代码: {result.retcode})")
        return result

    def stop_strategy(self): self._stop_event.set()
    def is_running(self): return not self._stop_event.is_set()

# --- 密码加解密函数 ---
def _load_key():
    """加载密钥，如果不存在则创建。"""
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f: return f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f: f.write(key)
        return key

cipher_suite = Fernet(_load_key())

def encrypt_password(text: str) -> str:
    if not text: return ""
    try: return cipher_suite.encrypt(text.encode('utf-8')).decode('utf-8')
    except Exception: return text

def decrypt_password(encrypted_text: str) -> str:
    if not encrypted_text: return ""
    try: return cipher_suite.decrypt(encrypted_text.encode('utf-8')).decode('utf-8')
    except Exception: return encrypted_text
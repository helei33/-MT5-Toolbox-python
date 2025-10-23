import threading
import os
from cryptography.fernet import Fernet
from constants import KEY_FILE

# --- 策略基类定义 ---
class BaseStrategy(threading.Thread):
    """
    所有策略插件的基类。
    主程序会自动将这个类注入到每个策略模块中。
    """
    strategy_name = "Base Strategy"
    strategy_params_config = {}
    strategy_description = "这是一个基础策略模板，没有具体功能。"

    def __init__(self, config, log_queue, params):
        super().__init__(daemon=True)
        self.config = config
        self.log_queue = log_queue
        self.params = params
        self._stop_event = threading.Event()

    def run(self): pass # 子类需要实现这个方法
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
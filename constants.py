import os
import sys

NUM_SLAVES = 3  # 支持的从属账户数量
NUM_MASTERS = 3 # 支持的主账户数量

def get_correct_path(relative_path):
    """获取文件/文件夹的正确路径，兼容打包后的情况"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

def get_app_data_dir():
    """获取跨平台的用户应用数据目录"""
    app_name = "MT5Toolbox"
    if sys.platform == "win32": return os.path.join(os.environ["APPDATA"], app_name)
    elif sys.platform == "darwin": return os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name)
    else: return os.path.join(os.path.expanduser("~"), ".config", app_name)

APP_DATA_DIR = get_app_data_dir()
STRATEGIES_DIR = get_correct_path('strategies')
CONFIG_FILE = os.path.join(APP_DATA_DIR, 'config.ini')
KEY_FILE = os.path.join(APP_DATA_DIR, 'secret.key')
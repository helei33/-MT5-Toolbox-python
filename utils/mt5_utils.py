# mt5_utils.py
import MetaTrader5 as mt5
import time

def _connect_mt5(config, log_queue, account_id_str, account_info_queue=None):
    """
    (公共函数) 辅助函数：连接到单个MT5账户。
    返回 (ping, mt5_instance, error_code)。
    此版本现在可以接受一个可选的 account_info_queue 来发送UI状态更新。
    """
    # 检查基本配置是否存在
    if not all(config.get(k) for k in ['path', 'login', 'password', 'server']):
        acc_id = config.get('account_id', '未知账户')
        log_queue.put(f"[{acc_id}] 连接失败: 配置不完整 (缺少 path, login, password, 或 server)")
        if account_info_queue:
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
        if account_info_queue:
            account_info_queue.put({'id': config['account_id'], 'status': 'error', 'ping': -1})
        return None, None, error_code
    
    # 连接成功
    return ping, mt5, mt5.RES_S_OK
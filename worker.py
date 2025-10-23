import threading
import time
from queue import Empty
import MetaTrader5 as mt5

import MetaTrader5 as mt5


class Worker(threading.Thread):
    def __init__(self, app, task_queue, log_queue, account_info_queue, stop_event):
        super().__init__(daemon=True)
        self.app = app
        self.task_queue = task_queue
        self.log_queue = log_queue
        self.account_info_queue = account_info_queue
        self.stop_event = stop_event
        self.connection_failures = {}

    def run(self):
        """后台工作线程的主循环。"""
        self.log_queue.put("后台引擎已启动，等待指令...")
        while not self.stop_event.is_set():
            try:
                # 1. 处理任务队列中的指令
                try:
                    task = self.task_queue.get(block=False)
                    self._process_task(task)
                except Empty:
                    pass

                # 2. 处理已登录的账户（跟单、策略、监控）
                self._process_logged_in_accounts()

            except Exception as e:
                self.log_queue.put(f"后台线程主循环发生严重错误: {e}")
            finally:
                # 使用主程序配置的检查间隔
                time.sleep(float(self.app.app_config.get('DEFAULT', 'check_interval', fallback='0.2')))

    def _process_task(self, task):
        """处理来自UI的单个任务。"""
        action = task.get('action')

        if action == 'LOGIN':
            account_id = task['account_id']
            self.app.logged_in_accounts.add(account_id)
            self.app._set_login_state_ui(account_id, True)

        elif action == 'LOGOUT':
            account_id = task['account_id']
            if 'slave' in account_id: self.app.toggle_slave_enabled(account_id, False, from_ui=False)
            if account_id in self.app.strategy_instances: self.task_queue.put({'action': 'STOP_STRATEGY', 'account_id': account_id})
            self.app.logged_in_accounts.discard(account_id)
            self.app._set_logout_state_ui(account_id)
            self.account_info_queue.put({'id': account_id, 'status': 'logged_out'})

        elif action == 'CLOSE_ALL_ACCOUNTS_FORCEFULLY':
            self.log_queue.put("指令收到：一键清仓所有账户。")
            if not self.app.logged_in_accounts:
                self.log_queue.put("没有已登录的账户可供清仓。")
                return
            all_accounts_to_clear = {acc_id: self.app.app_config[acc_id] for acc_id in self.app.logged_in_accounts}
            for acc_id, config in all_accounts_to_clear.items():
                self._stop_strategy_sync(acc_id)
                self._close_all_trades_for_single_account(acc_id, config)

        elif action == 'CLOSE_SINGLE_TRADE':
            account_id, ticket = task['account_id'], task['ticket']
            self.log_queue.put(f"指令收到：平仓账户 {account_id} 的订单 {ticket}。")
            config_to_use = self.app.app_config[account_id]
            self._close_single_trade_for_account(account_id, config_to_use, int(ticket))

        elif action == 'STOP_AND_CLOSE':
            account_id = task['account_id']
            self._stop_strategy_sync(account_id)
            self._close_all_trades_for_single_account(account_id, self.app.app_config[account_id])

        elif action == 'MODIFY_SLTP':
            account_id, ticket, sl, tp = task['account_id'], task['ticket'], task['sl'], task['tp']
            self.log_queue.put(f"指令收到：修改账户 {account_id} 订单 {ticket} 的SL/TP。")
            config = self.app.app_config[account_id]
            _, mt5_conn, _ = self._connect_mt5(config, self.log_queue, f"修改SL/TP账户 {account_id}")
            if mt5_conn:
                try:
                    request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "order": ticket, "sl": sl, "tp": tp}
                    mt5_conn.order_send(request)
                finally:
                    mt5_conn.shutdown()

        elif action == 'START_STRATEGY':
            account_id, strategy_name, params = task['account_id'], task['strategy_name'], task.get('params', {})
            if account_id in self.app.strategy_instances:
                self.log_queue.put(f"账户 {account_id} 的策略已在运行中，请先停止。")
                return
            self.log_queue.put(f"正在为 {account_id} 初始化策略 '{strategy_name}'...")
            try:
                acc_config = self.app.app_config[account_id]
                acc_config['account_id'] = account_id # 确保 account_id 在配置中
                strategy_info = self.app.available_strategies.get(strategy_name)
                if not strategy_info:
                    self.log_queue.put(f"错误：找不到名为 '{strategy_name}' 的策略。")
                    return
                strategy = strategy_info['class'](acc_config, self.log_queue, params)
                strategy.start()
                self.app.strategy_instances[account_id] = strategy
                self.log_queue.put(f"账户 {account_id} 的策略 '{strategy_name}' 已成功启动。")
                self.account_info_queue.put({'id': account_id, 'status': 'strategy_running'})
            except Exception as e:
                self.log_queue.put(f"启动策略失败 ({account_id}): {e}")

        elif action == 'STOP_STRATEGY':
            account_id = task.get('account_id')
            if account_id in self.app.strategy_instances:
                self.log_queue.put(f"正在停止账户 {account_id} 的策略...")
                self._stop_strategy_sync(account_id)
                self.log_queue.put(f"账户 {account_id} 的策略已停止。")
                self.account_info_queue.put({'id': account_id, 'status': 'inactive'})
            else:
                self.log_queue.put(f"指令忽略：账户 {account_id} 未在运行策略。")

    def _stop_strategy_sync(self, account_id):
        if account_id in self.app.strategy_instances:
            strategy = self.app.strategy_instances.pop(account_id)
            strategy.stop_strategy()
            strategy.join(timeout=5)

    def _calculate_volume(self, master_trade, master_info, slave_info, slave_config, mt5_conn, symbol):
        master_volume = master_trade.volume if hasattr(master_trade, 'volume') else master_trade.volume_initial
        volume_mode = slave_config.get('volume_mode', 'same_as_master')
        volume = master_volume

        if volume_mode == 'fixed_lot':
            try: volume = float(slave_config.get('fixed_lot_size', '0.01'))
            except (ValueError, TypeError): volume = 0.01
        elif volume_mode == 'equity_ratio':
            if master_info and master_info.equity > 0 and slave_info and slave_info.equity > 0:
                ratio = slave_info.equity / master_info.equity
                volume = master_volume * ratio

        symbol_info = mt5_conn.symbol_info(symbol)
        if symbol_info:
            lot_step, min_lot, max_lot = symbol_info.volume_step, symbol_info.volume_min, symbol_info.volume_max
            volume = max(min_lot, volume)
            volume = round(volume / lot_step) * lot_step
            volume = min(max_lot, volume)
        return volume

    def _process_logged_in_accounts(self):
        MAX_CONN_FAILURES = 10
        logged_in_accounts_copy = self.app.logged_in_accounts.copy()
        if not logged_in_accounts_copy:
            return

        # 1. 验证新配置
        for acc_id in list(self.app.pending_verification_config.keys()):
            if acc_id in logged_in_accounts_copy:
                temp_config = self.app.pending_verification_config[acc_id]
                self.log_queue.put(f"正在使用新配置验证账户 {acc_id}...")
                _, mt5_conn, _ = self._connect_mt5(temp_config, self.log_queue, f"验证账户 {acc_id}")
                if mt5_conn:
                    self.log_queue.put(f"账户 {acc_id} 验证成功！新配置已应用。")
                    for key, value in temp_config.items():
                        self.app.app_config.set(acc_id, key, str(value))
                    self.app.verified_passwords.add(acc_id)
                    self.connection_failures[acc_id] = 0
                    mt5_conn.shutdown()
                else:
                    self.log_queue.put(f"账户 {acc_id} 验证失败。配置未更改。")
                    self.connection_failures[acc_id] = MAX_CONN_FAILURES + 1
                del self.app.pending_verification_config[acc_id]

        # 2. 处理跟单账户
        slaves_by_master = {}
        for i in range(1, self.app.NUM_SLAVES + 1):
            slave_id = f'slave{i}'
            if slave_id not in logged_in_accounts_copy or slave_id in self.app.strategy_instances: continue
            slave_config = self.app.app_config[slave_id]
            if not slave_config.getboolean('enabled', fallback=False): continue
            master_id_to_follow = slave_config.get('follow_master_id', 'master1')
            if master_id_to_follow not in slaves_by_master:
                slaves_by_master[master_id_to_follow] = []
            slaves_by_master[master_id_to_follow].append((slave_id, slave_config))
        
        for master_id, slave_group in slaves_by_master.items():
            if master_id in logged_in_accounts_copy:
                master_config = self.app.app_config[master_id]
                master_config['account_id'] = master_id
                if self.connection_failures.get(master_id, 0) >= MAX_CONN_FAILURES:
                    self.account_info_queue.put({'id': master_id, 'status': 'locked'})
                    continue
                ping, mt5_master, err_code = self._connect_mt5(master_config, self.log_queue, f"主账户 {master_id}")
                if not mt5_master:
                    self.connection_failures[master_id] = self.connection_failures.get(master_id, 0) + 1
                    if err_code == 1045: self.connection_failures[master_id] = MAX_CONN_FAILURES + 1
                    for s_id, _ in slave_group: self.account_info_queue.put({'id': s_id, 'status': 'inactive'})
                    continue
                try:
                    self.connection_failures[master_id] = 0
                    self._get_account_details(mt5_master, master_id, ping)
                    self.account_info_queue.put({'id': master_id, 'status': 'connected'})
                    master_info = mt5_master.account_info()
                    if not master_info: continue
                    master_trades_dict = {t.ticket: t for t in list(mt5_master.positions_get() or []) + list(mt5_master.orders_get() or [])}
                    for slave_id, slave_config in slave_group:
                        slave_config['account_id'] = slave_id
                        if self.connection_failures.get(slave_id, 0) >= MAX_CONN_FAILURES:
                            self.account_info_queue.put({'id': slave_id, 'status': 'locked'})
                            continue
                        self._copy_trades_for_slave(slave_id, slave_config, master_trades_dict, master_info, self.app.per_slave_mapping, self.log_queue)
                finally:
                    mt5_master.shutdown()

        # 3. 处理所有其他已登录的账户 (运行策略的，或仅登录监控的)
        for acc_id in logged_in_accounts_copy:
            is_strategy_running = acc_id in self.app.strategy_instances
            is_copying = any(acc_id == s_id for m_id in slaves_by_master for s_id, _ in slaves_by_master[m_id])

            if is_copying: continue

            if self.connection_failures.get(acc_id, 0) >= MAX_CONN_FAILURES:
                self.account_info_queue.put({'id': acc_id, 'status': 'locked'})
                continue

            config = self.app.app_config[acc_id]
            config['account_id'] = acc_id
            ping, mt5_conn, err_code = self._connect_mt5(config, self.log_queue, f"账户 {acc_id}")

            if mt5_conn:
                self.connection_failures[acc_id] = 0
                self._get_account_details(mt5_conn, acc_id, ping)
                mt5_conn.shutdown()
                if is_strategy_running:
                    if not self.app.strategy_instances[acc_id].is_alive():
                        self.log_queue.put(f"警告：账户 {acc_id} 的策略线程已意外终止。")
                        del self.app.strategy_instances[acc_id]
                        self.account_info_queue.put({'id': acc_id, 'status': 'error'})
                    else:
                        self.account_info_queue.put({'id': acc_id, 'status': 'strategy_running'})
                else:
                    self.account_info_queue.put({'id': acc_id, 'status': 'connected'})
            else:
                self.connection_failures[acc_id] = self.connection_failures.get(acc_id, 0) + 1
                if err_code == 1045:
                    self.connection_failures[acc_id] = MAX_CONN_FAILURES + 1
                    if is_strategy_running:
                        self.log_queue.put(f"!!! 严重错误: 账户 {acc_id} 授权失败，已停止该策略。")
                        self.task_queue.put({'action': 'STOP_STRATEGY', 'account_id': acc_id})

    def _copy_trades_for_slave(self, slave_id, slave_config, master_trades_dict, master_info, per_slave_mapping, log_queue):
        is_enabled = slave_config.getboolean('enabled', fallback=False)
        if not is_enabled:
            self.account_info_queue.put({'id': slave_id, 'status': 'disabled'})
            return

        ping, mt5_conn, err_code = self._connect_mt5(slave_config, log_queue, f"从账户 {slave_id}")
        if not mt5_conn:
            self.connection_failures[slave_id] = self.connection_failures.get(slave_id, 0) + 1
            return
        try:
            self._get_account_details(mt5_conn, slave_id, ping)
            self.account_info_queue.put({'id': slave_id, 'status': 'copying'})
            
            slave_info = mt5_conn.account_info()
            self.connection_failures[slave_id] = 0
            if not slave_info: return

            copy_mode = slave_config.get('copy_mode', 'forward')
            slave_magic = int(slave_config.get('magic'))
            current_slave_map = per_slave_mapping.get(slave_id, {})
            deviation_points = int(slave_config.get('slippage', '20'))
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
                    log_queue.put(f"[{slave_id}] 检测到主单 {master_ticket_ref} 已关闭，正在平掉从单 {slave_trade.ticket}...")
                    if hasattr(slave_trade, 'profit'): # 持仓
                        tick = mt5_conn.symbol_info_tick(slave_trade.symbol)
                        if tick:
                            close_request = {"action": mt5.TRADE_ACTION_DEAL, "position": slave_trade.ticket, "symbol": slave_trade.symbol, "volume": slave_trade.volume, "type": 1-slave_trade.type, "price": tick.bid if slave_trade.type == 0 else tick.ask, "deviation": deviation_points, "magic": slave_magic, "comment": f"Close F {master_ticket_ref}", "type_filling": mt5.ORDER_FILLING_IOC}
                            mt5_conn.order_send(close_request)
                    else: # 挂单
                        remove_request = {"action": mt5.TRADE_ACTION_REMOVE, "order": slave_trade.ticket}
                        mt5_conn.order_send(remove_request)

            for master_ticket_ref, slave_trade in slave_copied_trades.items():
                if master_ticket_ref in master_trades_dict:
                    master_trade = master_trades_dict[master_ticket_ref]
                    expected_sl, expected_tp = (master_trade.tp, master_trade.sl) if copy_mode == 'reverse' else (master_trade.sl, master_trade.tp)
                    if abs(slave_trade.sl - expected_sl) > 1e-9 or abs(slave_trade.tp - expected_tp) > 1e-9:
                        log_queue.put(f"[{slave_id}] 检测到主单 {master_ticket_ref} 的SL/TP已修改，正在更新从单 {slave_trade.ticket}...")
                        modify_request = {"sl": expected_sl, "tp": expected_tp, "symbol": slave_trade.symbol}
                        if hasattr(slave_trade, 'profit'):
                            modify_request.update({"action": mt5.TRADE_ACTION_SLTP, "position": slave_trade.ticket})
                        else:
                            modify_request.update({"action": mt5.TRADE_ACTION_MODIFY, "order": slave_trade.ticket, "price": slave_trade.price_open})
                        result = mt5_conn.order_send(modify_request)
                        if result and result.retcode != mt5.TRADE_RETCODE_DONE:
                            log_queue.put(f"[{slave_id}] 更新从单 {slave_trade.ticket} SL/TP 失败: {result.comment} (代码: {result.retcode})")

            for m_ticket, master_trade in master_trades_dict.items():
                if m_ticket not in slave_copied_trades and master_trade.magic != slave_magic:
                    slave_symbol = master_trade.symbol
                    mapping_rule_tuple = current_slave_map.get(master_trade.symbol)
                    if mapping_rule_tuple:
                        rule, text = mapping_rule_tuple
                        if rule == 'replace': slave_symbol = text
                    elif default_rule != 'none' and default_text:
                        if default_rule == 'suffix': slave_symbol += default_text
                        elif default_rule == 'prefix': slave_symbol = default_text + slave_symbol
                    
                    if not mt5_conn.symbol_select(slave_symbol, True):
                        log_queue.put(f"[{slave_id}] 无法选择品种 {slave_symbol}，跳过订单 {m_ticket}"); continue

                    volume = self._calculate_volume(master_trade, master_info, slave_info, slave_config, mt5_conn, slave_symbol)
                    sl, tp = (master_trade.tp, master_trade.sl) if copy_mode == 'reverse' else (master_trade.sl, master_trade.tp)
                    type_map_reverse = {0: 1, 1: 0, 2: 5, 3: 4, 4: 3, 5: 2}
                    trade_type = type_map_reverse.get(master_trade.type) if copy_mode == 'reverse' else master_trade.type
                    if trade_type is None and copy_mode == 'reverse': continue

                    request = {"symbol": slave_symbol, "volume": volume, "type": trade_type, "sl": sl, "tp": tp, "magic": slave_magic, "comment": f"F {m_ticket}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC, "deviation": deviation_points}
                    if hasattr(master_trade, 'price_open'):
                        request.update({"action": mt5.TRADE_ACTION_PENDING, "price": master_trade.price_open})
                    else:
                        tick = mt5_conn.symbol_info_tick(slave_symbol)
                        if not tick: continue
                        price = tick.ask if trade_type in [0, 2, 4] else tick.bid
                        request.update({"action": mt5.TRADE_ACTION_DEAL, "price": price})
                    log_queue.put(f"[{slave_id}] 正在为 主单 {m_ticket} 创建从单...")
                    mt5_conn.order_send(request)
        except Exception as e:
            log_queue.put(f"处理从账户 {slave_id} 时出错: {e}")
        finally:
            if mt5_conn: mt5_conn.shutdown()

    def _close_all_trades_for_single_account(self, account_id, config):
        self.log_queue.put(f"--- 正在清空账户 {account_id} ---")
        _, mt5_conn, _ = self._connect_mt5(config, self.log_queue, f"清仓账户 {account_id}")
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
            self.log_queue.put(f"  > [{account_id}] 清空指令已发送。")
        finally:
            mt5_conn.shutdown()

    def _close_single_trade_for_account(self, account_id, config, ticket):
        self.log_queue.put(f"--- 正在为账户 {account_id} 处理订单 {ticket} ---")
        _, mt5_conn, _ = self._connect_mt5(config, self.log_queue, f"平仓账户 {account_id}")
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

    def _connect_mt5(self, config, log_queue, account_id_str):
        """辅助函数：连接到单个MT5账户。"""
        if not all(config.get(k) for k in ['path', 'login', 'password', 'server']):
            self.account_info_queue.put({'id': config.get('account_id'), 'status': 'config_incomplete'})
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
            self.account_info_queue.put({'id': config.get('account_id'), 'status': 'error', 'ping': -1})
            return None, None, error_code
        
        return ping, mt5, mt5.RES_S_OK

    def _get_account_details(self, mt5_instance, account_id, ping):
        """辅助函数：获取账户详细信息"""
        info = mt5_instance.account_info()
        if not info:
            self.account_info_queue.put({'id': account_id, 'status': 'error', 'ping': ping})
            return

        positions = mt5_instance.positions_get() or []
        orders = mt5_instance.orders_get() or []
        
        processed_trades = []
        for trade in list(positions) + list(orders):
            is_position = hasattr(trade, 'profit')
            if is_position:
                trade_type_str = "Buy" if trade.type == mt5.ORDER_TYPE_BUY else "Sell"
                profit_or_status = f"{trade.profit:,.2f}"
                volume = trade.volume
            else: # is order
                type_map = {mt5.ORDER_TYPE_BUY_LIMIT: "Buy Limit", mt5.ORDER_TYPE_SELL_LIMIT: "Sell Limit", 
                            mt5.ORDER_TYPE_BUY_STOP: "Buy Stop", mt5.ORDER_TYPE_SELL_STOP: "Sell Stop"}
                trade_type_str = type_map.get(trade.type, "Pending")
                profit_or_status = "挂单中"
                volume = trade.volume_initial
            
            processed_trades.append({
                'ticket': trade.ticket, 'symbol': trade.symbol, 'type_str': trade_type_str,
                'volume': volume, 'price_open': trade.price_open, 'sl': trade.sl, 'tp': trade.tp,
                'profit_str': profit_or_status, 'is_position': is_position,
                'profit': getattr(trade, 'profit', 0.0)
            })

        self.account_info_queue.put({
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
            'positions_data': processed_trades
        })
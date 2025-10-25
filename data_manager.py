# data_manager.py

import pandas as pd
import os
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import time
import duckdb  # 导入 duckdb
import re

# 建议在 constants.py 中将 HDF5_FILE 更改为 DUCKDB_FILE
# from constants import DUCKDB_FILE 
# 为了方便，我们暂时在这里定义
DUCKDB_FILE = 'data/market_data.duckdb'

from mt5_utils import _connect_mt5

class DataManager:
    def __init__(self, data_path=DUCKDB_FILE):
        """初始化数据管理器，指定DuckDB文件路径。"""
        self.data_path = data_path
        # 确保数据文件所在的目录存在
        os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
        # print(f"[DataManager] 使用 DuckDB 数据库: {self.data_path}")

    def _get_connection(self):
        """获取一个DuckDB连接。"""
        return duckdb.connect(database=self.data_path, read_only=False)

    def _sanitize_name(self, name):
        """将 symbol 和 timeframe 转换为有效的SQL表名。"""
        # 移除非字母数字字符，替换为下划线
        return re.sub(r'[^A-Za-z0-9_]+', '_', name)

    def _get_table_name(self, symbol, timeframe_str):
        """从 symbol 和 timeframe 生成标准化的表名。"""
        return f"{self._sanitize_name(symbol)}_{self._sanitize_name(timeframe_str)}"

    def sync_data(self, symbols, timeframes, mt5_config, log_queue, start_date_str=None, end_date_str=None):
        """
        同步多个交易品种和时间周期的数据到DuckDB。
        """
        log_queue.put(f"[DataManager] 开始数据同步任务 (数据库: DuckDB)...")
        
        total_tasks = len(symbols) * len(timeframes)
        completed_tasks = 0
        
        ping, mt5_conn, err_code = _connect_mt5(mt5_config, log_queue, f"数据同步")
        if not mt5_conn:
            log_queue.put(f"[DataManager] 错误：无法连接到MT5进行数据同步。错误代码: {err_code}")
            return False

        try:
            with self._get_connection() as conn:
                for symbol in symbols:
                    for tf_str in timeframes:
                        completed_tasks += 1
                        log_queue.put(f"[DataManager] 正在处理 {symbol} - {tf_str}... (进度 {completed_tasks}/{total_tasks})")
                        
                        table_name = self._get_table_name(symbol, tf_str)
                        
                        # 确保表存在
                        conn.execute(f"""
                            CREATE TABLE IF NOT EXISTS {table_name} (
                                time TIMESTAMP PRIMARY KEY,
                                open DOUBLE,
                                high DOUBLE,
                                low DOUBLE,
                                close DOUBLE,
                                tick_volume BIGINT,
                                spread INT,
                                real_volume BIGINT
                            )
                        """)
                        
                        # 确定下载的时间范围
                        if start_date_str and end_date_str:
                            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
                            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
                            log_queue.put(f"[DataManager] 使用指定时间范围: {start_date.date()} 到 {end_date.date()}")
                        else:
                            # 增量同步逻辑
                            start_date = datetime(2020, 1, 1) # 默认的起始下载日期
                            try:
                                # 查找本地最新时间戳
                                last_time_result = conn.execute(f"SELECT MAX(time) FROM {table_name}").fetchone()
                                if last_time_result and last_time_result[0]:
                                    last_time = last_time_result[0]
                                    start_date = last_time + timedelta(minutes=1) # 从最后一条数据之后开始
                                    log_queue.put(f"[DataManager] 本地最新数据时间: {last_time}，将从之后开始同步。")
                                else:
                                     log_queue.put(f"[DataManager] 本地没有找到 {table_name} 的数据，将从 {start_date.date()} 开始完整下载。")
                            except Exception as e:
                                log_queue.put(f"[DataManager] 查询本地数据时发生错误: {e}，将尝试完整下载。")
                            
                            end_date = datetime.now()
                        
                        if start_date >= end_date:
                            log_queue.put(f"[DataManager] {symbol} - {tf_str} 的数据已是最新，无需同步。")
                            continue

                        timeframe_mt5 = getattr(mt5, f"TIMEFRAME_{tf_str}")
                        
                        log_queue.put(f"[DataManager] 正在从MT5下载 {symbol} {tf_str} 数据...")
                        rates = mt5_conn.copy_rates_range(symbol, timeframe_mt5, start_date, end_date)
                        
                        if rates is None or len(rates) == 0:
                            log_queue.put(f"[DataManager] 未能获取 {symbol} 在 {tf_str} 的新数据。")
                            continue

                        data_df = pd.DataFrame(rates)
                        data_df['time'] = pd.to_datetime(data_df['time'], unit='s')
                        
                        # 在写入DuckDB时，列名必须完全匹配
                        # MT5返回 'tick_volume', 'spread', 'real_volume'
                        # 确保我们的表结构和DataFrame列名一致
                        data_df = data_df[['time', 'open', 'high', 'low', 'close', 'tick_volume', 'spread', 'real_volume']]

                        # 使用 DuckDB 的高效方式插入数据，并自动处理重复（基于主键 'time'）
                        conn.register('new_data_df', data_df)
                        conn.execute(f"""
                            INSERT INTO {table_name} 
                            SELECT * FROM new_data_df
                            ON CONFLICT(time) DO NOTHING
                        """)
                        
                        log_queue.put(f"[DataManager] 成功同步并写入了 {len(data_df)} 条 {symbol} ({tf_str}) 的新数据。")
                        time.sleep(0.5)

            log_queue.put("[DataManager] 所有数据同步任务完成。")
            return True

        except Exception as e:
            import traceback
            log_queue.put(f"[DataManager] 同步数据时发生严重错误: {e}\n{traceback.format_exc()}")
            return False
        finally:
            if mt5_conn:
                mt5_conn.shutdown()

    def get_data(self, symbol, timeframe_str, start_date, end_date):
        """
        从DuckDB文件中获取指定范围内的数据。
        """
        table_name = self._get_table_name(symbol, timeframe_str)
        
        if isinstance(start_date, str):
            start_date = pd.to_datetime(start_date)
        if isinstance(end_date, str):
            end_date = pd.to_datetime(end_date)

        try:
            # 使用 'read_only=True' 可以允许多个进程同时读取
            with duckdb.connect(database=self.data_path, read_only=True) as conn:
                
                # 1. 检查表是否存在
                table_check = conn.execute(f"SELECT 1 FROM information_schema.tables WHERE table_name = '{table_name}'").fetchone()
                if not table_check:
                    print(f"[DataManager] 警告: 在数据库 '{self.data_path}' 中没有找到表 '{table_name}'。")
                    return None
                
                # 2. 查询数据
                # 使用参数化查询防止SQL注入
                query = f"""
                    SELECT * FROM {table_name} 
                    WHERE time >= ? AND time <= ?
                    ORDER BY time
                """
                data = conn.execute(query, [start_date, end_date]).fetch_df()
                
                if data.empty:
                    print(f"[DataManager] 警告: 在指定日期范围 {start_date.date()} 到 {end_date.date()} 内没有找到 {table_name} 的数据。")
                    return None
                
                # 3. 将 'time' 列设为索引，以匹配 backtest_engine 的期望
                data.set_index('time', inplace=True)
                return data
                
        except Exception as e:
            print(f"[DataManager] 从DuckDB文件中读取数据时出错: {e}")
            return None

    def get_local_data_list(self):
        """扫描DuckDB，返回所有已存储数据集的列表。"""
        if not os.path.exists(self.data_path):
            return []
        
        datasets = []
        try:
            with duckdb.connect(database=self.data_path, read_only=True) as conn:
                tables = conn.execute("SHOW TABLES").fetchall()
                
                for (table_name,) in tables:
                    try:
                        # 获取详细信息
                        stats = conn.execute(f"""
                            SELECT 
                                COUNT(*), 
                                MIN(time), 
                                MAX(time) 
                            FROM {table_name}
                        """).fetchone()
                        
                        count, min_date, max_date = stats
                        
                        if count == 0:
                            continue
                            
                        # 尝试从表名解析回 symbol 和 timeframe (这依赖于 _get_table_name 的逻辑)
                        # 这是一个简单的假设，可能需要根据你的命名规则调整
                        parts = table_name.rsplit('_', 1)
                        symbol = parts[0]
                        timeframe = parts[1] if len(parts) > 1 else 'UNKNOWN'
                        
                        datasets.append({
                            'symbol': symbol, 
                            'timeframe': timeframe,
                            'count': count,
                            'start_date': min_date.strftime('%Y-%m-%d'),
                            'end_date': max_date.strftime('%Y-%m-%d')
                        })
                    except Exception as e:
                        print(f"[DataManager] 无法获取 {table_name} 的详细信息: {e}")
                        continue

        except Exception as e:
            print(f"[DataManager] 扫描本地数据仓库时出错: {e}")
        
        return sorted(datasets, key=lambda x: (x['symbol'], x['timeframe']))
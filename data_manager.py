# data_manager.py

import pandas as pd
import os
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import time

from constants import HDF5_FILE
from mt5_utils import _connect_mt5

class DataManager:
    def __init__(self, data_path=HDF5_FILE):
        """初始化数据管理器，指定HDF5文件路径。"""
        self.data_path = data_path
        # 确保数据文件所在的目录存在
        os.makedirs(os.path.dirname(self.data_path), exist_ok=True)

    def sync_data(self, symbols, timeframes, mt5_config, log_queue, start_date_str=None, end_date_str=None):
        """
        同步多个交易品种和时间周期的数据。
        该函数会检查本地HDF5文件中每组数据的最新时间戳，
        然后只从MT5下载从该时间戳到现在的增量数据。
        如果指定了时间范围，则下载指定范围内的数据。
        """
        log_queue.put(f"[DataManager] 开始数据同步任务...")
        
        ping, mt5_conn, err_code = _connect_mt5(mt5_config, log_queue, f"数据同步")
        if not mt5_conn:
            log_queue.put(f"[DataManager] 错误：无法连接到MT5进行数据同步。错误代码: {err_code}")
            return False

        try:
            for symbol in symbols:
                for tf_str in timeframes:
                    log_queue.put(f"[DataManager] 正在处理 {symbol} - {tf_str}...")
                    key = f'{symbol.upper()}/{tf_str.upper()}'
                    
                    # 确定下载的时间范围
                    if start_date_str and end_date_str:
                        # 使用指定的时间范围
                        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
                        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
                        log_queue.put(f"[DataManager] 使用指定时间范围: {start_date.date()} 到 {end_date.date()}")
                    else:
                        # 使用默认的增量同步逻辑
                        start_date = datetime(2020, 1, 1) # 默认的起始下载日期
                        
                        try:
                            with pd.HDFStore(self.data_path, 'r') as store:
                                if key in store:
                                    # 如果数据已存在，找到最新的时间戳
                                    last_time = store.select(key, start=-1).index[0]
                                    # 从最后一条数据之后开始下载
                                    start_date = last_time.to_pydatetime() + timedelta(minutes=1) 
                                    log_queue.put(f"[DataManager] 本地最新数据时间: {last_time}，将从之后开始同步。")
                        except (KeyError, IndexError):
                            log_queue.put(f"[DataManager] 本地没有找到 {key} 的数据，将从 {start_date.date()} 开始完整下载。")
                        except Exception as e:
                            log_queue.put(f"[DataManager] 读取本地数据时发生错误: {e}，将尝试完整下载。")

                        end_date = datetime.now()
                    
                    if start_date >= end_date:
                        log_queue.put(f"[DataManager] {symbol} - {tf_str} 的数据已是最新，无需同步。")
                        continue

                    timeframe_mt5 = getattr(mt5, f"TIMEFRAME_{tf_str}")
                    
                    # 请求数据
                    rates = mt5_conn.copy_rates_range(symbol, timeframe_mt5, start_date, end_date)
                    
                    if rates is None or len(rates) == 0:
                        log_queue.put(f"[DataManager] 未能获取 {symbol} 在 {tf_str} 的新数据。")
                        continue

                    # 转换数据为DataFrame
                    data_df = pd.DataFrame(rates)
                    data_df['time'] = pd.to_datetime(data_df['time'], unit='s')
                    data_df.set_index('time', inplace=True)
                    
                    # 去重，防止下载的数据与本地最后一条数据重叠
                    data_df = data_df[~data_df.index.duplicated(keep='first')]

                    # 将新数据追加到HDF5文件
                    try:
                        with pd.HDFStore(self.data_path, 'a') as store:
                            # 使用 'append' 模式来添加数据，不创建列索引
                            store.append(key, data_df, format='table')
                    except Exception as hdf_error:
                        log_queue.put(f"[DataManager] HDF5写入失败，尝试重新创建文件: {hdf_error}")
                        # 如果HDF5文件损坏，尝试重新创建
                        try:
                            backup_path = self.data_path + '.backup'
                            if os.path.exists(self.data_path):
                                os.rename(self.data_path, backup_path)
                                log_queue.put(f"[DataManager] 已备份损坏的文件到: {backup_path}")
                            
                            # 重新创建文件并写入数据
                            with pd.HDFStore(self.data_path, 'w') as new_store:
                                new_store.append(key, data_df, format='table')
                            log_queue.put(f"[DataManager] 已重新创建数据文件并写入数据")
                        except Exception as recreate_error:
                            log_queue.put(f"[DataManager] 重新创建文件失败: {recreate_error}")
                            continue
                    
                    log_queue.put(f"[DataManager] 成功同步并追加了 {len(data_df)} 条 {symbol} ({tf_str}) 的K线数据。")
                    time.sleep(0.5) # 短暂休眠，防止过于频繁地请求API

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
        从HDF5文件中获取指定范围内的数据。
        这是回测引擎的数据来源。
        """
        key = f'{symbol.upper()}/{timeframe_str.upper()}'
        
        # 确保日期是datetime对象
        if isinstance(start_date, str):
            start_date = pd.to_datetime(start_date)
        if isinstance(end_date, str):
            end_date = pd.to_datetime(end_date)

        try:
            with pd.HDFStore(self.data_path, 'r') as store:
                if key not in store:
                    print(f"[DataManager] 警告: 在本地数据文件 '{self.data_path}' 中没有找到键 '{key}'。")
                    return None
                
                # 根据日期范围筛选数据
                # HDFStore的查询语法要求使用字符串
                where_clause = f"index >= '{start_date}' and index <= '{end_date}'"
                data = store.select(key, where=where_clause)
                
                if data.empty:
                    print(f"[DataManager] 警告: 在指定日期范围 {start_date.date()} 到 {end_date.date()} 内没有找到 {key} 的数据。")
                    return None
                    
                return data
        except Exception as e:
            print(f"[DataManager] 从HDF5文件中读取数据时出错: {e}")
            return None

    def get_local_data_list(self):
        """扫描HDF5文件，返回所有已存储数据集的列表（symbol, timeframe, count, min_date, max_date）。"""
        if not os.path.exists(self.data_path):
            return []
        
        datasets = []
        try:
            with pd.HDFStore(self.data_path, 'r') as store:
                for key in store.keys():
                    # 跳过系统内部键
                    if key.startswith('/_') or key == '/':
                        continue
                        
                    parts = key.strip('/').split('/')
                    if len(parts) == 2:
                        symbol, timeframe = parts
                        try:
                            # 尝试获取数据信息
                            try:
                                # 先尝试获取存储器信息
                                storer = store.get_storer(key)
                                if storer is None:
                                    continue
                                    
                                num_rows = storer.nrows
                                if num_rows == 0:
                                    continue
                                
                                # 尝试读取第一条和最后一条数据
                                try:
                                    first_data = store.select(key, start=0, stop=1)
                                    last_data = store.select(key, start=num_rows-1, stop=num_rows)
                                    
                                    if first_data.empty or last_data.empty:
                                        continue
                                        
                                    first_date = first_data.index[0].strftime('%Y-%m-%d')
                                    last_date = last_data.index[0].strftime('%Y-%m-%d')
                                    
                                    datasets.append({
                                        'symbol': symbol, 
                                        'timeframe': timeframe,
                                        'count': num_rows,
                                        'start_date': first_date,
                                        'end_date': last_date
                                    })
                                    
                                except Exception as read_error:
                                    # 如果无法读取数据，至少提供基本信息
                                    datasets.append({
                                        'symbol': symbol, 
                                        'timeframe': timeframe,
                                        'count': num_rows,
                                        'start_date': '未知',
                                        'end_date': '未知'
                                    })
                                    
                            except Exception as storer_error:
                                print(f"[DataManager] 无法获取 {key} 的存储器信息: {storer_error}")
                                continue
                                
                        except Exception as e:
                            print(f"[DataManager] 处理 {key} 时出错: {e}")
                            continue

        except Exception as e:
            print(f"[DataManager] 扫描本地数据仓库时出错: {e}")
            # 如果HDF5文件损坏，尝试重新创建
            try:
                print(f"[DataManager] 尝试重新创建数据文件...")
                if os.path.exists(self.data_path):
                    backup_path = self.data_path + '.backup'
                    os.rename(self.data_path, backup_path)
                    print(f"[DataManager] 已备份损坏的文件到: {backup_path}")
            except Exception as backup_error:
                print(f"[DataManager] 备份文件时出错: {backup_error}")
        
        return sorted(datasets, key=lambda x: (x['symbol'], x['timeframe']))
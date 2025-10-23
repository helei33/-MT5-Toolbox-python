GUIDE_TEXT = '''# 交易策略库设计模式与使用说明

## 设计模式分析
这个工具箱的策略功能采用的是一种 **“插件式”** 设计模式。

- **发现机制**：主程序在启动时，会自动扫描 `strategies` 文件夹，寻找所有 `.py` 结尾的文件，并将它们作为潜在的策略插件。
- **加载机制**：主程序会动态地加载这些文件，并在其中寻找一个继承了内置 `BaseStrategy` 基类的策略类。
- **实例化与运行**：当用户为某个账户选择并启动一个策略时，主程序会创建这个策略类的一个实例，并在一个独立的后台线程中调用它。
---
### `BaseStrategy` “插座”的规则和开发标准 
  
 `BaseStrategy` 是所有自动化交易策略的基石。它提供了一个安全、高效且功能丰富的运行环境，将 MT5 连接管理、日志记录、参数解析和基础交易操作等通用功能封装起来，让策略开发者可以专注于核心交易逻辑。 
  
 **核心理念：** 策略开发者只需实现特定的“钩子”方法，并利用基类提供的工具，即可构建功能强大的自动化策略。 
  
 --- 
  
 #### 1. 策略文件的结构和发现 
  
 *   **文件位置**: 策略必须是一个独立的 Python 文件（例如 `MyStrategy.py`），放置在 `strategies` 文件夹内。 
 *   **类定义**: 每个策略文件应包含一个且只有一个继承自 `BaseStrategy` 的类。这个类就是你的“插头”。 
  
 #### 2. 策略类的基本要求 (插头的“形状”和“标签”) 
  
 你的策略类必须继承 `BaseStrategy`，并定义以下**类属性**来向主程序声明其身份和功能： 
  
 *   **`strategy_name: str` (必需)** 
     *   **用途**: 策略的唯一标识符和在 UI 中显示的名称。 
     *   **规则**: 必须是一个字符串。建议使用简洁、描述性的名称。 
     *   **示例**: `strategy_name = "双均线交叉策略"` 
  
 *   **`strategy_description: str` (可选，强烈推荐)** 
     *   **用途**: 策略的详细说明，会在 UI 的策略库标签页中显示。 
     *   **规则**: 字符串，可以包含多行文本，解释策略的原理、适用市场、风险等。 
     *   **示例**: `strategy_description = "当快线（5周期）上穿慢线（20周期）时开多，下穿时开空。"` 
  
 *   **`strategy_params_config: dict` (可选，推荐)** 
     *   **用途**: 定义策略的所有可配置参数。主程序会根据此字典自动生成 UI 控件，并进行参数的加载、保存和类型转换。 
     *   **规则**: 这是一个字典，其中每个键是参数的内部名称（在策略代码中通过 `self.params['param_key']` 访问），每个值是另一个字典，描述该参数的属性： 
         *   `'label': str` (必需): 参数在 UI 中显示的名称。 
         *   `'type': str` (必需): 参数的数据类型。支持 `'int'`, `'float'`, `'bool'`, `'str'`。基类会自动将从配置文件读取的字符串值转换为此类型。 
         *   `'default': any` (必需): 参数的默认值。当用户未配置或配置文件中没有该参数时，将使用此值。类型应与 `'type'` 匹配。 
     *   **示例**: 
         ```python 
         strategy_params_config = { 
             "fast_ma_period": {"label": "快线周期", "type": "int", "default": 5}, 
             "slow_ma_period": {"label": "慢线周期", "type": "int", "default": 20}, 
             "trade_volume": {"label": "交易手数", "type": "float", "default": 0.01}, 
             "enable_trailing_stop": {"label": "启用追踪止损", "type": "bool", "default": True}, 
             "symbol_to_trade": {"label": "交易品种", "type": "str", "default": "EURUSD"} 
         } 
         ``` 
  
 #### 3. 策略的生命周期方法 (插头内部的“电路板”) 
  
 `BaseStrategy` 定义了三个核心的“钩子”方法，策略开发者需要重写它们来注入自己的交易逻辑。 
  
 *   **`on_init(self) -> bool`** 
     *   **调用时机**: 策略启动时，在 MT5 连接成功后，且在 `on_tick` 循环开始前，只调用一次。 
     *   **用途**: 用于策略的初始化设置，例如加载历史数据、订阅品种、检查参数等。 
     *   **返回值**: 如果返回 `True`，策略将继续执行。如果返回 `False`，策略将立即停止。 
     *   **示例**: 
         ```python 
         def on_init(self): 
             self.log("策略正在初始化...") 
             if self.params['trade_volume'] <= 0: 
                 self.log("错误: 交易手数必须大于0。") 
                 return False 
             return True 
         ``` 
  
 *   **`on_tick(self)`** 
     *   **调用时机**: 在策略运行期间，会以 `tick_interval`（默认为1秒）的频率循环调用。 
     *   **用途**: 策略的核心交易逻辑发生在这里，例如获取行情、计算指标、生成信号、执行交易等。 
     *   **注意**: 避免在此方法中执行耗时过长的操作。 
     *   **示例**: 
         ```python 
         def on_tick(self): 
             # if self.check_buy_signal() and not self.get_positions('EURUSD'): 
             #     self.buy('EURUSD', self.params['trade_volume']) 
             pass 
         ``` 
  
 *   **`on_deinit(self)`** 
     *   **调用时机**: 策略停止时，在 MT5 连接关闭前，只调用一次。 
     *   **用途**: 用于策略的清理工作，例如保存最终状态、记录总结等。 
     *   **示例**: 
         ```python 
         def on_deinit(self): 
             self.log("策略正在反初始化，执行清理工作...") 
         ``` 
  
 #### 4. `BaseStrategy` 提供的工具 (插座的“功能按钮”) 
  
 `BaseStrategy` 为你的策略提供了丰富的内置方法和属性，可以直接通过 `self.` 访问： 
  
 *   **`self.log(message: str)`**: 发送日志到主界面。 
 *   **`self.params: dict`**: 访问已自动转换类型的策略参数。 
 *   **`self.config: dict`**: 访问当前账户的配置信息。 
 *   **`self.mt5`**: 直接访问 `MetaTrader5` 库的实例，用于调用原生API。 
 *   **`self.connected: bool`**: 检查MT5连接状态。 
 *   **`self.buy(...)` / `self.sell(...)`**: 便捷的市价开仓方法。 
 *   **`self.get_positions(symbol: str = None) -> tuple`**: 获取当前持仓。 
 *   **`self.stop_strategy()`**: 请求停止策略。 
 *   **`self.is_running() -> bool`**: 检查策略是否被请求停止。 
 *   **`self.tick_interval: float`**: （可设置）控制 `on_tick` 的调用频率（秒）。 
  
 #### 5. 最佳实践和注意事项 
  
 *   **线程安全**: 策略内部变量是线程安全的，无需担心与其他策略实例冲突。 
 *   **错误处理**: 对 `order_send` 等关键API调用的返回值进行检查。 
 *   **日志记录**: 善用 `self.log()` 记录关键决策和状态，便于调试。 
 *   **避免阻塞**: `on_tick` 应快速返回。 
 *   **状态管理**: 使用 `self` 的属性（如 `self.last_trade_price`）来在 `on_tick` 调用之间保持状态。 
  
 --- 
  
 通过遵循这些规则和标准，你就可以创建出能无缝集成到本工具中运行的自动化交易策略。 
 """
'''
# Project Proposal: OpenPlantPulse — Desktop Hub Architecture

本方案将原本分散的“微控制器边缘网关”迁移至常开主机（macOS / Linux Desktop / Home Server）作为核心 Hub。花盆端仅保留极简的硬件单元，由常开桌面主机承担协议解析、数据建模、前瞻性预测与多生态分发。

借鉴 **`OrangePeachPink/sprout`** 的核心设计哲学（*“Monitor · Predict · Act”* 与极具设计感的可视化看板），常开主机的充沛算力与存储将彻底打破嵌入式芯片的性能束缚。

---

## 1. 架构拓扑 (Hybrid Edge-to-Host)

```text
[ 花盆端: 极简低功耗节点 ]
  316 不锈钢探头 (Modbus-RTU)
      └──> XIAO ESP32-C3 + RS485 扩展板 (18650 电池供电)
            └──[ 周期唤醒 / 2.5s BTHome BLE 广播 / Deep Sleep ]
                     │
                     ▼ (无感无线空口广播)
[ 桌面端: 常开 Desktop Hub (macOS / Linux) ]
  原生 Bluetooth 硬件 (CoreBluetooth / BlueZ)
      └──> OpenPlantPulse Daemon (常驻后台服务)
            ├── 模块 1: BTHome BLE 抓包与 Modbus 遥测解包
            ├── 模块 2: SQLite / InfluxDB 本地高频时序存储
            ├── 模块 3: 植物生理状态引擎 (吸收速率 / VPD / 施肥风险)
            └── 模块 4: 多端输出 (Web UI / 状态栏 / Home Assistant / Webhook)

```

---

## 2. 核心功能头脑风暴：借助电脑作为 Hub 能解锁什么？

微控制器（ESP32）受限于内存（400KB SRAM）和 Flash，只能做“数据搬运工”。而常开电脑拥有无限的持久化存储、浮点算力与网络拓扑能力，可以从以下四个维度借鉴并超越 `sprout`：

### 维度一：借鉴 Sprout 的核心能力 —— “Predict” 预测引擎

* **根系吸水速率与动态浇水倒计时 (Depletion Curve Fitting)：**
* 微控制器只能告诉你“当前湿度 25%”；
* 桌面 Hub 可以利用过去 7 天的时序数据拟合蒸腾衰减曲线，结合作息与室温，直接给出预测：*“预计 38 小时后（周四上午）土壤含水量跌破 15%，请届时浇水”*。


* **施肥累积与盐分脱水模型 (EC Dynamics)：**
* 连续记录每次浇水后的 EC 稀释与蒸发浓缩速率。当检测到水分下降但 EC 持续攀升且斜率异常时，自动判断盆底盐分结晶超标，提示*“高渗透压风险，下次浇水需大水透盆洗盐”*。



### 维度二：原生系统级集成 (Desktop-Native Integration)

* **macOS 顶部状态栏小组件 (Menu Bar Dropdown)：**
* 类似 iStat Menus，直接在桌面菜单栏常驻一株白鸟蕉小图标。
* 点开即可查看实时深层土壤含水量、EC、最近一次上报时间，以及简洁的水分历史 Sparkline 折线。


* **系统原生推送通知 (Native Notification Center)：**
* 当深层湿度骤降或环境异常，直接调用系统级桌面通知，无需依赖手机或外部云服务。


* **本地 Web 监控仪表盘 (借鉴 Sprout 的设计审美)：**
* 电脑本地跑一个轻量级 Web 服务（如 FastAPI / Svelte / Next.js），提供极具设计感暗黑模式的大屏看板，支持缩放查看数月的时序图表。



### 维度三：高阶植物生理学衍生指标

* **气孔导度与蒸汽压亏缺计算 (VPD Calculation)：**
* 结合土壤探头的地温传感器，调用本地气象 API 或室内温湿度计，计算植物冠层与空气之间的 **VPD (Vapor Pressure Deficit)**，准确评估植物是否处于气孔关闭或过度蒸腾状态。


* **DLI (日累积光照量) 关联分析：**
* 如果未来外接光照传感器，电脑端可轻松累加计算 24 小时内的 **DLI (Daily Light Integral)**，将光能吸收与吸水速率联动分析。



### 维度四：多协议转发与自动化联动 (Hub of Hubs)

* **Home Assistant 零侵入对接：**
* Hub 解析数据后，通过本地 MQTT 或 WebSocket 直接将实体推送给局域网内的 Home Assistant，让 HA 保持轻量，计算全在 Hub 完成。


* **硬件执行器扩展 (Actuators)：**
* 借鉴 `sprout` 的 *“Water (Next)”* 设想：当判定缺水时，电脑可通过 Wi-Fi/Zigbee 智能插座或 USB 继电器控制蠕动水泵，实现闭环精准滴灌。



---

## 3. 桌面 Hub 技术栈选型

* **运行环境：** macOS / Linux 后台守护进程（Launchd / Systemd）。
* **BLE 捕获层：** Python `bleak` 异步蓝牙协议库（原生跨平台支持 macOS CoreBluetooth 与 Linux BlueZ），专门监听标准 `0xFCD2` (BTHome v2) 广播包。
* **时序存储层：** 本地轻量化嵌入式数据库（SQLite + DuckDB 或轻量级 InfluxDB），零维护成本，支持单表百万条数据秒级聚合。
* **服务暴露：**
* 本地 REST/WebSocket API 供 Web UI 和状态栏抓取。
* 本地 MQTT Client 自动桥接发布至 Home Assistant (`homeassistant/sensor/...`)。



---

## 4. 实施阶段规划

* **阶段 1：** 验证花盆端 ESP32-C3 的低功耗 BTHome 广播时序，调通电脑端的 BLE 捕获与报文解包脚本。
* **阶段 2：** 建立本地 SQLite 时序存储与失水斜率算法，实现简单的吸水衰减预测。
* **阶段 3：** 封装为开机自启的后台守护进程，输出 macOS 状态栏应用或极简 Web 看板。
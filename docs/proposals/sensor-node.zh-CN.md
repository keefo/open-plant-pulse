# 工业级高精度绿植智能监测终端（FloraSense RS485）开源方案

本项目是一套专为大型室内观叶植物（如大鹤望兰 / 白鸟蕉）设计的高精度、低功耗、本地化土壤监测系统。通过工业级 316 不锈钢探头采集深层土壤介电常数（水分）、高频电导率（EC）与温湿度数据，结合 ESP32-C3 微控制器与 RS485 叠层设计，实现全链路无飞线的紧凑架构。数据通过 BTHome v2 协议以低功耗蓝牙（BLE）广播直连 Home Assistant，兼具工业仪表的可靠性与极简的美观度。

---

## 1. 硬件物料清单 (BOM)

| 类别 | 器件名称 | 关键规格 / 芯片型号 | 参考单价 (CAD) | 作用描述 |
| --- | --- | --- | --- | --- |
| **感知单元** | 工业多合一土壤探头 | ComWinTop NPKPHCTH-S (或 THC-S), 316 不锈钢针, IP68, RS485 (Modbus-RTU) | $38.00 – $57.80 | 深插盆底监测水分、EC、温度，耐高盐弱酸腐蚀 |
| **主控单元** | 开发板 | Seeed Studio XIAO ESP32-C3 (RISC-V 32-bit, 4MB Flash, 带外置软天线) | ~$12.60 | 协处理器调度、Modbus 采集、BLE 广播与休眠管理 |
| **接口单元** | 专用扩展叠层板 | Seeed Studio RS-485 Breakout Board for XIAO (板载 TP8485E、DC-DC 升压) | ~$7.30 | 直插叠层，提供 3.3V TTL-485 转换与 12V 探头升压 |
| **电源单元** | 动力/容量型锂电 | 18650 锂电池 (3.7V / 2600–3500mAh，带保护板) | 家中现存 / ~$8.00 | 提供整机数月至一年的野外级离线续航 |
| **辅料配件** | 电池盒与紧固件 | 单节 18650 带引线卡槽电池盒、PG7 防水接头、M2 螺丝 | ~$3.00 | 物理固定，避免电芯直接承受高温烙铁焊接 |
| **结构外壳** | 定制保护盒 | 3D 打印外壳 (PETG / ABS，阻燃耐候) | 自制 | 容纳双层主控与电池，支持挂靠盆壁边缘 |

---

## 2. 硬件架构与电气设计

### 2.1 拓扑框图

```text
[ 18650 锂电池 (3.7V) ]
         │
         ▼ (焊盘直连)
[ XIAO ESP32-C3 ]  <=== (2.54mm 双排针直插) ===>  [ RS-485 Breakout 扩展板 ]
   │ (GPIO 控制)                                            │
   └───────────────> [ DC-DC 升压与收发使能 ]                │ (12V 稳压 + RS485 差分)
                                                            ▼
                                               [ 螺丝端子排: VCC GND A+ B- ]
                                                            │ (2米工业屏蔽电缆)
                                                            ▼
                                               [ 工业 316 不锈钢土壤探头 ]

```

### 2.2 电气连接与引脚定义

1. **叠层母排连接：**
* XIAO ESP32-C3 直接插入扩展板的排母。
* 硬件通信端口：ESP32-C3 的 `GPIO21` 映射为 `UART TX`，`GPIO20` 映射为 `UART RX`。
* 拓扑电源控制：扩展板通过引脚（默认通常为 `D3` / `GPIO5`，视跳线定义）提供高低电平，用于开启或切断板载 12V 升压回路及 485 收发器电源。


2. **探头与螺丝端子接线：**
* **红线 (Power+)** $\rightarrow$ 扩展板端子 `VCC`（确认跳线设为 12V 档位）。
* **黑线 (GND)** $\rightarrow$ 扩展板端子 `GND`。
* **黄线 (485 A+)** $\rightarrow$ 扩展板端子 `A`。
* **绿线/蓝线 (485 B-)** $\rightarrow$ 扩展板端子 `B`。


3. **电池与充电回路：**
* 18650 电池盒的红正极、黑负极分别焊接至 XIAO ESP32-C3 背面的 `+` 和 `-` 电池专用焊盘。
* 维护充电：当需要补电时，直接使用 Type-C 数据线插入 XIAO 侧面接口，板载线性充电管理芯片自动以恒流恒压（CC/CV）对 18650 进行安全补充。



---

## 3. 固件架构与软件设计

为最大化电池续航并保证数据完全自治，软件采用纯 C/C++ 架构（基于 ESP-IDF 或 PlatformIO），抛弃高耗能的 Wi-Fi 连接，采用 **BTHome v2** 协议进行轻量级无连接 BLE 广播。

### 3.1 状态机工作流程

1. **唤醒 (Deep Sleep Wakeup)：** ESP32-C3 内部 RTC 定时器到期触发唤醒（默认周期 30 分钟）。
2. **传感器供电 (Power Gating ON)：** 拉高电源控制引脚，启动扩展板 12V 升压与 TP8485E 芯片，阻塞延时 150ms 等待探头电路进入稳态。
3. **Modbus-RTU 采集：** 通过硬件 UART 向地址 `0x01` 投递读保持寄存器指令（功能码 `0x03`），捕获包含水分、温度、EC 等参数的 19 字节响应，执行标准 CRC16-Modbus 校验。
4. **彻底掉电 (Power Gating OFF)：** 校验通过并解析数据后，立即拉低使能引脚，切断探头及升压板供电，将静态漏电归零。
5. **BLE 数据打包与广播：** 将采集数据组装为 BTHome v2 规范的 Service Data Payload，开启 NimBLE 发射 2.5 秒，确保客厅蓝牙网关可靠捕获。
6. **重入休眠：** 关闭射频，配置定时器 `30 * 60 * 1000000ULL` 微秒，进入深度休眠（此时系统总电流低于 20 $\mu\text{A}$）。

### 3.2 核心 C 源码实现 (`main.c`)

```c
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_sleep.h"
#include "esp_log.h"
#include "driver/uart.h"
#include "driver/gpio.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "services/gap/ble_svc_gap.h"

#define TAG             "FloraSense"
#define UART_PORT       UART_NUM_1
#define PIN_TXD         GPIO_NUM_21
#define PIN_RXD         GPIO_NUM_20
#define PIN_PWR_EN      GPIO_NUM_3

#define SLEEP_MINUTES   30

typedef struct {
    float humidity;     // %
    float temperature;  // °C
    uint16_t ec;        // us/cm
} SensorReadings;

static SensorReadings g_sensor_data;
static const uint8_t QUERY_CMD[8] = {0x01, 0x03, 0x00, 0x00, 0x00, 0x03, 0x05, 0xCB};

static uint16_t modbus_crc16(const uint8_t *buf, size_t len) {
    uint16_t crc = 0xFFFF;
    for (size_t pos = 0; pos < len; pos++) {
        crc ^= (uint16_t)buf[pos];
        for (int i = 8; i != 0; i--) {
            if (crc & 0x0001) {
                crc = (crc >> 1) ^ 0xA001;
            } else {
                crc >>= 1;
            }
        }
    }
    return crc;
}

static bool poll_sensor(SensorReadings *out) {
    uart_flush_input(UART_PORT);
    uart_write_bytes(UART_PORT, (const char *)QUERY_CMD, sizeof(QUERY_CMD));

    uint8_t rx[32];
    int len = uart_read_bytes(UART_PORT, rx, sizeof(rx), pdMS_TO_TICKS(500));

    // 基础校验: 1(ID) + 1(Func) + 1(ByteCount) + 6(Data: 3 regs) + 2(CRC) = 11 Bytes
    if (len < 11) {
        ESP_LOGE(TAG, "UART read timeout or insufficient length: %d", len);
        return false;
    }

    uint16_t rx_crc = (rx[len - 1] << 8) | rx[len - 2];
    if (modbus_crc16(rx, len - 2) != rx_crc) {
        ESP_LOGE(TAG, "CRC check failure");
        return false;
    }

    out->humidity    = ((rx[3] << 8) | rx[4]) * 0.1f;
    out->temperature = ((int16_t)((rx[5] << 8) | rx[6])) * 0.1f;
    out->ec          = (rx[7] << 8) | rx[8];
    return true;
}

// BTHome v2 格式广播封包与射频控制
static void start_bthome_adv(void) {
    struct ble_gap_adv_params adv_params;
    memset(&adv_params, 0, sizeof(adv_params));
    adv_params.conn_mode = BLE_GAP_CONN_MODE_NON;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    // 构造 BTHome Payload (未加密)
    // UUID: 0xFCD2 (BTHome)
    // 0x40 (BTHome v2 标志)
    // 0x02: 温度 (factor 0.01, sint16)
    // 0x03: 湿度 (factor 0.01, uint16)
    // 0x0A: 电导率 EC (factor 1, uint16)
    int16_t raw_temp = (int16_t)(g_sensor_data.temperature * 100);
    uint16_t raw_humi = (uint16_t)(g_sensor_data.humidity * 100);
    uint16_t raw_ec = g_sensor_data.ec;

    uint8_t p[] = {
        0x02, 0x01, 0x06,                         // Flags
        0x03, 0x03, 0xD2, 0xFC,                   // Complete 16-bit Service UUID: 0xFCD2
        0x10, 0x16, 0xD2, 0xFC,                   // Service Data Header
        0x40,                                     // BTHome Device Info (v2, unencrypted)
        0x02, (uint8_t)(raw_temp & 0xFF), (uint8_t)(raw_temp >> 8),
        0x03, (uint8_t)(raw_humi & 0xFF), (uint8_t)(raw_humi >> 8),
        0x0A, (uint8_t)(raw_ec & 0xFF), (uint8_t)(raw_ec >> 8)
    };

    ble_gap_adv_set_data(p, sizeof(p));
    ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER, &adv_params, NULL, NULL);
}

void app_main(void) {
    // 1. 初始化电源隔离引脚
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << PIN_PWR_EN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_down_en = GPIO_PULLDOWN_ENABLE
    };
    gpio_config(&io_conf);

    // 打开探头电源
    gpio_set_level(PIN_PWR_EN, 1);
    vTaskDelay(pdMS_TO_TICKS(150));

    // 2. 初始化 UART
    uart_config_t uart_config = {
        .baud_rate = 9600,
        .data_bits = UART_DATA_8_BITS,
        .parity    = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE
    };
    uart_param_config(UART_PORT, &uart_config);
    uart_set_pin(UART_PORT, PIN_TXD, PIN_RXD, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    uart_driver_install(UART_PORT, 256, 0, 0, NULL, 0);

    // 3. 执行 Modbus 读取
    bool success = poll_sensor(&g_sensor_data);

    // 4. 彻底断开探头电源以切断静态电流
    gpio_set_level(PIN_PWR_EN, 0);
    uart_driver_delete(UART_PORT);

    // 5. 广播数据
    if (success) {
        nimble_port_init();
        ble_svc_gap_device_name_set("FloraSense");
        nimble_port_freertos_init(start_bthome_adv);
        vTaskDelay(pdMS_TO_TICKS(2500)); // 广播 2.5 秒保证接收
    }

    // 6. 配置 RTC 定时并进入深度休眠
    esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_MINUTES * 60 * 1000000ULL);
    esp_deep_sleep_start();
}

```

---

## 4. 上位机接入与自动化部署 (Home Assistant)

由于固件直接遵循 **BTHome v2** 广播标准，免去了传统的 MQTT 配置环节：

1. **自动设备发现：**
* 确保局域网内存在带有 ESPHome Bluetooth Proxy 功能的节点，或 Home Assistant 宿主机自带蓝牙。
* 固件启动广播后，Home Assistant 的“设备与服务”界面将立即弹出“**发现新设备：BTHome Sensor (FloraSense)**”。


2. **实体生成：**
* 系统自动映射生成三个高精度实体：
* `sensor.florasense_temperature`（单位：°C）
* `sensor.florasense_humidity`（单位：%）
* `sensor.florasense_conductivity`（单位：$\mu\text{S/cm}$）




3. **大鹤望兰养护自动化参考配置 (`automations.yaml`)：**

```yaml
automation:
  - alias: "植物报警: 大鹤望兰根系极度缺水"
    trigger:
      - platform: numeric_state
        entity_id: sensor.florasense_humidity
        below: 18
        for:
          hours: 6
    action:
      - service: notify.notify
        data:
          title: "Strelitzia 浇水提醒"
          message: "盆底深层含水量已跌至 18%，根系处于吸水困难期，请执行充分灌透操作。"

  - alias: "施肥监测: 肥料浓度过高告警"
    trigger:
      - platform: numeric_state
        entity_id: sensor.florasense_conductivity
        above: 2500
    action:
      - service: notify.notify
        data:
          title: "Strelitzia 烧根风险"
          message: "土壤 EC 骤升至 2.5 mS/cm 以上，有高渗透压脱水烧根隐患，请暂缓施肥并用清水淋洗盆土。"

```

---

## 5. 机械工程与装配规范

* **探头埋设准则：**
* 不得在表层横置。需垂直或以 45 度角斜向深插入盆底向内 1/2 至 2/3 的深度（大鹤望兰粗壮肉质主根聚集区）。
* 插入时应顺延盆沿滑入，严禁用锤击打探头环氧灌封体，避免损坏内部压电晶振和 316 探针连接点。


* **外壳与电路布局：**
* 使用 PETG 材料 3D 打印一个体积约 $80 \times 45 \times 30\text{ mm}$ 的圆角机盒。
* 将叠层板紧固在盒底，电池盒置于侧方，探头 4 芯线缆通过 PG7 旋钮接头导入盒内并压紧锁死，实现良好的防潮防滴溅表现。
* 外壳背面可通过卡扣或挂环悬挂在花盆后侧踢脚线盲区，实现正面视觉的零线路杂乱感。
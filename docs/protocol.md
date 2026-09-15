# 串口协议（板子 ⟷ 下位机）

## 下发：亮度（板子 → MCU）

固定 3 字节，无校验：

```
0xAA  [亮度 0x00~0x64]  0x55
 │          │             └─ 帧尾
 │          └─ 亮度百分比 0~100（程序侧限制在软件死区 10~90 之间下发）
 └─ 帧头
```

例：亮度 50% → `AA 32 55`

## 上行：传感器文本行（MCU → 板子）

一行一条，`\r\n` 结尾，用正则解析：

| 报文 | 含义 |
|---|---|
| `Humi:45%RH Temp:26C` | 湿度 45%、温度 26℃ |
| `Light:50%` | 当前亮度回显（界面用它校准滑块） |
| `DHT11 Read Error` | 传感器读取失败（界面会把读数置 `--` 并记日志） |

## 连接方式

主程序的「端口」下拉框支持三种写法，都是同一个串口对象：

| 写法 | 场景 |
|---|---|
| `/dev/ttyUSB0` | USB 转串口 / HC-05 插 USB |
| `bt:AA:BB:CC:DD:EE:FF` | **RFCOMM 直连**（内核没编 RFCOMM TTY、没有 `/dev/rfcomm0` 时用这个） |
| `bt:auto` | 从环境变量 `LAMP_BT_MAC` 或 `~/.lamp_bt_mac` 取 MAC |
| `socket://127.0.0.1:8888` | 走 `bt_bridge.py` 桥接（给不支持 `socket.AF_BLUETOOTH` 的解释器，如 conda 版 Python） |

> 蓝牙寻呼+认证要 3~6 秒，连接超时必须给到 **≥10 秒**，否则会被误判为失败并反复重连。

## 下位机侧（真实实现）

固件已开源，完整代码见 [`../firmware/`](../firmware/)。主循环核心（`User/main.c`）：

```c
while (1)
{
    /* 串口调光：收到完整帧 0xAA [亮度] 0x55 即更新亮度 */
    if (Serial_GetFrameFlag())
    {
        PWM_StopSoftStart();                            // 取消软启动，防爬升覆盖手动设置
        Light = PWM_SetCompare2(Serial_GetFrameData());  // 死区钳位后取回实际生效值
        OLED_ShowNum(4, 7, Light, 2);
        Serial_Printf("Light:%d%%\r\n", Light);          // 回显实际生效值
    }

    /* 温湿度读取：每 2s 一次（查询式，主循环不等待） */
    if (Delay_Elapsed(ReadTick, 2000))
    {
        ReadTick = Delay_GetTick();
        if (DHT11_ReadData(&Hum, &Temp) == 0)
            Serial_Printf("Humi:%d%%RH Temp:%dC\r\n", Hum, Temp);
        else
            Serial_Printf("DHT11 Read Error\r\n");
    }
}
```

接收端是**中断 + 三态状态机**（`Hardware/Serial.c`），非帧头字节一律丢弃，
所以丢字节、被噪声打断都能自动重新同步，不会卡死在半帧：

```c
case 0: if (Byte == 0xAA) Serial_RxState = 1; break;          // 等帧头
case 1: Serial_FrameData = Byte; Serial_RxState = 2; break;   // 收数据字节
case 2: if (Byte == 0x55) Serial_FrameFlag = 1;               // 校验帧尾
        Serial_RxState = 0; break;                            // 无论对错都回到等帧头
```

下发值经 `PWM_SetCompare2()` 做 **10~90% 死区钳位**后返回实际生效值，
回显的就是这个真值——所以界面滑块与灯的实际亮度永远一致。

### 引脚对应（详见 [`../hardware/`](../hardware/)）

| 功能 | MCU 引脚 | 备注 |
|---|---|---|
| 调光 PWM → Q4（N-MOS） | **PB7** | TIM4_CH2，10kHz |
| 蓝牙串口（HC-05） | **PA9 / PA10** | USART1，9600 8N1 |
| DHT-11 数据 | **PA12** | 单总线，2s 一次 |
| 板载 OLED | **PB8 / PB9** | 软件 I2C（SCL / SDA） |
| 蜂鸣器 | **PB6** | 软启动完成时响 200ms |

上位机侧解析（见 `desk_lamp_gui_linux.py` 的 `_poll_serial()`，逐行正则匹配）：

```python
m = re.search(r"Humi:(\d+)%RH\s+Temp:(\d+)C", line)   # 温湿度一起报
if m:
    humi, temp = int(m.group(1)), int(m.group(2))

m = re.search(r"Light:(\d+)", line)                    # 亮度回显
if m:
    val = int(m.group(1))
```

> 注意：温湿度必须**同一行**给出（`Humi:..%RH Temp:..C`），否则只会匹配到其中一个。

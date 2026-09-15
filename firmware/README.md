# 下位机固件（STM32F103C8T6）

台灯的执行端：接收蓝牙传来的调光指令驱动 PWM，读取 DHT-11 温湿度回传，
并在一块 0.96" OLED 上显示本机状态。

- **芯片**：STM32F103C8T6（72MHz，64KB Flash / 20KB RAM）
- **工程**：Keil MDK 5（`Project.uvprojx`），STM32 标准外设库 V3.5.0
- **在板方式**：板上不焊 MCU，通过 **H2/H3 两个 20pin 母座**插一块 STM32 最小系统板
  （电路见 [`../hardware/`](../hardware/)）

---

## 引脚分配

| 功能 | 引脚 | 外设 | 说明 |
|---|---|---|---|
| **PWM 调光** | **PB7** | TIM4_CH2 | 10kHz，经 R1 驱动 Q4（N-MOS）→ 灯串 |
| **蜂鸣器** | **PB6** | GPIO | 提示音（上电软启动完成时响 200ms） |
| **DHT-11 数据** | **PA12** | GPIO 单总线 | 2 秒读一次，失败显示 `ER` |
| **蓝牙串口** | **PA9** (TX) / **PA10** (RX) | USART1 | 9600 8N1，接 HC-05 |
| **OLED** | **PB8** (SCL) / **PB9** (SDA) | 软件 I2C | 0.96" 128×64，4 行显示 |

> 引脚都在源码顶部以宏定义给出（如 `#define DHT11_PIN GPIO_Pin_12`），
> 换板只需要改这几行。

## 串口协议

定长 3 字节，**上位机 → 下位机**：

```
0xAA  [亮度 0~100]  0x55
例：AA 32 55  →  亮度 50%（0x32 = 50）
```

接收用中断 + 三态状态机（`Serial.c`）：非帧头字节一律丢弃，**自动重新同步**，
所以中途丢字节或被噪声打断都能自己恢复，不会卡死。

**下位机 → 上位机**（`\r\n` 结尾）：

```
Light:50%                  收到调光帧后回显实际生效值
Humi:22%RH Temp:28C        每 2 秒上报一次温湿度
DHT11 Read Error           DHT-11 读取失败
```

完整说明见 [`../docs/protocol.md`](../docs/protocol.md)。

## 三个设计要点

**① 全程非阻塞**

`Delay` 模块用 TIM3 做 1ms 节拍（`Delay_GetTick()` / `Delay_Elapsed()`），
主循环里所有周期性动作都是「查时间到了没」而不是 `delay_ms()` 干等：

```c
if (Delay_Elapsed(ReadTick, 2000)) { ... }   // 到点才做，否则立即返回
```

唯一的例外是 DHT-11 读时序本身需要约 25ms 阻塞（单总线协议决定的，无法避免），
所以它每 2 秒才发生一次。

**② PWM 死区 10~90%**

占空比被钳位在 10%~90% 之间：

- 低于 10%：MOS 接近截止，灯几乎不亮，调节无意义
- 高于 90%：MOS 基本全通，失去调光意义且电流不受控

任何入口（上电初始值、串口下发、软启动终点）都会经过同一套钳位，
并把**实际生效值**回传显示——所以 OLED 和上位机看到的永远是真值。

**③ 软启动**

上电不是直接给到目标亮度，而是从 10% 起**每 20ms 升 2%** 爬到目标值
（如目标 50% 约需 400ms），避免 MOS 瞬间全通、灯珠电流突变。
爬到目标时置一个"完成事件"，主循环据此让蜂鸣器响 200ms 提示。
一旦收到上位机的调光帧，软启动立即取消，防止爬升把用户设的值覆盖掉。

## 编译与烧录

1. 用 Keil MDK 5 打开 `Project.uvprojx`（已配好相对路径，直接能编译）
2. ⚠️ **编码设置**：源码统一为 UTF-8，请在
   `Edit → Configuration → Editor → Encoding` 选 **UTF-8**，否则中文注释乱码
3. 编译（F7）→ 用 ST-Link / J-Link 下载（F8），或自己勾选
   `Options for Target → Output → Create HEX File` 后用串口 ISP 烧录
4. 首次上电应看到 OLED 显示 `DeskLamp` 并出现软启动过程，完成时蜂鸣一声

> 烧录工具与固件库版本无关；工程里 `Library/`、`Start/` 是 ST 官方代码，
> 保留原样以便原封编译，**不要改**。

## 文件结构

```
firmware/
├─ Project.uvprojx      # Keil 工程（相对路径，开箱可编译）
├─ keilkill.bat         # 清理编译产物的批处理
├─ DebugConfig/         # Keil 调试器配置（ST 模板自带）
├─ User/
│   ├─ main.c           # 主循环：软启动 / 串口调光 / 温湿度上报
│   ├─ stm32f10x_it.c   # 中断服务（串口接收在此触发）
│   └─ stm32f10x_conf.h # 外设库裁剪配置
├─ Hardware/            # ★ 本项目写的驱动
│   ├─ PWM.c/h          # PB7 TIM4_CH2 调光 + 死区 + 非阻塞软启动
│   ├─ Serial.c/h       # USART1 收发 + 0xAA..0x55 帧状态机
│   ├─ DHT-11.c/h       # 单总线温湿度读取
│   ├─ BEEP.c/h         # 蜂鸣器（触发电平可配置）
│   └─ OLED.c/h + OLED_Font.h   # 软件 I2C OLED 驱动（含字库）
├─ System/Delay.c/h     # µs 延时 + TIM3 非阻塞节拍
├─ Start/               # ST 官方启动文件与 CMSIS（startup_stm32f10x_md.s 等）
└─ Library/             # ST 标准外设库 V3.5.0（MCD-ST Liberty SW License）
```

## 已知事项

- `OLED.c` / `OLED_Font.h` 取自常见的 STM32 OLED 教程模板（软件 I2C 版本），
  非本项目原创，版权归原作者；若要商用请替换为自研驱动或确认授权
- `Library/` 与 `Start/` 为 ST 官方代码，遵循 MCD-ST Liberty SW License，
  见仓库根目录 [NOTICE](../NOTICE)
- 软启动实际步长为**每 20ms 升 2%**（源码行内注释与实现一致）

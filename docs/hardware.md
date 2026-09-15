# 硬件与接线

## 清单

| 部件 | 型号 | 备注 |
|---|---|---|
| 主控板 | Purple Pi OH（RK3566） | 4×A55 @1.8GHz，NPU 0.8TOPS，4G+32G eMMC |
| 屏幕 | HDMI 显示器 / 触屏 | 1920×1080 最佳；1280×800 也能自适应 |
| 摄像头 | MIPI OV5648（ZV-P26-50-VD5.1） | i2c-4 地址 0x36；也可换 USB UVC 免驱摄像头 |
| 蓝牙 | HC-05（串口蓝牙） | 默认 PIN 1234，UART 9600 |
| 下位机 | **STM32F103C8T6 最小系统板** | 不焊在板上，插在拓展板的 H2/H3 两个 20pin 母座上；固件见 [`../firmware/`](../firmware/) |
| 拓展板 | **自绘 Ø98mm 圆形双层板** | 电路/打板文件见 [`../hardware/`](../hardware/) |
| 温湿度 | DHT-11 | 数据脚 → STM32 **PA12** |
| 灯 | LED 灯珠 + Q4（N-MOS） | 由 STM32 **PB7** 输出 PWM 调光（死区 10~90%） |
| 板载显示 | 0.96" OLED（128×64，I2C） | SCL→**PB8** / SDA→**PB9**（软件 I2C） |
| 提示音 | 有源蜂鸣器 | **PB6** 驱动，软启动完成时响 200ms |

> 下位机（MCU + DHT-11 + HC-05 + 灯驱动）的 **PCB 在嘉立创 EDA 上设计**，
> 源工程、原理图 PDF 与 Gerber 见 [`../hardware/`](../hardware/)。

## 接线

**上位机 ⟷ 下位机**走蓝牙，HC-05 挂在 STM32 的串口上：

```
Purple Pi OH ──蓝牙 RFCOMM──► HC-05 ──UART 9600 8N1──► STM32 PA9 (TX) / PA10 (RX)
```

（也可以直接用 USB 转串口接 MCU，端口填 `/dev/ttyUSB0`，
不需要蓝牙时最省事——见 [`protocol.md`](protocol.md) 的端口写法表。）

**拓展板上的连接**由 PCB 走线固定，不需要手工接线：

| STM32 引脚 | 接到 |
|---|---|
| **PB7** | Q4 栅极（经 R1），Q4 漏极驱动灯串 → 10kHz PWM 调光 |
| **PB6** | 蜂鸣器 |
| **PA12** | DHT-11 DATA |
| **PA9 / PA10** | HC-05 的 RXD / TXD（交叉） |
| **PB8 / PB9** | OLED SCL / SDA（软件 I2C） |
| 5V / GND | 电源：DC 座 → F1 → SW1 → VIN → U6（DC-DC 降压）→ 5V |

## 两个硬件经验

1. **天线**：HC-05 板载天线很小，配对/连接不稳定时先贴一片铜箔/外接天线；
   同时**关掉电脑和手机的蓝牙**再扫描，否则干扰明显。
2. **WiFi 与蓝牙共用天线**：本板 WiFi（brcmfmac）与蓝牙共用前端，
   用蓝牙扫描（inquiry）期间 WiFi 会掉。所以桥接脚本走 **RFCOMM 直连固定 MAC**，
   不主动扫描；`docs/troubleshooting.md` 里有相关排查记录。

## 摄像头选择建议

| | MIPI OV5648 | USB UVC |
|---|---|---|
| 免驱 | 需要 rkisp/3A 配置，开机有概率 probe 失败需重绑 | 即插即用 |
| 帧率 | 640×480@15fps | 640×480 MJPG ≈18fps（**必须 MJPG**，YUYV 只有 6fps） |
| 调试成本 | 高 | 低 |

生产/演示建议 USB UVC（`/dev/video9`）；想在板子上直接跑高帧率 MIPI 需处理
3A 服务（本项目实测该镜像只有 v1 XML IQ，服务只认 v2 JSON，画面偏绿但不影响手势）。

## 下位机电路与打板

下位机是**自己画的 PCB**，设计在 **嘉立创 EDA（专业版）** 上完成，走的是
「在线设计 → 一键打板/SMT」那条链路。

| 资料 | 位置 |
|---|---|
| 源工程（`.epro`，可用 EDA 导入继续改） | [`hardware/protaiden-mcu.epro`](../hardware/) |
| 原理图 PDF（不装 EDA 也能看） | [`hardware/schematic.pdf`](../hardware/) |
| Gerber 制板文件 | [`hardware/gerber/`](../hardware/) |
| BOM 物料清单 | [`hardware/bom.csv`](../hardware/) |
| 在线查看 / 复刻 | 立创开源硬件平台（链接见 [`hardware/README.md`](../hardware/README.md)） |

想自己改板的话，把 `.epro` 直接拖进嘉立创 EDA（**文件 → 导入 → 嘉立创EDA(专业版)**）
即可编辑；只想打板就直接把 `hardware/gerber/` 打包上传到任意板厂。

导出方法与许可说明见 [`../hardware/README.md`](../hardware/README.md)。

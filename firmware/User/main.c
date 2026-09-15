#include "stm32f10x.h"                  // Device header（STM32 标准外设库）
#include "Delay.h"                      // 延时：阻塞（µs/ms）+ 非阻塞节拍（TIM3）
#include "OLED.h"                       // OLED 显示驱动（软件 I2C）
#include "Serial.h"                     // 串口1 驱动（温湿度上报 + 调光帧接收）
#include "DHT-11.h"                     // DHT-11 温湿度传感器驱动
#include "PWM.h"                        // LED 调光驱动（PB7 PWM）
#include "BEEP.h"                       // 蜂鸣器驱动（PB6）

uint8_t Hum;		//湿度值（%RH）
uint8_t Temp;		//温度值（℃）
uint8_t Light = 50;	//LED 亮度（0~100，实际由 PWM 死区钳位到 10~90）

uint32_t ReadTick = 0;	//上次读取温湿度的节拍（非阻塞 2s 周期计时）
uint32_t BeepTick = 0;	//蜂鸣器鸣响开始时刻
uint8_t BeepActive = 0;	//蜂鸣器鸣响进行中标志

int main(void)
{
	/* ---------- 模块初始化 ---------- */
	OLED_Init();		//OLED 初始化（软件 I2C）
	Serial_Init();		//串口1 初始化：PA9(TX)/PA10(RX)，9600 波特率
	Delay_Init();		//非阻塞节拍初始化：TIM3 1ms 中断（供 Elapsed 查询计时）
	DHT11_Init();		//DHT-11 初始化：PA12 数据脚，总线空闲拉高
	PWM_Init();			//LED 调光初始化：PB7 输出 10kHz PWM 驱动 MOS，初始 0%
	BEEP_Init();		//蜂鸣器初始化：PB6，初始静音

	/* ---------- OLED 界面绘制 ---------- */
	OLED_ShowString(1, 1, "DeskLamp");	//第 1 行：标题
	OLED_ShowString(2, 1, "Humi:");		//第 2 行：湿度标签
	OLED_ShowString(2, 10, "%");		//第 2 行：湿度单位
	OLED_ShowString(3, 1, "Temp:");		//第 3 行：温度标签
	OLED_ShowString(3, 10, "C");		//第 3 行：温度单位
	OLED_ShowString(4, 1, "Light:");	//第 4 行：亮度标签
	OLED_ShowString(4, 10, "%");		//第 4 行：亮度单位

	/* ---------- 启动 LED 软启动（非阻塞） ---------- */
	PWM_SoftStart(Light);		//记录目标亮度，开始从 10% 爬升
	OLED_ShowNum(4, 7, Light, 2);	//OLED 显示目标亮度

	/* ---------- 主循环（非阻塞） ---------- */
	while (1)
	{
		/* ① 软启动推进：每 20ms 升 2%，未到时刻立即返回 */
		PWM_SoftStart_Tick();

		/* ② 软启动完成事件：蜂鸣器鸣响 200ms，提示可进入调光模式 */
		if (PWM_SoftStartDone())
		{
			BeepActive = 1;				//置鸣响标志
			BeepTick = Delay_GetTick();	//记录鸣响开始时刻
			BEEP_Set(1);				//蜂鸣器鸣响
		}
		if (BeepActive && Delay_Elapsed(BeepTick, 200))
		{
			BeepActive = 0;				//清鸣响标志
			BEEP_Set(0);				//蜂鸣器静音
		}

		/* ③ 串口调光：收到完整帧 0xAA [亮度0~100] 0x55 即更新亮度 */
		if (Serial_GetFrameFlag())
		{
			PWM_StopSoftStart();		//取消软启动，防止爬升覆盖手动设置
			Light = PWM_SetCompare2(Serial_GetFrameData());	//设置亮度并取回实际生效值
			OLED_ShowNum(4, 7, Light, 2);		//OLED 显示实际亮度（死区钳位后）
			Serial_Printf("Light:%d%%\r\n", Light);	//串口回显确认
		}

		/* ④ 温湿度读取：每 2s 一次（查询式，循环不等待） */
		if (Delay_Elapsed(ReadTick, 2000))
		{
			ReadTick = Delay_GetTick();		//刷新计时起点

			/* 读取温湿度（DHT-11 单总线读取本身约 25ms 阻塞，不可避免）：
			   成功则刷新 OLED 并串口上报，失败则显示 ER */
			if (DHT11_ReadData(&Hum, &Temp) == 0)
			{
				OLED_ShowNum(2, 7, Hum, 2);		//刷新湿度显示
				OLED_ShowNum(3, 7, Temp, 2);		//刷新温度显示
				Serial_Printf("Humi:%d%%RH Temp:%dC\r\n", Hum, Temp);	//串口上报
			}
			else
			{
				OLED_ShowString(2, 7, "ER");		//湿度位置显示错误标志
				OLED_ShowString(3, 7, "ER");		//温度位置显示错误标志
				Serial_Printf("DHT11 Read Error\r\n");	//串口上报读取失败
			}
		}
	}
}

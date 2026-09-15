#include "stm32f10x.h"                  // Device header（STM32 标准外设库）
#include "Delay.h"                      // 延时函数（单总线时序计时）

/* DHT-11 数据引脚：PA12 */
#define DHT11_PORT		GPIOA
#define DHT11_PIN		GPIO_Pin_12

/**
  * @brief  数据引脚配置为推挽输出（主机主动驱动总线）
  *         用于发送起始信号：拉低 20ms 唤醒 DHT-11
  * @param  无
  * @retval 无
  */
static void DHT11_SetOutput(void)
{
	GPIO_InitTypeDef GPIO_InitStructure;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;	//推挽输出：可强拉高/强拉低
	GPIO_InitStructure.GPIO_Pin = DHT11_PIN;		//PA12
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;	//50MHz 翻转速率
	GPIO_Init(DHT11_PORT, &GPIO_InitStructure);
}

/**
  * @brief  数据引脚配置为上拉输入（释放总线，接收 DHT-11 数据）
  *         上拉输入保证空闲态总线为高，且裸传感器无需外部上拉电阻
  * @param  无
  * @retval 无
  */
static void DHT11_SetInput(void)
{
	GPIO_InitTypeDef GPIO_InitStructure;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IPU;		//上拉输入：读引脚电平
	GPIO_InitStructure.GPIO_Pin = DHT11_PIN;		//PA12
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;	//输入模式速度字段无实际作用
	GPIO_Init(DHT11_PORT, &GPIO_InitStructure);
}

/**
  * @brief  等待引脚达到指定电平（带超时保护，防止死循环）
  * @param  Level 目标电平（0=低，1=高）；Timeout_us 超时时间（微秒）
  * @retval 0 正常等到目标电平；1 超时
  */
static uint8_t DHT11_WaitLevel(uint8_t Level, uint16_t Timeout_us)
{
	/* 循环轮询引脚电平，直到满足目标电平或超时 */
	while (GPIO_ReadInputDataBit(DHT11_PORT, DHT11_PIN) != Level)
	{
		Delay_us(2);			//每次轮询间隔 2us，同时作为超时计时单位
		if (Timeout_us <= 2)		//超时时间耗尽
		{
			return 1;			//返回超时错误
		}
		Timeout_us -= 2;		//扣除本次轮询消耗的时间
	}
	return 0;				//正常等到目标电平
}

/**
  * @brief  读取 1 位数据
  *         每一位格式：50us 低电平开始，随后高电平 26~28us 表示 0、70us 表示 1
  * @param  无
  * @retval 0/1 数据位；2 超时错误
  */
static uint8_t DHT11_ReadBit(void)
{
	uint16_t Time = 0;		//记录高电平持续时间（us）

	/* 等待 50us 起始低电平结束（低→高跳变） */
	if (DHT11_WaitLevel(1, 100))
	{
		return 2;			//100us 内未等到高电平，超时
	}
	/* 测量数据高电平宽度：循环计数直到引脚变低或超时 */
	while (GPIO_ReadInputDataBit(DHT11_PORT, DHT11_PIN) == 1)
	{
		Delay_us(2);
		Time += 2;			//每 2us 累加一次
		if (Time > 100)			//高电平超过 100us 属于异常
		{
			return 2;		//返回超时错误
		}
	}

	/* 以 40us 为阈值区分 0/1：26~28us 判 0，70us 判 1 */
	return (Time > 40) ? 1 : 0;
}

void DHT11_Init(void)
{
	/* 开启 GPIOA 时钟（PA12 属于 APB2 总线） */
	RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA, ENABLE);

	/* 初始化为输出模式并拉高，保证总线空闲态为高电平 */
	DHT11_SetOutput();
	GPIO_SetBits(DHT11_PORT, DHT11_PIN);	//空闲态：总线拉高
}

/**
  * @brief  读取温湿度（两次读取间隔需 ≥1s，由调用方保证）
  *         数据帧 40 位：湿度整数+湿度小数+温度整数+温度小数+校验和
  * @param  Hum 湿度整数部分（%RH）；Temp 温度整数部分（℃）
  * @retval 0 成功；1 无响应/响应超时；2 数据位超时；3 校验和错误
  */
uint8_t DHT11_ReadData(uint8_t *Hum, uint8_t *Temp)
{
	uint8_t i, Bit;			//循环变量与单 bit 结果
	uint8_t Data[5] = {0};		//40 位数据暂存：每 8 位一字节

	/* ① 主机起始信号：拉低 ≥18ms 唤醒传感器，释放后延时 20~40us */
	DHT11_SetOutput();				//切回输出模式
	GPIO_ResetBits(DHT11_PORT, DHT11_PIN);		//拉低总线
	Delay_ms(20);					//低电平保持 20ms（要求 ≥18ms）
	GPIO_SetBits(DHT11_PORT, DHT11_PIN);		//释放总线，回到高电平
	Delay_us(30);					//延时 20~40us 窗口内，等待 DHT-11 接管总线
	DHT11_SetInput();				//切回输入模式，开始接收数据

	/* ② DHT-11 响应信号：80us 低电平 + 80us 高电平 */
	if (DHT11_WaitLevel(0, 100))		//等待响应低电平出现
	{
		DHT11_SetOutput();			//超时：恢复总线空闲态
		GPIO_SetBits(DHT11_PORT, DHT11_PIN);
		return 1;				//传感器未响应
	}
	if (DHT11_WaitLevel(1, 100))		//等待 80us 低电平结束
	{
		DHT11_SetOutput();
		GPIO_SetBits(DHT11_PORT, DHT11_PIN);
		return 1;
	}
	if (DHT11_WaitLevel(0, 100))		//等待 80us 高电平结束
	{
		DHT11_SetOutput();
		GPIO_SetBits(DHT11_PORT, DHT11_PIN);
		return 1;
	}

	/* ③ 连续接收 40 位数据：逐位拼装进 Data[0]~Data[4] */
	for (i = 0; i < 40; i++)
	{
		Bit = DHT11_ReadBit();			//读取 1 位
		if (Bit == 2)				//数据位超时
		{
			DHT11_SetOutput();
			GPIO_SetBits(DHT11_PORT, DHT11_PIN);
			return 2;
		}
		Data[i / 8] <<= 1;			//左移腾出最低位
		if (Bit == 1)				//该位为 1 则置位
		{
			Data[i / 8] |= 1;
		}
	}

	/* ④ 收完数据后恢复输出模式，总线拉高回到空闲态 */
	DHT11_SetOutput();
	GPIO_SetBits(DHT11_PORT, DHT11_PIN);

	/* ⑤ 校验：校验和 = 前 4 字节之和的低 8 位 */
	if (Data[4] != (uint8_t)(Data[0] + Data[1] + Data[2] + Data[3]))
	{
		return 3;				//校验失败，数据不可信
	}

	/* ⑥ 输出结果：DHT-11 小数部分恒为 0，只取整数部分 */
	*Hum = Data[0];				//湿度整数（%RH）
	*Temp = Data[2];			//温度整数（℃）
	return 0;					//读取成功
}

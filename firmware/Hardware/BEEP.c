#include "stm32f10x.h"                  // Device header（STM32 标准外设库）

/*------------------- 蜂鸣器触发电平配置 -------------------*/
/* 市售"蜂鸣器模块"（带 S8550 三极管）绝大多数是低电平触发：
   I/O 拉低 = 鸣响，拉高 = 静音
   若你的模块是高电平触发（响=拉高），把下面 BEEP_ACTIVE 改为 BEEP_ON_HIGH */
#define BEEP_ON_HIGH	1	//高电平触发：鸣响=输出高
#define BEEP_ON_LOW		0	//低电平触发：鸣响=输出低
#define BEEP_ACTIVE		BEEP_ON_LOW		//← 按实际模块型号改这里

/* 蜂鸣器引脚：PB6（若接在别处，只改下面两行） */
#define BEEP_PORT	GPIOB
#define BEEP_PIN	GPIO_Pin_6

/**
  * @brief  控制蜂鸣器鸣响（电平极性由 BEEP_ACTIVE 自动决定）
  * @param  State 1=鸣响，0=静音
  * @retval 无
  */
void BEEP_Set(uint8_t State)
{
	uint8_t Level;						//要写入引脚的电平：1=高 0=低

	if (State == 1)						//鸣响：输出触发电平
	{
		Level = (BEEP_ACTIVE == BEEP_ON_HIGH) ? 1 : 0;
	}
	else								//静音：输出与触发电平相反
	{
		Level = (BEEP_ACTIVE == BEEP_ON_HIGH) ? 0 : 1;
	}

	GPIO_WriteBit(BEEP_PORT, BEEP_PIN, (BitAction)Level);
}

/**
  * @brief  蜂鸣器初始化：PB6 推挽输出，初始静音
  * @param  无
  * @retval 无
  */
void BEEP_Init(void)
{
	/* 开启 GPIOB 时钟（APB2 总线） */
	RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOB, ENABLE);

	/* 配置 PB6 为推挽输出（直接驱动蜂鸣器模块） */
	GPIO_InitTypeDef GPIO_InitStructure;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;	//推挽输出
	GPIO_InitStructure.GPIO_Pin = BEEP_PIN;			//PB6
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;	//50MHz 翻转速率
	GPIO_Init(BEEP_PORT, &GPIO_InitStructure);

	BEEP_Set(0);						//初始静音（电平极性由配置自动决定）
}

#include "stm32f10x.h"

/**
  * @brief  微秒级延时
  * @param  xus 延时时长，范围：0~233015
  * @retval 无
  */
void Delay_us(uint32_t xus)
{
	SysTick->LOAD = 72 * xus;				//设置定时器重装值
	SysTick->VAL = 0x00;					//清空当前计数值
	SysTick->CTRL = 0x00000005;				//设置时钟源为HCLK，启动定时器
	while(!(SysTick->CTRL & 0x00010000));	//等待计数到0
	SysTick->CTRL = 0x00000004;				//关闭定时器
}

/**
  * @brief  毫秒级延时
  * @param  xms 延时时长，范围：0~4294967295
  * @retval 无
  */
void Delay_ms(uint32_t xms)
{
	while(xms--)
	{
		Delay_us(1000);
	}
}
 
/**
  * @brief  秒级延时
  * @param  xs 延时时长，范围：0~4294967295
  * @retval 无
  */
void Delay_s(uint32_t xs)
{
	while(xs--)
	{
		Delay_ms(1000);
	}
} 

/*====================================================================*/
/* 以下为非阻塞延时：TIM3 产生 1ms 中断节拍，供主循环查询式使用        */
/* 使用独立的 TIM3 而非 SysTick，避免与上方阻塞延时的 SysTick 冲突     */
/*====================================================================*/

static volatile uint32_t Tick;		//1ms 系统节拍计数（中断中累加）

/**
  * @brief  非阻塞延时初始化：TIM3 配置为 1ms 中断
  *         计数频率 = 72MHz / 72 = 1MHz，ARR = 999 → 每 1ms 产生更新中断
  * @param  无
  * @retval 无
  */
void Delay_Init(void)
{
	/* 开启 TIM3 时钟（APB1 总线） */
	RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM3, ENABLE);

	/* 选择内部时钟作为计数时钟源（72MHz） */
	TIM_InternalClockConfig(TIM3);

	/* 时基配置：得到 1ms 的更新事件周期 */
	TIM_TimeBaseInitTypeDef TIM_TimeBaseInitStructure;
	TIM_TimeBaseInitStructure.TIM_ClockDivision = TIM_CKD_DIV1;
	TIM_TimeBaseInitStructure.TIM_CounterMode = TIM_CounterMode_Up;
	TIM_TimeBaseInitStructure.TIM_Period = 1000 - 1;	//ARR = 999，计满 1000 个 1us
	TIM_TimeBaseInitStructure.TIM_Prescaler = 72 - 1;	//PSC = 71，72MHz / 72 = 1MHz
	TIM_TimeBaseInitStructure.TIM_RepetitionCounter = 0;
	TIM_TimeBaseInit(TIM3, &TIM_TimeBaseInitStructure);

	/* 使能更新中断并配置 NVIC（优先级低于串口，避免影响串口收发） */
	TIM_ITConfig(TIM3, TIM_IT_Update, ENABLE);
	NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);
	NVIC_InitTypeDef NVIC_InitStructure;
	NVIC_InitStructure.NVIC_IRQChannel = TIM3_IRQn;
	NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
	NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 2;
	NVIC_InitStructure.NVIC_IRQChannelSubPriority = 2;
	NVIC_Init(&NVIC_InitStructure);

	TIM_Cmd(TIM3, ENABLE);			//启动定时器，开始产生 1ms 节拍
}

/**
  * @brief  获取当前系统节拍（非阻塞，无任何等待）
  * @param  无
  * @retval 自初始化以来的毫秒数（uint32_t，约 49.7 天回绕一次）
  */
uint32_t Delay_GetTick(void)
{
	return Tick;
}

/**
  * @brief  判断自 Start 时刻起是否已过去 Ms 毫秒（非阻塞查询）
  * @param  Start 起始节拍（由 Delay_GetTick 获取）；Ms 等待时长（毫秒）
  * @retval 0 未到时间；1 时间已到
  */
uint8_t Delay_Elapsed(uint32_t Start, uint32_t Ms)
{
	/* 无符号减法：即使 Tick 回绕也能正确计算差值 */
	return (Delay_GetTick() - Start) >= Ms;
}

/**
  * @brief  TIM3 更新中断服务函数：每 1ms 累加一次节拍
  * @param  无
  * @retval 无
  */
void TIM3_IRQHandler(void)
{
	if (TIM_GetITStatus(TIM3, TIM_IT_Update) == SET)
	{
		Tick++;
		TIM_ClearITPendingBit(TIM3, TIM_IT_Update);
	}
}

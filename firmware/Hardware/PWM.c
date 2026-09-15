#include "stm32f10x.h"                  // Device header（STM32 标准外设库）
#include "Delay.h"                      // 非阻塞节拍（软启动状态机计时用）

/*------------------- 占空比死区定义 -------------------*/
/* 限制占空比只允许在 10%~90% 之间调节，防止以下两种情况：
   1. 占空比 = 0% ：MOS 完全截止，LED 全灭（低于调光有效区间）
   2. 占空比 = 100%：MOS 完全导通，LED 全亮（失去 PWM 调光意义，电流不受控） */
#define PWM_DUTY_MIN	10
#define PWM_DUTY_MAX	90

/**
  * @brief  PWM 初始化：PB7 = TIM4_CH2（默认映射，无需重映射）
  *         频率 = 72MHz / PSC(72) / ARR(100) = 10kHz
  *         10kHz：高于人眼闪烁感知频率，且不在人耳可闻频段内
  * @param  无
  * @retval 无
  */
void PWM_Init(void)
{
	/* 开启外设时钟：TIM4 在 APB1 总线，GPIOB 在 APB2 总线 */
	RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);
	RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOB, ENABLE);

	/* 配置 PB7 为复用推挽输出（引脚控制权交给 TIM4_CH2） */
	GPIO_InitTypeDef GPIO_InitStructure;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;	//复用推挽：引脚由定时器外设驱动
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_7;		//PB7 = TIM4_CH2 输出脚
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;	//50MHz 翻转速率，满足 PWM 边沿需求
	GPIO_Init(GPIOB, &GPIO_InitStructure);

	/* 选择 TIM4 内部时钟作为计数时钟源（72MHz） */
	TIM_InternalClockConfig(TIM4);

	/* 时基配置：决定 PWM 频率与占空比分辨率 */
	TIM_TimeBaseInitTypeDef TIM_TimeBaseInitStructure;
	TIM_TimeBaseInitStructure.TIM_ClockDivision = TIM_CKD_DIV1;		//时钟不分频
	TIM_TimeBaseInitStructure.TIM_CounterMode = TIM_CounterMode_Up;		//向上计数模式
	TIM_TimeBaseInitStructure.TIM_Period = 100 - 1;		//ARR = 99，计满 100 个数溢出一次
	TIM_TimeBaseInitStructure.TIM_Prescaler = 72 - 1;	//PSC = 71，72MHz / 72 = 1MHz 计数频率
	TIM_TimeBaseInitStructure.TIM_RepetitionCounter = 0;	//重复计数（高级定时器专用，此处无效）
	TIM_TimeBaseInit(TIM4, &TIM_TimeBaseInitStructure);

	/* 输出比较配置：CCR 与计数器比较产生 PWM 波形 */
	TIM_OCInitTypeDef TIM_OCInitStructure;
	TIM_OCStructInit(&TIM_OCInitStructure);			//先填入库默认值，再覆盖需要的字段
	TIM_OCInitStructure.TIM_OCMode = TIM_OCMode_PWM1;	//PWM 模式1：CNT < CCR 时输出有效电平
	TIM_OCInitStructure.TIM_OCPolarity = TIM_OCPolarity_High;	//有效电平为高（高占空比 = 高亮度）
	TIM_OCInitStructure.TIM_OutputState = TIM_OutputState_Enable;	//使能该通道输出
	TIM_OCInitStructure.TIM_Pulse = 0;			//CCR = 0，初始占空比 0%（上电先灭灯，等待软启动）
	TIM_OC2Init(TIM4, &TIM_OCInitStructure);		//通道2 初始化：对应 PB7

	TIM_Cmd(TIM4, ENABLE);		//启动定时器，PB7 开始持续输出 PWM
}

/**
  * @brief  设置 LED 亮度（带死区限制）
  * @param  Compare 目标占空比（0~100），内部自动钳位到死区 10~90
  * @retval 无
  */
uint16_t PWM_SetCompare2(uint16_t Compare)
{
	/* 死区钳位：超出范围的值直接截断到边界，保证任何调用都合法 */
	if (Compare > PWM_DUTY_MAX)	Compare = PWM_DUTY_MAX;		//上限 90%
	if (Compare < PWM_DUTY_MIN)	Compare = PWM_DUTY_MIN;		//下限 10%

	TIM_SetCompare2(TIM4, Compare);		//写入 CCR 寄存器，占空比立即生效
	return Compare;						//返回实际生效的占空比（供显示用）
}

/* 软启动状态机变量（非阻塞：由主循环调用 PWM_SoftStart_Tick 推进） */
static uint16_t SoftStartDuty;		//当前已爬升到的占空比
static uint16_t SoftStartTarget;	//目标占空比
static uint32_t SoftStartTick;		//上次推进的时刻（节拍）
static uint8_t SoftStartActive;		//软启动进行中标志（1=进行中）
static uint8_t SoftStartDone;		//软启动完成事件标志（边沿触发，查询后清除）

/**
  * @brief  启动 LED 软启动（非阻塞）：占空比从死区下限逐级爬升到目标值
  *         只记录目标与起点，实际爬升由 PWM_SoftStart_Tick() 周期性推进，
  *         避免上电瞬间 MOS 全导通、LED 电流突变冲击灯珠
  * @param  Target 目标占空比（0~100），内部自动钳位到死区
  * @retval 无
  */
void PWM_SoftStart(uint16_t Target)
{
	/* 目标值同样做死区钳位，保证爬升终点合法 */
	if (Target > PWM_DUTY_MAX)	Target = PWM_DUTY_MAX;		//上限 90%
	if (Target < PWM_DUTY_MIN)	Target = PWM_DUTY_MIN;		//下限 10%

	SoftStartTarget = Target;			//记录目标占空比
	SoftStartDuty = PWM_DUTY_MIN;		//从死区下限 10% 起步
	SoftStartTick = Delay_GetTick();	//记录当前节拍作为起始时刻
	SoftStartActive = 1;				//置软启动进行中标志
	SoftStartDone = 0;					//清除历史完成事件
	PWM_SetCompare2(SoftStartDuty);		//立即输出起点占空比
}

/**
  * @brief  软启动推进函数（非阻塞，需在主循环中周期调用）
  *         每 20ms 上升 2%，未到时刻立即返回，不占用 CPU 等待
  * @param  无
  * @retval 无
  */
void PWM_SoftStart_Tick(void)
{
	/* 软启动未进行或已完成则直接返回 */
	if (!SoftStartActive)
	{
		return;
	}

	/* 查询式判断：距上次推进是否已满 20ms */
	if (Delay_Elapsed(SoftStartTick, 20))
	{
		SoftStartTick = Delay_GetTick();	//刷新推进时刻

		if (SoftStartDuty < SoftStartTarget)
		{
			SoftStartDuty+=2;				//占空比升百分之2
			PWM_SetCompare2(SoftStartDuty);	//写入 CCR，亮度上升一格
		}
		else
		{
			SoftStartActive = 0;			//达到目标，软启动结束
			SoftStartDone = 1;				//置完成事件，通知外部（如蜂鸣器提示）
		}
	}
}

/**
  * @brief  取消软启动（手动调光时调用，防止爬升覆盖用户设置）
  * @param  无
  * @retval 无
  */
void PWM_StopSoftStart(void)
{
	SoftStartActive = 0;	//结束爬升
	SoftStartDone = 0;		//同时清除完成事件，避免误触发提示
}

/**
  * @brief  查询软启动完成事件（边沿触发：每次完成后只返回一次 1）
  * @param  无
  * @retval 1 软启动刚完成；0 无事件
  */
uint8_t PWM_SoftStartDone(void)
{
	if (SoftStartDone == 1)
	{
		SoftStartDone = 0;	//查询后自动清除
		return 1;
	}
	return 0;
}

#ifndef __DELAY_H
#define __DELAY_H

void Delay_us(uint32_t us);	//阻塞延时：微秒
void Delay_ms(uint32_t ms);	//阻塞延时：毫秒
void Delay_s(uint32_t s);	//阻塞延时：秒

void Delay_Init(void);			//非阻塞节拍初始化（TIM3，1ms 中断）
uint32_t Delay_GetTick(void);		//获取当前节拍（毫秒，非阻塞）
uint8_t Delay_Elapsed(uint32_t Start, uint32_t Ms);	//查询自 Start 起是否已过 Ms 毫秒（非阻塞）

#endif

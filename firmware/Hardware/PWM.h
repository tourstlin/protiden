#ifndef __PWM_H
#define __PWM_H

void PWM_Init(void);			//PWM 初始化（PB7 = TIM4_CH2，10kHz）
uint16_t PWM_SetCompare2(uint16_t Compare);	//设置亮度：死区钳位 10~90，返回实际生效值
void PWM_SoftStart(uint16_t Target);	//启动软启动（非阻塞）：记录目标，从 10% 起步
void PWM_SoftStart_Tick(void);		//软启动推进（非阻塞）：主循环周期调用，每 20ms 升 2%
void PWM_StopSoftStart(void);		//取消软启动（手动调光时调用，防爬升覆盖）
uint8_t PWM_SoftStartDone(void);	//查询软启动完成事件（边沿触发，查询后清除）

#endif

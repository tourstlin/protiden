#ifndef __BEEP_H
#define __BEEP_H

void BEEP_Init(void);			//蜂鸣器初始化（PB6，初始静音）
void BEEP_Set(uint8_t State);		//控制蜂鸣器：1=鸣响，0=静音

#endif

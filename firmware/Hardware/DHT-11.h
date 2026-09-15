#ifndef __DHT11_H
#define __DHT11_H

void DHT11_Init(void);				//DHT-11 初始化（PA12，总线空闲拉高）
uint8_t DHT11_ReadData(uint8_t *Hum, uint8_t *Temp);	//读取温湿度，返回 0 成功 / 1~3 错误码

#endif

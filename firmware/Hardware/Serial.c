#include "stm32f10x.h"                  // Device header
#include <stdio.h>
#include <stdarg.h>

volatile uint8_t Serial_RxData;			//中断写、主循环读：volatile 防止优化后读取被缓存进寄存器
volatile uint8_t Serial_RxFlag;

/*------------------- 调光帧协议定义 -------------------*/
/* 上位机下发定长帧：0xAA [亮度0~100] 0x55
   例：AA 32 55 = 亮度50%（0x32 = 50） */
#define SERIAL_FRAME_HEAD	0xAA	//帧头
#define SERIAL_FRAME_TAIL	0x55	//帧尾

static uint8_t Serial_RxState;		//帧接收状态机：0=等帧头 1=收数据 2=等帧尾（仅中断内访问，无需 volatile）
static volatile uint8_t Serial_FrameData;	//帧中携带的亮度数据（中断写、主循环读）
static volatile uint8_t Serial_FrameFlag;	//完整帧接收完成标志（中断置位、主循环查询清除）

/**
  * @brief  帧接收状态机：逐字节喂入，拼装完整调光帧
  *         非帧头字节一律丢弃（自动重新同步，抗噪声干扰）
  * @param  Byte 新接收到的字节
  * @retval 无
  */
static void Serial_FrameProcess(uint8_t Byte)
{
	switch (Serial_RxState)
	{
	case 0:							//等待帧头
		if (Byte == SERIAL_FRAME_HEAD)
		{
			Serial_RxState = 1;		//收到帧头，进入数据字节状态
		}
		break;
	case 1:							//接收数据字节
		Serial_FrameData = Byte;	//暂存亮度数据
		Serial_RxState = 2;			//进入等待帧尾状态
		break;
	case 2:							//等待帧尾
		if (Byte == SERIAL_FRAME_TAIL)
		{
			Serial_FrameFlag = 1;	//帧完整，置接收完成标志
		}
		Serial_RxState = 0;			//无论对错，都回到等待帧头（重新同步）
		break;
	}
}

uint8_t Serial_GetFrameFlag(void)
{
	if (Serial_FrameFlag == 1)
	{
		Serial_FrameFlag = 0;		//查询后自动清除
		return 1;
	}
	return 0;
}

uint8_t Serial_GetFrameData(void)
{
	return Serial_FrameData;
}

void Serial_Init(void)
{
	RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE);
	RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA, ENABLE);
	
	GPIO_InitTypeDef GPIO_InitStructure;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_9;
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
	GPIO_Init(GPIOA, &GPIO_InitStructure);
	
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IPU;
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_10;
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
	GPIO_Init(GPIOA, &GPIO_InitStructure);
	
	USART_InitTypeDef USART_InitStructure;
	USART_InitStructure.USART_BaudRate = 9600;
	USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
	USART_InitStructure.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;
	USART_InitStructure.USART_Parity = USART_Parity_No;
	USART_InitStructure.USART_StopBits = USART_StopBits_1;
	USART_InitStructure.USART_WordLength = USART_WordLength_8b;
	USART_Init(USART1, &USART_InitStructure);
	
	USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
	
	NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);
	
	NVIC_InitTypeDef NVIC_InitStructure;
	NVIC_InitStructure.NVIC_IRQChannel = USART1_IRQn;
	NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
	NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 1;
	NVIC_InitStructure.NVIC_IRQChannelSubPriority = 1;
	NVIC_Init(&NVIC_InitStructure);
	
	USART_Cmd(USART1, ENABLE);
}

void Serial_SendByte(uint8_t Byte)
{
	USART_SendData(USART1, Byte);
	while (USART_GetFlagStatus(USART1, USART_FLAG_TXE) == RESET);
}

void Serial_SendArray(uint8_t *Array, uint16_t Length)
{
	uint16_t i;
	for (i = 0; i < Length; i ++)
	{
		Serial_SendByte(Array[i]);
	}
}

void Serial_SendString(char *String)
{
	uint8_t i;
	for (i = 0; String[i] != '\0'; i ++)
	{
		Serial_SendByte(String[i]);
	}
}

uint32_t Serial_Pow(uint32_t X, uint32_t Y)
{
	uint32_t Result = 1;
	while (Y --)
	{
		Result *= X;
	}
	return Result;
}

void Serial_SendNumber(uint32_t Number, uint8_t Length)
{
	uint8_t i;
	for (i = 0; i < Length; i ++)
	{
		Serial_SendByte(Number / Serial_Pow(10, Length - i - 1) % 10 + '0');
	}
}

int fputc(int ch, FILE *f)
{
	Serial_SendByte(ch);
	return ch;
}

void Serial_Printf(char *format, ...)
{
	char String[100];
	va_list arg;
	va_start(arg, format);
	vsnprintf(String, sizeof(String), format, arg);	//限长写入：超长自动截断，防止栈溢出（工程已开 C99）
	va_end(arg);
	Serial_SendString(String);
}

uint8_t Serial_GetRxFlag(void)
{
	if (Serial_RxFlag == 1)
	{
		Serial_RxFlag = 0;
		return 1;
	}
	return 0;
}

uint8_t Serial_GetRxData(void)
{
	return Serial_RxData;
}

void USART1_IRQHandler(void)
{
	if (USART_GetITStatus(USART1, USART_IT_RXNE) == SET)
	{
		Serial_RxData = USART_ReceiveData(USART1);
		Serial_RxFlag = 1;
		Serial_FrameProcess(Serial_RxData);	//喂入帧状态机，拼装调光帧
		USART_ClearITPendingBit(USART1, USART_IT_RXNE);
	}
}

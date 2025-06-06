# ESP32智能语音对话系统

## 项目概述

这是一个基于ESP32的智能语音对话系统，支持语音录制、识别、大语言模型处理、语音合成和播放。系统采用分布式微服务架构，ESP32作为前端负责音频采集和播放，后端由多个独立的Python服务组成。

## 系统架构

```
ESP32 (wifi.ino) ←→ 后端服务群 ←→ 外部API服务
```

## 后端服务文件说明

### 核心服务文件

#### `main.py` - 主控制器服务
- **端口**: 8000
- **作用**: 系统核心调度器，协调各个子服务之间的通信
- **功能**:
  - 接收音频处理请求
  - 调度ASR、LLM、TTS服务
  - 管理会话状态和中断机制
  - 提供系统健康检查

#### `input_service.py` - 录音输入服务
- **端口**: 5001 (Socket) + 8087 (HTTP)
- **作用**: 接收ESP32发送的录音数据
- **功能**:
  - 监听ESP32的Socket连接
  - 接收PCM音频流并转换为MP3
  - 提取或生成MAC地址标识
  - 将音频文件信息发送给主控制器

#### `asr_service.py` - 语音识别服务
- **端口**: 8081
- **作用**: 将音频转换为文本
- **功能**:
  - 使用FunASR模型进行语音识别
  - 支持多种音频格式
  - 提供实时识别能力

#### `llm_service.py` - 大语言模型服务
- **端口**: 8082
- **作用**: 基于识别文本生成智能回复
- **功能**:
  - 连接Dify平台API
  - 流式输出回复内容
  - 支持上下文对话

#### `tts_service.py` - 语音合成服务
- **端口**: 8085
- **作用**: 将文本转换为语音
- **功能**:
  - 使用火山引擎TTS API
  - 生成高质量语音文件
  - 支持多种音色配置

#### `upload_service.py` - 音频上传服务
- **端口**: 8086 (HTTP) + 5002 (Socket to ESP32)
- **作用**: 将生成的语音文件传输到ESP32
- **功能**:
  - 管理音频文件上传队列
  - 与ESP32建立Socket连接
  - 支持上传中断和重试

### 辅助服务文件

#### `recive_ble.py` - BLE设备扫描服务
- **作用**: 扫描并处理BLE iBeacon设备
- **功能**:
  - 持续扫描BLE设备
  - 解析iBeacon广播数据
  - 向Flask服务发送设备信息

#### `start_services.bat` - 服务启动脚本
- **作用**: 按正确顺序启动所有服务
- **功能**:
  - 检查并关闭占用端口
  - 依次启动各个服务
  - 进行健康检查

## 完整工作流程

### 1. 系统初始化
```
1. 运行 start_services.bat
2. 依次启动：ASR → LLM → TTS → Upload → Input → Main 服务
3. ESP32连接WiFi并启动BLE广播
4. 建立与input_service的连接准备
```

### 2. 录音阶段
```
用户按下ESP32按钮
    ↓
ESP32开始录音(I2S麦克风)
    ↓
实时发送PCM音频流到input_service (Socket端口5001)
    ↓
input_service接收并缓存音频数据
    ↓
用户松开按钮，ESP32发送结束标记
    ↓
input_service将音频转换为MP3并保存
```

### 3. 语音处理阶段
```
input_service发送音频文件信息到main.py
    ↓
main.py读取音频文件并调用asr_service
    ↓
asr_service返回识别的文本内容
    ↓
main.py将文本发送给llm_service
    ↓
llm_service流式返回AI回复内容
    ↓
main.py逐句调用tts_service生成语音
    ↓
每个语音片段保存为WAV文件
```

### 4. 音频播放阶段
```
main.py调用upload_service上传语音文件
    ↓
upload_service与ESP32建立Socket连接(端口5002)
    ↓
将WAV文件转换为PCM格式流式传输
    ↓
ESP32接收PCM数据并通过I2S扬声器播放
    ↓
播放完成后删除临时文件
```

### 5. 中断处理
```
用户在播放期间按下按钮
    ↓
ESP32发送中断信号给upload_service
    ↓
立即停止当前播放并开始新的录音
    ↓
重新进入录音处理流程
```

## ESP32配置 (wifi.ino)

### 必须修改的配置项

#### 1. WiFi连接配置
```cpp
// 第44-45行
const char* ssid = "你的WiFi名称";           // 修改为实际WiFi名称
const char* password = "你的WiFi密码";        // 修改为实际WiFi密码
```

#### 2. 服务器IP配置
```cpp
// 第46行
const char* pcIP = "192.168.18.37";        // 修改为运行后端服务的PC IP地址
```

#### 3. 端口配置（通常不需要修改）
```cpp
// 第47-48行
const int pcPort = 5001;                   // input_service的Socket端口
const int receivePort = 5002;              // ESP32接收TTS的端口
```

### 可选配置项

#### 1. I2S引脚配置
```cpp
// 第4-11行 - 根据实际硬件连接修改
#define BUTTON_PIN 32                      // 按钮引脚

// I2S 麦克风引脚
#define I2S_MIC_SD 15
#define I2S_MIC_WS 19  
#define I2S_MIC_SCK 21

// I2S 扬声器引脚
#define I2S_SPK_DOUT 14
#define I2S_SPK_BCLK 12
#define I2S_SPK_LRC 13
```

#### 2. BLE iBeacon配置
```cpp
// 第306-319行 - 可修改iBeacon的UUID、Major、Minor值
uint8_t beaconData[25] = {
    0x4C, 0x00,             // Apple 公司 ID
    0x02, 0x15,             // iBeacon 类型
    // UUID: 可修改为自定义UUID
    0x12, 0x34, 0x56, 0x78, 0x12, 0x34, 0x12, 0x34,
    0x12, 0x34, 0x12, 0x34, 0x56, 0x78, 0x90, 0xAB,
    0x03, 0xE9,             // Major = 1001 (可修改)
    0x00, 0x2A,             // Minor = 42 (可修改)
    0xC5                    // Tx Power = -59 dBm
};
```

## 快速部署指南

### 1. 后端部署
```bash
# 1. 安装Python依赖
pip install -r requirements.txt

# 2. 配置各服务的API密钥（如需要）
# - asr_service.py 中的FunASR配置
# - llm_service.py 中的Dify API配置  
# - tts_service.py 中的火山引擎API配置

# 3. 启动所有服务
start_services.bat
```

### 2. ESP32部署
```
1. 安装Arduino IDE和ESP32开发板包
2. 安装所需库：WiFi、SPIFFS、BLEDevice等
3. 修改wifi.ino中的WiFi和IP配置
4. 编译并上传到ESP32
5. 通过串口监视器查看运行状态
```

### 3. 验证部署
```
1. 检查所有后端服务健康状态：http://localhost:8000/health
2. ESP32连接WiFi成功，获取IP地址
3. 按下ESP32按钮测试录音和播放功能
4. 观察各服务日志确认数据流转正常
```

## 故障排除

### 常见问题

1. **400 Bad Request错误**
   - 检查MAC地址格式是否正确
   - 确认网络连接稳定

2. **503 Service Unavailable错误**
   - 检查相关服务是否正常启动
   - 验证服务间网络连通性

3. **ESP32连接失败**
   - 确认WiFi账号密码正确
   - 检查服务器IP地址配置
   - 验证防火墙设置

4. **音频质量问题**
   - 检查I2S引脚连接
   - 调整音频采样参数
   - 验证硬件工作状态

## 技术特性

- **分布式架构**: 各服务独立运行，便于维护和扩展
- **实时流处理**: 支持音频流实时传输和处理
- **中断机制**: 支持播放过程中的即时中断
- **错误恢复**: 完善的错误处理和重试机制
- **健康监控**: 提供服务状态监控和诊断
- **BLE支持**: 集成蓝牙iBeacon广播功能
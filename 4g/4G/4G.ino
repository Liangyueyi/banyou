#include <HardwareSerial.h>
#include <SPIFFS.h>
#include "driver/i2s.h"
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEAdvertising.h>

#define MODEM_RX 33  // ESP32 接收引脚（接模块 TXD）
#define MODEM_TX 23  // ESP32 发送引脚（接模块 RXD）
#define BAUD_RATE 115200

HardwareSerial modemSerial(1);

// 通用发送 AT 命令并打印返回
void sendATCommand(const char* cmd) {
  modemSerial.println(cmd);
  Serial.print("发送: ");
  Serial.println(cmd);
  delay(500);
  while (modemSerial.available()) {
    Serial.write(modemSerial.read());
  }
  Serial.println();
}

// 等待指定响应字符串 - 修复版，更robust的匹配
bool waitForResponse(const char* expected, unsigned long timeout) {
  unsigned long start = millis();
  String resp = "";
  
  Serial.println("等待响应: " + String(expected));
  
  while (millis() - start < timeout) {
    while (modemSerial.available()) {
      char c = modemSerial.read();
      resp += c;
      Serial.print(c);
      
      // 检查是否找到期望的响应
      if (resp.indexOf(expected) != -1) {
        Serial.println("\n✅ 找到期望响应: " + String(expected));
        Serial.println("完整响应内容: " + resp);
        
        // 等待并显示所有剩余数据，但不影响返回值
        unsigned long extraStart = millis();
        while (millis() - extraStart < 2000) { // 等待2秒显示完整响应
          while (modemSerial.available()) {
            char extra = modemSerial.read();
            Serial.print(extra);
          }
          delay(10);
        }
        
        Serial.println("\n🎯 连接响应处理完成，返回成功");
        return true;
      }
    }
    delay(10); // 小延迟避免CPU占用过高
  }
  
  Serial.println("\n❌ 超时未收到期望响应: " + String(expected));
  Serial.println("实际收到的内容: " + resp);
  return false;
}

// 打印串口所有返回
void readAllAvailable(unsigned long timeout) {
  unsigned long start = millis();
  while (millis() - start < timeout) {
    while (modemSerial.available()) {
      Serial.write(modemSerial.read());
    }
  }
}

#define BUTTON_PIN 32

// I2S 麦克风（录音）
#define I2S_MIC_SD 15
#define I2S_MIC_WS 19
#define I2S_MIC_SCK 21

// I2S 扬声器（播放）
#define I2S_SPK_DOUT 14
#define I2S_SPK_BCLK 12
#define I2S_SPK_LRC 13

// 服务器信息 - 4G版本使用公网IP和新端口
const char* serverIP = "124.223.102.137";  // 公网服务器IP（请修改为实际公网IP）
const int sessionPort = 8083;  // input_service端口（录音上传）
const int apiPort = 8000;  // main_service端口（API调用）

bool isRecording = false;
bool isPlaying = false;
volatile bool needInterrupt = false;
bool is4GReady = false;  // 改名：4G网络就绪（但未建立TCP连接）
bool isConnected = false;  // 新增：TCP连接状态
bool isPollingActive = false;  // 标记是否激活轮询状态
unsigned long pollingStartTime = 0;  // 轮询开始时间
const unsigned long POLLING_TIMEOUT = 60000;  // 60秒超时
const unsigned long POLLING_INTERVAL = 2000;   // 2秒轮询间隔

// I2S 配置
i2s_config_t i2s_mic_config = {
  .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
  .sample_rate = 16000,
  .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
  .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
  .communication_format = I2S_COMM_FORMAT_STAND_I2S,
  .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
  .dma_buf_count = 8,
  .dma_buf_len = 512,
  .use_apll = false,
  .tx_desc_auto_clear = false,
  .fixed_mclk = 0,
  .mclk_multiple = I2S_MCLK_MULTIPLE_256,
  .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
};

i2s_config_t i2s_spk_config = {
  .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
  .sample_rate = 16000,
  .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
  .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
  .communication_format = I2S_COMM_FORMAT_STAND_I2S,
  .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
  .dma_buf_count = 8,
  .dma_buf_len = 512,
  .use_apll = false,
  .tx_desc_auto_clear = true,
  .fixed_mclk = 0,
  .mclk_multiple = I2S_MCLK_MULTIPLE_256,
  .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
};

i2s_pin_config_t mic_pins = {
  .mck_io_num = I2S_PIN_NO_CHANGE,
  .bck_io_num = I2S_MIC_SCK,
  .ws_io_num = I2S_MIC_WS,
  .data_out_num = I2S_PIN_NO_CHANGE,
  .data_in_num = I2S_MIC_SD
};

i2s_pin_config_t spk_pins = {
  .mck_io_num = I2S_PIN_NO_CHANGE,
  .bck_io_num = I2S_SPK_BCLK,
  .ws_io_num = I2S_SPK_LRC,
  .data_out_num = I2S_SPK_DOUT,
  .data_in_num = I2S_PIN_NO_CHANGE
};

// BLE相关变量
BLEAdvertising* pAdvertising;
TaskHandle_t bleTaskHandle = NULL;
bool bleEnabled = true;

String getMACAddress() {
  uint64_t chipId = ESP.getEfuseMac();
  uint8_t mac[6];
  mac[0] = 0x02;
  mac[1] = (chipId >> 32) & 0xFF;
  mac[2] = (chipId >> 24) & 0xFF;
  mac[3] = (chipId >> 16) & 0xFF;
  mac[4] = (chipId >> 8) & 0xFF;
  mac[5] = chipId & 0xFF;
  
  char macStr[18] = { 0 };
  sprintf(macStr, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(macStr);
}

void setupI2S() {
  // 设置麦克风I2S
  esp_err_t result = i2s_driver_install(I2S_NUM_0, &i2s_mic_config, 0, NULL);
  if (result == ESP_OK) {
    Serial.println("✅ 麦克风I2S驱动安装成功");
  } else {
    Serial.printf("❌ 麦克风I2S驱动安装失败, 错误码: %d\n", result);
  }
  
  result = i2s_set_pin(I2S_NUM_0, &mic_pins);
  if (result == ESP_OK) {
    Serial.println("✅ 麦克风I2S引脚设置成功");
  } else {
    Serial.printf("❌ 麦克风I2S引脚设置失败, 错误码: %d\n", result);
  }
  
  // 设置扬声器I2S
  esp_err_t spkResult = i2s_driver_install(I2S_NUM_1, &i2s_spk_config, 0, NULL);
  if (spkResult == ESP_OK) {
    Serial.println("✅ 扬声器I2S驱动安装成功");
  } else {
    Serial.printf("❌ 扬声器I2S驱动安装失败, 错误码: %d\n", spkResult);
  }
  
  spkResult = i2s_set_pin(I2S_NUM_1, &spk_pins);
  if (spkResult == ESP_OK) {
    Serial.println("✅ 扬声器I2S引脚设置成功");
  } else {
    Serial.printf("❌ 扬声器I2S引脚设置失败, 错误码: %d\n", spkResult);
  }
}

// 4G模块基础初始化 - 只做网络检查，不建立TCP连接
bool init4GNetwork() {
  Serial.println("🔧 开始初始化4G网络...");
  
  // 基础4G模块初始化 AT 指令序列
  sendATCommand("AT"); // 测试模块是否响应
  sendATCommand("ATE0"); // 关闭命令回显
  sendATCommand("AT+CPIN?"); // 查询 SIM 卡状态
  sendATCommand("AT+CEREG?"); // 查询网络注册状态
  sendATCommand("AT+CSQ"); // 查询信号强度
  sendATCommand("AT+CGSN"); // 查询模块序列号（IMEI）
  sendATCommand("AT+COPS?"); // 查询当前运营商
  sendATCommand("AT+CGPADDR"); // 查询PDP激活状态及分配的IP地址
  
  Serial.println("✅ 4G网络初始化完成 - 待用状态");
  is4GReady = true;
  return true;
}

// 建立单一TCP连接 - 简化版，只连接input_service
bool establishConnections() {
  if (!is4GReady) {
    Serial.println("❌ 4G网络未就绪，无法建立连接");
    return false;
  }
  
  Serial.println("🔗 建立TCP连接（单连接模式）...");
  
  // 先关闭可能存在的旧连接
  Serial.println("🧹 清理旧连接...");
  sendATCommand("AT+MIPCLOSE=0");
  delay(500);
  
  // 只建立到input_service的TCP连接 (Socket 0)
  Serial.println("🔗 连接到 input_service...");
  String connectCmd0 = "AT+MIPOPEN=0,\"TCP\",\"" + String(serverIP) + "\"," + String(sessionPort);
  
  // 直接发送命令并等待响应，不使用sendATCommand
  modemSerial.println(connectCmd0);
  Serial.println("发送: " + connectCmd0);
  
  if (!waitForResponse("+MIPOPEN: 0,0", 20000)) { // 增加到20秒
    Serial.println("❌ input_service连接失败");
    return false;
  }
  
  // 短暂延迟确保连接稳定
  delay(1000);
  
  Serial.println("✅ TCP连接建立成功（单连接模式）");
  isConnected = true;
  
  // 立即发送会话开始信号，避免服务器端超时断开连接
  Serial.println("🚀 立即发送会话开始信号...");
  sendSessionStart();
  
  return true;
}

// 建立轮询连接 - 录音完成后调用
bool establishPollingConnection() {
  if (!is4GReady) {
    Serial.println("❌ 4G网络未就绪，无法建立轮询连接");
    return false;
  }
  
  // 先关闭录音连接
  Serial.println("🔌 关闭录音连接...");
  sendATCommand("AT+MIPCLOSE=0");
  delay(1000);
  
  // 建立到main_service的TCP连接 (Socket 1) 用于轮询
  Serial.println("🔗 建立轮询连接到 main_service...");
  String connectCmd1 = "AT+MIPOPEN=1,\"TCP\",\"" + String(serverIP) + "\"," + String(apiPort);
  
  // 直接发送命令并等待响应
  modemSerial.println(connectCmd1);
  Serial.println("发送: " + connectCmd1);
  
  if (!waitForResponse("+MIPOPEN: 1,0", 20000)) {
    Serial.println("❌ main_service轮询连接失败");
    isConnected = false;
    return false;
  }
  
  // 短暂延迟确保连接稳定
  delay(1000);
  
  Serial.println("✅ 轮询连接建立成功");
  return true;
}

// 断开TCP连接 - 会话结束时调用
void closeConnections() {
  if (!isConnected) return;
  
  Serial.println("🔌 断开TCP连接...");
  
  // 关闭Socket 0 (input_service)
  sendATCommand("AT+MIPCLOSE=0");
  delay(1000);
  
  // 关闭Socket 1 (main_service)  
  sendATCommand("AT+MIPCLOSE=1");
  delay(1000);
  
  Serial.println("✅ TCP连接已断开");
  isConnected = false;
}

// 4G协议：发送会话开始信号（修复版）
void sendSessionStart() {
  if (!isConnected) return;
  
  String mac = getMACAddress();
  String sessionMsg = "SESSION_START:" + mac;
  
  Serial.println("🔧 准备发送会话开始信号: " + sessionMsg);
  
  // 发送AT+MIPSEND指令
  String sendCmd = "AT+MIPSEND=0," + String(sessionMsg.length());
  modemSerial.println(sendCmd);
  Serial.println("📤 发送AT指令: " + sendCmd);
  
  // 等待模块响应">"提示符，最多等待5秒
  unsigned long startTime = millis();
  bool promptReceived = false;
  String response = "";
  
  while (millis() - startTime < 5000) {
    if (modemSerial.available()) {
      char c = modemSerial.read();
      response += c;
      Serial.print(c);
      
      if (c == '>') {
        promptReceived = true;
        Serial.println("\n✅ 收到发送提示符，开始发送数据");
        break;
      }
    }
  }
  
  if (!promptReceived) {
    Serial.println("❌ 未收到发送提示符，发送失败");
    return;
  }
  
  // 发送实际数据
  modemSerial.print(sessionMsg);
  Serial.println("📤 数据已发送: " + sessionMsg);
  
  // 等待发送完成确认
  startTime = millis();
  while (millis() - startTime < 3000) {
    if (modemSerial.available()) {
      String confirmResponse = modemSerial.readString();
      Serial.print("📥 发送确认: " + confirmResponse);
      if (confirmResponse.indexOf("SEND OK") != -1 || confirmResponse.indexOf("+MIPSEND: 0,") != -1) {
        Serial.println("✅ 会话开始信号发送成功");
        return;
      }
    }
  }
  
  Serial.println("⚠️ 未收到发送确认，但数据可能已发送");
}

// 全局变量：记录上次发送时间和计数
static unsigned long lastSendTime = 0;
static int audioSendCount = 0;

// 4G协议：批量发送音频数据到input_service（进一步优化版）
void sendAudioData(const uint8_t* data, size_t length) {
  if (!isConnected) return;
  
  // 限制发送频率，避免4G模块过载
  unsigned long currentTime = millis();
  if (currentTime - lastSendTime < 100) { // 最小100ms间隔
    return;
  }
  
  audioSendCount++;
  
  // 清空串口缓冲区，避免残留数据干扰
  while (modemSerial.available()) {
    modemSerial.read();
  }
  
  // 发送AT+MIPSEND指令
  String sendCmd = "AT+MIPSEND=0," + String(length);
  modemSerial.println(sendCmd);
  Serial.println("📤 音频数据包 #" + String(audioSendCount) + ", 长度: " + String(length));
  
  // 等待模块响应">"提示符，增加到8秒等待时间
  unsigned long startTime = millis();
  bool promptReceived = false;
  String response = "";
  
  while (millis() - startTime < 8000) {
    if (modemSerial.available()) {
      char c = modemSerial.read();
      response += c;
      if (c == '>') {
        promptReceived = true;
        Serial.println("✅ 收到提示符，发送音频数据包 #" + String(audioSendCount));
        break;
      }
    }
    delay(5); // 增加延迟
  }
  
  if (!promptReceived) {
    Serial.println("❌ 音频数据包 #" + String(audioSendCount) + " 发送失败：未收到提示符");
    Serial.println("收到的响应: " + response);
    return;
  }
  
  // 发送音频数据
  modemSerial.write(data, length);
  
  // 等待发送完成确认
  startTime = millis();
  bool sendConfirmed = false;
  while (millis() - startTime < 5000) {
    if (modemSerial.available()) {
      String confirmResponse = modemSerial.readString();
      if (confirmResponse.indexOf("OK") != -1 || confirmResponse.indexOf("+MIPSEND") != -1) {
        Serial.println("✅ 音频数据包 #" + String(audioSendCount) + " 发送确认");
        sendConfirmed = true;
        break;
      }
    }
    delay(5);
  }
  
  if (!sendConfirmed) {
    Serial.println("⚠️ 音频数据包 #" + String(audioSendCount) + " 未收到确认");
  }
  
  lastSendTime = currentTime;
  delay(100); // 固定间隔，避免4G模块过载
}

// 4G协议：发送录音完成信号
void sendRecordingComplete() {
  if (!isConnected) return;
  
  String completeMsg = "RECORDING_COMPLETE";
  String sendCmd = "AT+MIPSEND=0," + String(completeMsg.length());
  modemSerial.println(sendCmd);
  delay(100);
  modemSerial.print(completeMsg);
  
  Serial.println("📤 已发送录音完成信号");
}

// 激活轮询机制
void activatePolling() {
  isPollingActive = true;
  pollingStartTime = millis();
  Serial.println("🔄 激活音频轮询机制");
}

// 停止轮询机制
void stopPolling() {
  isPollingActive = false;
  pollingStartTime = 0;
  Serial.println("⏹️ 停止音频轮询机制");
}

// 检查轮询超时
bool isPollingTimeout() {
  if (!isPollingActive) return false;
  return (millis() - pollingStartTime) > POLLING_TIMEOUT;
}

// 新增：记录最后数据接收时间
static unsigned long lastDataReceiveTime = 0;

// 流式PCM传输相关变量
int currentAudioIndex = -1;
int totalAudioCount = 0;

// 4G协议：HTTP API调用请求音频信息 (流式PCM版)
void requestAudioFromAPI() {
  if (!isConnected || !isPollingActive) return;
  
  // 智能轮询超时检查：只有在长时间没有收到数据时才停止
  unsigned long timeSinceLastData = millis() - lastDataReceiveTime;
  if (lastDataReceiveTime > 0 && timeSinceLastData > 120000) { // 2分钟没有数据才停止
    Serial.println("⏰ 长时间未收到数据（2分钟），自动停止轮询");
    stopPolling();
    closeConnections();
    return;
  }
  
  String mac = getMACAddress();
  
  // 构建标准HTTP POST请求到main_service的4G API
  String httpRequest = "POST /api/4g/request_audio?mac=" + mac + " HTTP/1.1\r\n";
  httpRequest += "Host: " + String(serverIP) + ":" + String(apiPort) + "\r\n";
  httpRequest += "User-Agent: ESP32-4G/1.0\r\n";
  httpRequest += "Accept: application/json\r\n";
  httpRequest += "Content-Length: 0\r\n";
  httpRequest += "Connection: keep-alive\r\n\r\n";
  
  Serial.println("🔄 发送轮询请求...");
  
  // 使用改进的AT+MIPSEND流程
  String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
  modemSerial.println(sendCmd);
  
  // 等待">"提示符
  unsigned long startTime = millis();
  bool promptReceived = false;
  while (millis() - startTime < 5000) {
    if (modemSerial.available()) {
      char c = modemSerial.read();
      if (c == '>') {
        promptReceived = true;
        break;
      }
    }
  }
  
  if (!promptReceived) {
    Serial.println("❌ 轮询请求发送失败：未收到提示符");
    return;
  }
  
  // 发送HTTP请求
  modemSerial.print(httpRequest);
  Serial.println("📤 HTTP请求已发送: " + httpRequest.substring(0, httpRequest.indexOf('\r')));
  
  // 等待发送完成确认
  delay(1000);
  startTime = millis();
  bool sendOK = false;
  while (millis() - startTime < 3000) {
    if (modemSerial.available()) {
      String response = modemSerial.readString();
      if (response.indexOf("SEND OK") != -1 || response.indexOf("+MIPSEND") != -1) {
        Serial.println("✅ HTTP请求发送确认");
        sendOK = true;
        break;
      }
    }
  }
  
  if (!sendOK) {
    Serial.println("❌ HTTP请求发送未确认");
    return;
  }
  
  // 主动检查连接状态 - 发送少量数据测试连接
  Serial.println("🔍 主动检查TCP连接状态...");
  delay(1000);
  
  // 发送一个很小的测试数据包
  String testCmd = "AT+MIPSEND=1,4";
  modemSerial.println(testCmd);
  Serial.println("发送连接测试: " + testCmd);
  
  // 等待响应，如果没有响应说明连接已断开
  unsigned long testStart = millis();
  bool connectionOK = false;
  String testResponse = "";
  
  while (millis() - testStart < 3000) {
    if (modemSerial.available()) {
      char c = modemSerial.read();
      testResponse += c;
      Serial.print(c);
      
      if (c == '>') {
        // 收到提示符，连接正常，发送测试数据
        modemSerial.print("TEST");
        Serial.println("\n✅ 连接测试正常");
        connectionOK = true;
        break;
      } else if (testResponse.indexOf("ERROR") != -1) {
        Serial.println("\n❌ 连接测试失败，连接已断开");
        break;
      }
    }
  }
  
  if (!connectionOK) {
    Serial.println("⚠️ 连接状态异常，尝试重新建立连接...");
    
    // 重新建立连接
    delay(2000);
    String connectCmd1 = "AT+MIPOPEN=1,\"TCP\",\"" + String(serverIP) + "\"," + String(apiPort);
    modemSerial.println(connectCmd1);
    Serial.println("🔄 重新发送: " + connectCmd1);
    
    if (waitForResponse("+MIPOPEN: 1,0", 15000)) {
      Serial.println("✅ 重新连接成功，重新发送HTTP请求...");
      
      // 重新发送HTTP请求
      String mac = getMACAddress();
      String httpRequest = "POST /api/4g/request_audio?mac=" + mac + " HTTP/1.1\r\n";
      httpRequest += "Host: " + String(serverIP) + ":" + String(apiPort) + "\r\n";
      httpRequest += "Content-Length: 0\r\n";
      httpRequest += "Connection: keep-alive\r\n\r\n";
      
      String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
      modemSerial.println(sendCmd);
      
      // 等待提示符并重新发送
      unsigned long retryStart = millis();
      while (millis() - retryStart < 5000) {
        if (modemSerial.available()) {
          char c = modemSerial.read();
          if (c == '>') {
            modemSerial.print(httpRequest);
            Serial.println("🔄 HTTP请求重新发送完成");
            break;
          }
        }
      }
    } else {
      Serial.println("❌ 重新连接失败，停止轮询");
      stopPolling();
      return;
    }
  }
  
  // 读取HTTP响应 - 优化版，分片接收
  Serial.println("⏳ 等待HTTP响应...");
  Serial.println("🔍 开始监听串口数据...");
  delay(3000); // 增加等待时间
  String httpResponse = "";
  startTime = millis();
  int responseChunks = 0;
  
  while (millis() - startTime < 15000) { // 增加到15秒
    if (modemSerial.available()) {
      String chunk = "";
      // 读取当前可用的所有数据
      while (modemSerial.available()) {
        char c = modemSerial.read();
        chunk += c;
        delay(1); // 很小的延迟确保数据完整
      }
      
      if (chunk.length() > 0) {
        httpResponse += chunk;
        responseChunks++;
        lastDataReceiveTime = millis(); // 更新最后数据接收时间
        Serial.println("📦 收到响应片段 #" + String(responseChunks) + ", 长度: " + String(chunk.length()));
        Serial.println("片段内容: " + chunk.substring(0, min(100, (int)chunk.length())));
        
        // 检查是否收到连接断开消息
        if (chunk.indexOf("+MIPURC: \"disconn\",1,") != -1) {
          Serial.println("⚠️ 检测到Socket 1连接断开，尝试重新连接...");
          
          // 重新建立连接
          delay(2000);
          String connectCmd1 = "AT+MIPOPEN=1,\"TCP\",\"" + String(serverIP) + "\"," + String(apiPort);
          modemSerial.println(connectCmd1);
          Serial.println("🔄 重新发送: " + connectCmd1);
          
          if (waitForResponse("+MIPOPEN: 1,0", 15000)) {
            Serial.println("✅ 重新连接成功，重新发送HTTP请求...");
            
            // 重新发送HTTP请求
            String mac = getMACAddress();
            String httpRequest = "POST /api/4g/request_audio?mac=" + mac + " HTTP/1.1\r\n";
            httpRequest += "Host: " + String(serverIP) + ":" + String(apiPort) + "\r\n";
            httpRequest += "Content-Length: 0\r\n";
            httpRequest += "Connection: keep-alive\r\n\r\n";
            
            String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
            modemSerial.println(sendCmd);
            
            // 等待提示符并重新发送
            unsigned long retryStart = millis();
            while (millis() - retryStart < 5000) {
              if (modemSerial.available()) {
                char c = modemSerial.read();
                if (c == '>') {
                  modemSerial.print(httpRequest);
                  Serial.println("🔄 HTTP请求重新发送完成");
                  break;
                }
              }
            }
            
            // 清空之前的响应，重新开始接收
            httpResponse = "";
            responseChunks = 0;
            startTime = millis(); // 重置超时时间
            continue;
          } else {
            Serial.println("❌ 重新连接失败");
            break;
          }
        }
        
        // 检查是否收到完整的HTTP响应 - 流式PCM模式（轻量级JSON）
        if (httpResponse.indexOf("\r\n\r\n") != -1) {
          Serial.println("✅ 检测到HTTP响应头结束，接收轻量级JSON...");
          
          // 流式PCM模式下只需要接收小型JSON响应
          unsigned long jsonStartTime = millis();
          
          while (millis() - jsonStartTime < 5000) { // 只需5秒接收小型JSON
            if (modemSerial.available()) {
              String chunk = "";
              while (modemSerial.available()) {
                char c = modemSerial.read();
                chunk += c;
                delay(1);
              }
              
              if (chunk.length() > 0) {
                httpResponse += chunk;
                responseChunks++;
                Serial.println("📦 追加轻量级JSON片段 #" + String(responseChunks) + ", 长度: " + String(chunk.length()));
                
                // 检查轻量级JSON是否完整 - 查找结束标记
                if (httpResponse.indexOf("\"status\":\"audio_ready\"") != -1 || 
                    httpResponse.indexOf("\"status\":\"no_audio\"") != -1) {
                  Serial.println("✅ 检测到轻量级JSON完整");
                  break;
                }
                
                // 小型JSON响应通常很快完成
                if (httpResponse.length() > 1000) { // 轻量级JSON不会超过1KB
                  Serial.println("✅ 轻量级JSON接收完成");
                  break;
                }
              }
              delay(50); // 减少等待时间
            } else {
              delay(20); // 没有数据时短暂等待
            }
          }
          
          Serial.println("✅ 轻量级JSON接收完成");
          break;
        }
      }
      delay(100); // 等待更多数据
    } else {
      delay(50); // 没有数据时短暂等待
    }
  }
  
  Serial.println("📊 总计收到 " + String(responseChunks) + " 个响应片段");
  
  Serial.println("📥 收到HTTP响应长度: " + String(httpResponse.length()));
  Serial.println("响应内容预览: " + httpResponse.substring(0, min(200, (int)httpResponse.length())));
  
  // 重构JSON响应 - 处理rtcp分片消息
  String reconstructedJSON = "";
  
  // 检查是否包含rtcp消息
  if (httpResponse.indexOf("+MIPURC: \"rtcp\"") != -1) {
    Serial.println("🔧 检测到rtcp消息，重构JSON响应...");
    
    // 提取所有rtcp消息中的数据部分
    int pos = 0;
    while (pos < httpResponse.length()) {
      int rtcpStart = httpResponse.indexOf("+MIPURC: \"rtcp\",1,", pos);
      if (rtcpStart == -1) break;
      
      // 找到数据长度
      int lengthStart = rtcpStart + 18; // 跳过 "+MIPURC: \"rtcp\",1,"
      int commaPos = httpResponse.indexOf(",", lengthStart);
      if (commaPos == -1) break;
      
      // 提取数据部分
      int dataStart = commaPos + 1;
      int nextRtcp = httpResponse.indexOf("+MIPURC:", dataStart);
      int dataEnd = (nextRtcp == -1) ? httpResponse.length() : nextRtcp;
      
      String dataPart = httpResponse.substring(dataStart, dataEnd);
      dataPart.trim(); // 移除换行符等
      reconstructedJSON += dataPart;
      
      pos = dataEnd;
    }
    
    Serial.println("📋 重构的JSON长度: " + String(reconstructedJSON.length()));
    Serial.println("JSON预览: " + reconstructedJSON.substring(0, min(200, (int)reconstructedJSON.length())));
  } else {
    reconstructedJSON = httpResponse;
  }
  
  // 解析重构后的JSON响应 - 流式PCM版本
  if (reconstructedJSON.indexOf("\"status\":\"audio_ready\"") != -1) {
    // 服务器有音频准备就绪，解析音频信息
    int totalCountStart = reconstructedJSON.indexOf("\"total_count\":") + 14;
    int totalCountEnd = reconstructedJSON.indexOf(",", totalCountStart);
    if (totalCountStart > 13 && totalCountEnd > totalCountStart) {
      totalAudioCount = reconstructedJSON.substring(totalCountStart, totalCountEnd).toInt();
    }
    
    int currentIndexStart = reconstructedJSON.indexOf("\"current_index\":") + 16;
    int currentIndexEnd = reconstructedJSON.indexOf(",", currentIndexStart);
    if (currentIndexStart > 15 && currentIndexEnd > currentIndexStart) {
      currentAudioIndex = reconstructedJSON.substring(currentIndexStart, currentIndexEnd).toInt();
    }
    
    Serial.println("🎵 服务器音频就绪: 总数=" + String(totalAudioCount) + ", 当前index=" + String(currentAudioIndex));
    
    // 请求开始流式传输当前音频
    requestStreamAudio(currentAudioIndex);
    
  } else if (reconstructedJSON.indexOf("\"status\":\"no_audio\"") != -1) {
    // 暂无音频，继续等待
    Serial.println("⏳ 服务器音频处理中...");
  } else {
    Serial.println("❓ 未识别的响应状态");
    Serial.println("重构JSON内容预览: " + reconstructedJSON.substring(0, min(300, (int)reconstructedJSON.length())));
  }
}


// 流式PCM：请求开始传输指定音频
void requestStreamAudio(int audioIndex) {
  if (!isConnected) return;
  
  String mac = getMACAddress();
  
  // 构建HTTP POST请求启动流式传输
  String httpRequest = "POST /api/4g/start_stream?mac=" + mac + "&audio_index=" + String(audioIndex) + " HTTP/1.1\r\n";
  httpRequest += "Host: " + String(serverIP) + ":" + String(apiPort) + "\r\n";
  httpRequest += "Content-Length: 0\r\n";
  httpRequest += "Connection: keep-alive\r\n\r\n";
  
  Serial.println("🎵 请求流式传输音频 index=" + String(audioIndex));
  
  String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
  modemSerial.println(sendCmd);
  delay(100);
  
  // 等待提示符
  unsigned long startTime = millis();
  bool promptReceived = false;
  while (millis() - startTime < 5000) {
    if (modemSerial.available()) {
      char c = modemSerial.read();
      if (c == '>') {
        promptReceived = true;
        break;
      }
    }
  }
  
  if (!promptReceived) {
    Serial.println("❌ 流式传输请求发送失败：未收到提示符");
    // 尝试重新建立连接
    Serial.println("🔄 尝试重新建立轮询连接...");
    if (establishPollingConnection()) {
      Serial.println("✅ 轮询连接重新建立成功，重试请求");
      // 重试发送请求
      String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
      modemSerial.println(sendCmd);
      delay(100);
      
      startTime = millis();
      while (millis() - startTime < 5000) {
        if (modemSerial.available()) {
          char c = modemSerial.read();
          if (c == '>') {
            modemSerial.print(httpRequest);
            Serial.println("📤 流式传输请求重试发送成功");
            break;
          }
        }
      }
    }
    return;
  }
  
  // 发送HTTP请求
  modemSerial.print(httpRequest);
  Serial.println("📤 流式传输请求已发送");
  
  // 等待HTTP响应确认服务器已开始准备传输
  delay(2000);
  
  // 建立到upload_service的连接接收PCM数据
  if (connectToUploadService(audioIndex)) {
    Serial.println("✅ 已连接到upload_service，开始接收PCM流");
    isPlaying = true;
    receiveDirectPCMStream();
  } else {
    Serial.println("❌ 连接upload_service失败");
  }
}

// 建立到upload_service的TCP连接接收PCM数据
bool connectToUploadService(int audioIndex) {
  Serial.println("🔗 建立到upload_service的TCP连接...");
  
  // 使用Socket 2连接到upload_service (端口8086)
  String connectCmd = "AT+MIPOPEN=2,\"TCP\",\"" + String(serverIP) + "\",8086";
  
  modemSerial.println(connectCmd);
  Serial.println("发送: " + connectCmd);
  
  if (!waitForResponse("+MIPOPEN: 2,0", 15000)) {
    Serial.println("❌ upload_service连接失败");
    return false;
  }
  
  Serial.println("✅ upload_service连接建立成功");
  return true;
}

// 直接接收PCM数据流
void receiveDirectPCMStream() {
  Serial.println("🎵 开始接收直接PCM音频流...");
  
  uint8_t pcmBuffer[1024];
  int bytesReceived = 0;
  unsigned long streamStartTime = millis();
  bool streamActive = true;
  
  while (streamActive && !needInterrupt && millis() - streamStartTime < 30000) { // 30秒超时
    if (modemSerial.available()) {
      String chunk = modemSerial.readString();
      
      // 检查是否收到PCM数据
      if (chunk.indexOf("+MIPURC: \"recv\",2,") != -1) {
        Serial.println("📦 收到PCM数据包");
        
        // 解析数据长度
        int lengthStart = chunk.indexOf("+MIPURC: \"recv\",2,") + 18;
        int lengthEnd = chunk.indexOf("\r\n", lengthStart);
        if (lengthStart > 17 && lengthEnd > lengthStart) {
          int dataLength = chunk.substring(lengthStart, lengthEnd).toInt();
          
          // 提取PCM数据
          int dataStart = lengthEnd + 2;
          if (dataStart < chunk.length() && (dataStart + dataLength) <= chunk.length()) {
            String pcmData = chunk.substring(dataStart, dataStart + dataLength);
            
            // 检查是否是结束标记
            if (pcmData.indexOf("END_STREAM") != -1) {
              Serial.println("✅ 检测到PCM流结束标记");
              streamActive = false;
              // 移除结束标记的数据
              int endPos = pcmData.indexOf("END_STREAM");
              if (endPos > 0) {
                pcmData = pcmData.substring(0, endPos);
              }
            }
            
            // 将字符串转换为字节数组并播放
            if (pcmData.length() > 0) {
              int pcmBytes = min(pcmData.length(), (int)sizeof(pcmBuffer));
              for (int i = 0; i < pcmBytes; i++) {
                pcmBuffer[i] = (uint8_t)pcmData[i];
              }
              
              bytesReceived += pcmBytes;
              
              // 直接播放PCM数据到I2S
              size_t bytesWritten;
              esp_err_t result = i2s_write(I2S_NUM_1, pcmBuffer, pcmBytes, &bytesWritten, portMAX_DELAY);
              if (result != ESP_OK) {
                Serial.printf("❌ I2S写入失败, 错误码: %d\n", result);
              }
            }
          }
        }
      }
    } else {
      delay(10); // 等待更多数据
    }
    
    // 检查中断
    if (needInterrupt) {
      Serial.println("⚠️ PCM流播放被中断");
      break;
    }
  }
  
  // 关闭upload_service连接
  sendATCommand("AT+MIPCLOSE=2");
  
  Serial.println("✅ 直接PCM音频流播放完成，总接收: " + String(bytesReceived) + " bytes");
  isPlaying = false;
  
  // 通知服务器播放完成
  notifyAudioComplete(currentAudioIndex);
}

// 流式PCM：开始接收PCM数据流
void startReceivingPCMStream() {
  Serial.println("🎵 开始接收PCM音频流...");
  
  // 等待服务器开始发送PCM数据
  delay(2000);
  
  uint8_t pcmBuffer[1024];
  int bytesReceived = 0;
  unsigned long streamStartTime = millis();
  bool streamActive = true;
  
  while (streamActive && !needInterrupt && millis() - streamStartTime < 30000) { // 30秒超时
    if (modemSerial.available()) {
      int bytesToRead = min((int)modemSerial.available(), (int)sizeof(pcmBuffer));
      for (int i = 0; i < bytesToRead; i++) {
        pcmBuffer[i] = modemSerial.read();
      }
      
      if (bytesToRead > 0) {
        bytesReceived += bytesToRead;
        
        // 检查是否是流结束标记
        String endMarker = "END_STREAM";
        bool isEndMarker = true;
        if (bytesToRead >= endMarker.length()) {
          for (int i = 0; i < endMarker.length(); i++) {
            if (pcmBuffer[bytesToRead - endMarker.length() + i] != endMarker[i]) {
              isEndMarker = false;
              break;
            }
          }
        } else {
          isEndMarker = false;
        }
        
        if (isEndMarker) {
          Serial.println("✅ 检测到PCM流结束标记");
          bytesToRead -= endMarker.length(); // 移除结束标记
          streamActive = false;
        }
        
        // 直接播放PCM数据到I2S
        if (bytesToRead > 0) {
          size_t bytesWritten;
          esp_err_t result = i2s_write(I2S_NUM_1, pcmBuffer, bytesToRead, &bytesWritten, portMAX_DELAY);
          if (result != ESP_OK) {
            Serial.printf("❌ I2S写入失败, 错误码: %d\n", result);
          }
        }
      }
    } else {
      delay(10); // 等待更多数据
    }
    
    // 检查中断
    if (needInterrupt) {
      Serial.println("⚠️ PCM流播放被中断");
      break;
    }
  }
  
  Serial.println("✅ PCM音频流播放完成，总接收: " + String(bytesReceived) + " bytes");
  isPlaying = false;
  
  // 通知服务器播放完成
  notifyAudioComplete(currentAudioIndex);
}

// 流式PCM：通知服务器音频播放完成
void notifyAudioComplete(int audioIndex) {
  if (!isConnected) return;
  
  String mac = getMACAddress();
  
  // 构建HTTP POST请求通知播放完成
  String httpRequest = "POST /api/4g/audio_complete?mac=" + mac + "&audio_index=" + String(audioIndex) + " HTTP/1.1\r\n";
  httpRequest += "Host: " + String(serverIP) + ":" + String(apiPort) + "\r\n";
  httpRequest += "Content-Length: 0\r\n";
  httpRequest += "Connection: keep-alive\r\n\r\n";
  
  Serial.println("📤 通知音频播放完成 index=" + String(audioIndex));
  
  String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
  modemSerial.println(sendCmd);
  delay(100);
  
  // 等待提示符并发送
  unsigned long startTime = millis();
  while (millis() - startTime < 3000) {
    if (modemSerial.available()) {
      char c = modemSerial.read();
      if (c == '>') {
        modemSerial.print(httpRequest);
        Serial.println("✅ 播放完成通知已发送");
        
        // 等待服务器响应，检查是否有下一个音频
        checkForNextAudio();
        return;
      }
    }
  }
  
  Serial.println("❌ 播放完成通知发送失败");
}

// 流式PCM：检查是否有下一个音频需要播放
void checkForNextAudio() {
  // 等待服务器响应
  delay(1000);
  
  String response = "";
  unsigned long startTime = millis();
  
  while (millis() - startTime < 5000) {
    if (modemSerial.available()) {
      response += (char)modemSerial.read();
    }
  }
  
  Serial.println("📥 服务器响应: " + response.substring(0, min(200, (int)response.length())));
  
  if (response.indexOf("\"status\":\"next_ready\"") != -1) {
    // 还有下一个音频
    int nextIndexStart = response.indexOf("\"next_index\":") + 13;
    int nextIndexEnd = response.indexOf(",", nextIndexStart);
    if (nextIndexStart > 12 && nextIndexEnd > nextIndexStart) {
      int nextIndex = response.substring(nextIndexStart, nextIndexEnd).toInt();
      Serial.println("🎵 准备播放下一个音频 index=" + String(nextIndex));
      
      // 更新当前音频索引并请求下一个
      currentAudioIndex = nextIndex;
      delay(1000);
      requestStreamAudio(nextIndex);
    }
  } else if (response.indexOf("\"status\":\"all_complete\"") != -1) {
    // 所有音频播放完成
    Serial.println("🎉 所有音频播放完成，结束会话");
    stopPolling();
    closeConnections();
  } else {
    // 继续轮询
    Serial.println("🔄 继续轮询等待更多音频...");
  }
}

// 4G协议：发送中断信号
void sendInterruptSignal() {
  if (!isConnected) return;
  
  String mac = getMACAddress();
  
  // 通过HTTP API发送中断请求
  String httpRequest = "POST /api/4g/interrupt?mac=" + mac + " HTTP/1.1\r\n";
  httpRequest += "Host: " + String(serverIP) + ":" + String(apiPort) + "\r\n";
  httpRequest += "Content-Length: 0\r\n";
  httpRequest += "Connection: keep-alive\r\n\r\n";
  
  String sendCmd = "AT+MIPSEND=1," + String(httpRequest.length());
  modemSerial.println(sendCmd);
  delay(100);
  modemSerial.print(httpRequest);
  
  Serial.println("📤 已发送中断信号到服务器");
}

void setupBLE() {
  if (!bleEnabled) {
    Serial.println("ℹ️ BLE功能已禁用");
    return;
  }
  
  Serial.println("🔧 初始化BLE...");
  
  String deviceName = "ESP32_Audio_" + getMACAddress().substring(12);
  deviceName.replace(":", "");
  
  BLEDevice::init(deviceName.c_str());
  Serial.printf("✅ BLE设备初始化完成，设备名: %s\n", deviceName.c_str());
  
  xTaskCreatePinnedToCore(
    bleTask,
    "BLETask",
    4000,
    NULL,
    1,
    &bleTaskHandle,
    1
  );
  
  Serial.println("🚀 BLE任务已在核心1启动");
}

void bleTask(void* parameter) {
  Serial.println("📶 BLE任务启动在核心1");
  
  uint8_t beaconData[25] = {
    0x4C, 0x00,
    0x02, 0x15,
    0x12, 0x34, 0x56, 0x78,
    0x12, 0x34,
    0x12, 0x34,
    0x12, 0x34,
    0x12, 0x34, 0x56, 0x78, 0x90, 0xAB,
    0x03, 0xE9,
    0x00, 0x2A,
    0xC5
  };

  BLEAdvertisementData advData;
  advData.setFlags(0x04);
  advData.setManufacturerData(String((char*)beaconData, 25)); 
  
  pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->setScanResponse(false);
  pAdvertising->setAdvertisementType(ADV_TYPE_NONCONN_IND);
  pAdvertising->setMinInterval(1600);
  pAdvertising->setMaxInterval(1600);
  pAdvertising->setAdvertisementData(advData);
  pAdvertising->start();
  
  Serial.println("✅ BLE iBeacon广播已启动");
  
  while (bleEnabled) {
    vTaskDelay(1000 / portTICK_PERIOD_MS);
  }
  
  if (pAdvertising) {
    pAdvertising->stop();
    Serial.println("🛑 BLE广播已停止");
  }
  
  bleTaskHandle = NULL;
  Serial.println("📴 BLE任务已结束");
  vTaskDelete(NULL);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n====== ESP32语音助手(4G版)启动中 ======");
  
  // 1. 初始化4G网络（不建立TCP连接）
  modemSerial.begin(BAUD_RATE, SERIAL_8N1, MODEM_RX, MODEM_TX);
  delay(3000);
  
  if (!init4GNetwork()) {
    Serial.println("❌ 4G网络初始化失败，系统无法启动");
    while(1);
  }

  // 2. 初始化文件系统
  if(SPIFFS.begin(true)) {
    Serial.println("✅ SPIFFS初始化成功");
  } else {
    Serial.println("❌ SPIFFS初始化失败");
  }
  
  // 3. 初始化I2S
  setupI2S();
  
  // 4. 初始化BLE
  setupBLE();
  
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  Serial.println("✅ 按钮初始化完成");
  
  Serial.println("\n🚀 ESP32语音助手(4G版)已启动");
  Serial.println("- 4G网络已就绪，待用状态（未建立TCP连接）");
  Serial.println("- 按下按钮将建立连接并开始录音");
  Serial.println("- 松开按钮结束录音并等待音频");
  Serial.println("- 会话结束后自动断开连接");
  Serial.println("- BLE iBeacon广播已启用");
  Serial.println("====================================");
}

void loop() {
  // 按钮处理逻辑
  static bool lastButtonState = HIGH;
  bool currentButtonState = digitalRead(BUTTON_PIN);
  
  if (currentButtonState != lastButtonState) {
    delay(50); // 防抖动
    if (digitalRead(BUTTON_PIN) == currentButtonState) {
      if (currentButtonState == LOW) {
        Serial.println("\n🔘 按钮按下");
        if (isPlaying || isPollingActive) {
          // 中断播放和轮询
          Serial.println("⚠️ 强制停止播放和轮询...");
          sendInterruptSignal(); // 发送中断信号到服务器
          stopPolling(); // 停止轮询
          isPlaying = false;
          needInterrupt = true; // 设置中断标志
          closeConnections(); // 断开连接
          Serial.println("✅ 播放已停止，连接已断开");
        } else {
          // 开始录音 - 4G协议流程
          Serial.println("🔗 建立连接并开始录音...");
          if (establishConnections()) { // 建立TCP连接并自动发送会话开始信号
            isRecording = true;
            needInterrupt = false; // 清除中断标志
            Serial.println("🎤 开始录音（4G流式传输）...");
          } else {
            Serial.println("❌ 连接失败，无法开始录音");
          }
        }
      } else {
        Serial.println("\n🔘 按钮释放");
        if (isRecording) {
          // 停止录音并建立轮询连接
          sendRecordingComplete(); // 发送录音完成信号
          isRecording = false;
          Serial.println("🛑 停止录音，准备建立轮询连接...");
          
          // 等待3秒让服务器处理音频
          delay(3000);
          
          // 建立到main_service的连接用于轮询
          if (establishPollingConnection()) {
            activatePolling(); // 激活轮询机制
            Serial.println("🔄 轮询连接已建立，开始请求音频...");
          } else {
            Serial.println("❌ 轮询连接失败，结束会话");
            closeConnections();
          }
        }
      }
    }
    lastButtonState = currentButtonState;
  }
  
  // 4G协议：流式录音数据处理
  if (isRecording && isConnected) {
    uint8_t buffer[512];
    size_t bytesRead;
    esp_err_t readResult = i2s_read(I2S_NUM_0, buffer, sizeof(buffer), &bytesRead, 0);
    if (readResult == ESP_OK && bytesRead > 0) {
      sendAudioData(buffer, bytesRead); // 流式发送到input_service
    }
  }
  
  // 4G协议：激活轮询音频请求逻辑
  static unsigned long lastRequestTime = 0;
  if (isPollingActive && !isRecording && isConnected && 
      millis() - lastRequestTime > POLLING_INTERVAL) { // 按轮询间隔请求
    requestAudioFromAPI(); // 使用HTTP API请求音频
    lastRequestTime = millis();
  }
  
  delay(10); // 稍微增加延迟，减少CPU占用
}

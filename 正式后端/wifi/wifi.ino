#include <WiFi.h>
#include <SPIFFS.h>
#include "driver/i2s.h"
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEAdvertising.h>

#define BUTTON_PIN 32

// I2S 麦克风（录音）
#define I2S_MIC_SD 15
#define I2S_MIC_WS 19
#define I2S_MIC_SCK 21

// I2S 扬声器（播放）
#define I2S_SPK_DOUT 14
#define I2S_SPK_BCLK 12
#define I2S_SPK_LRC 13

// Wi-Fi 及服务器信息
const char* ssid = "DE.AI";
const char* password = "slyl8888";
const char* pcIP = "192.168.18.37";  // PC 服务器IP
const int pcPort = 5001;  // 录音上传端口
const int receivePort = 5002;  // ESP32 接收TTS端口

WiFiClient senderClient;
WiFiServer receiverServer(receivePort);
bool isRecording = false;
bool isPlaying = false;  // 跟踪TTS是否正在播放
volatile bool needInterrupt = false; // 使用volatile确保在任务间正确共享

// I2S 录音配置 (适配ESP32 Arduino 3.x)
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

// I2S 播放配置 (适配ESP32 Arduino 3.x)
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

// I2S 引脚映射 (适配ESP32 Arduino 3.x)
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

// 存储当前会话的客户端连接
WiFiClient currentPlaybackClient;

// 用于防抖动的变量
unsigned long lastDebounceTime = 0;
const unsigned long debounceDelay = 50;
bool lastButtonReading = HIGH;
bool lastButtonState = HIGH;

// 用于管理播放任务的句柄
TaskHandle_t playbackTaskHandle = NULL;

// BLE相关变量
BLEAdvertising* pAdvertising;
TaskHandle_t bleTaskHandle = NULL;
bool bleEnabled = true;  // 可以通过这个变量控制BLE开关

// 获取ESP32的MAC地址
String getMACAddress() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
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
  setupSpeakerI2S();
}

// 分离扬声器I2S配置方便重置
void setupSpeakerI2S() {
  esp_err_t result = i2s_driver_install(I2S_NUM_1, &i2s_spk_config, 0, NULL);
  if (result == ESP_OK) {
    Serial.println("✅ 扬声器I2S驱动安装成功");
  } else {
    Serial.printf("❌ 扬声器I2S驱动安装失败, 错误码: %d\n", result);
  }
  
  result = i2s_set_pin(I2S_NUM_1, &spk_pins);
  if (result == ESP_OK) {
    Serial.println("✅ 扬声器I2S引脚设置成功");
  } else {
    Serial.printf("❌ 扬声器I2S引脚设置失败, 错误码: %d\n", result);
  }
}

void connectWiFi() {
  WiFi.begin(ssid, password);
  Serial.print("正在连接WiFi");
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✅ WiFi连接成功");
    Serial.print("IP地址: ");
    Serial.println(WiFi.localIP());
    Serial.print("MAC地址: ");
    Serial.println(getMACAddress());
  } else {
    Serial.println("\n❌ WiFi连接失败，请检查配置");
  }
}

void stopPlayback() {
  if (isPlaying) {
    Serial.println("\n⚠️ 强制停止播放...");
    
    // 设置中断标志
    needInterrupt = true;
    
    // 发送中断信号到服务器的三种方式，增加可靠性
    if (currentPlaybackClient) {
      // 1. 发送特殊中断标记
      Serial.println("📤 发送_INTERRUPT_信号给服务器");
      currentPlaybackClient.println("_INTERRUPT_");
      delay(20);
      
      // 2. 确保数据被刷新
      currentPlaybackClient.flush();
      delay(10);
      
      // 3. 断开连接
      currentPlaybackClient.stop();
      Serial.println("✅ 已断开播放客户端连接");
    }
    
    // 重置I2S播放器
    i2s_stop(I2S_NUM_1);
    i2s_zero_dma_buffer(I2S_NUM_1);
    
    // 重新初始化I2S播放器以解决可能的锁定问题
    i2s_driver_uninstall(I2S_NUM_1);
    setupSpeakerI2S();
    
    // 如果有播放任务，终止它
    if (playbackTaskHandle != NULL) {
      vTaskDelete(playbackTaskHandle);
      playbackTaskHandle = NULL;
      Serial.println("✅ 播放任务已终止");
    }
    
    isPlaying = false;
    needInterrupt = false;
    Serial.println("✅ 播放已完全停止并重置");
  }
}

void startRecording() {
  // 检查是否正在播放，如果是，停止播放
  if (isPlaying) {
    Serial.println("ℹ️ 开始录音前先停止播放");
    stopPlayback();
    delay(200); // 确保播放完全停止
  }
  
  // 如果已经在录音，不再重复初始化
  if (isRecording) {
    Serial.println("ℹ️ 已经在录音中");
    return;
  }
  
  Serial.printf("🔌 尝试连接到PC(%s:%d)...\n", pcIP, pcPort);
  
  // 多次尝试连接
  bool connected = false;
  for (int attempt = 0; attempt < 3 && !connected; attempt++) {
    if (senderClient.connect(pcIP, pcPort)) {
      connected = true;
      Serial.printf("✅ 连接PC成功 (尝试%d)\n", attempt+1);
      break;
    } else {
      Serial.printf("⚠️ 连接尝试%d失败，重试中...\n", attempt+1);
      delay(100);
    }
  }
  
  if (!connected) {
    Serial.println("❌ 连接PC失败，无法开始录音");
    return;
  }
  
  // 上传开始 - 使用MAC地址作为会话标识符
  String deviceMAC = "ESP32_" + getMACAddress();
  int bytesSent = senderClient.print(deviceMAC);
  Serial.printf("📤 发送会话ID(MAC): %s (%d字节)\n", deviceMAC.c_str(), bytesSent);
  
  // 初始化麦克风
  i2s_start(I2S_NUM_0);
  
  // 清空录音缓冲区
  size_t bytes_read;
  uint8_t buffer[512];
  for (int i = 0; i < 5; i++) {
    i2s_read(I2S_NUM_0, buffer, sizeof(buffer), &bytes_read, 0);
  }
  
  isRecording = true;
  Serial.println("🎤 开始录音...");
}

void stopRecording() {
  if (isRecording) {
    // 发送特殊标记告知服务器录音结束
    if (senderClient.connected()) {
      senderClient.println("RECORDING_COMPLETE");
      Serial.println("📤 发送录音结束标记");
      delay(50); // 确保数据发送完成
    }
    
    isRecording = false;
    senderClient.stop();
    Serial.println("🛑 停止录音，连接已关闭");
  }
}

// 播放处理函数 - 在单独的任务中运行
void playbackTask(void* parameter) {
  WiFiClient client = *((WiFiClient*)parameter);
  delete (WiFiClient*)parameter; // 释放分配的内存
  
  Serial.println("🎵 播放任务启动");
  
  // 保存客户端连接
  currentPlaybackClient = client;
  isPlaying = true;
  
  // 开始播放
  i2s_start(I2S_NUM_1);
  i2s_zero_dma_buffer(I2S_NUM_1);
  
  uint8_t buffer[512];
  size_t bytesRead = 0;
  size_t bytesWritten = 0;
  bool receivedInterrupt = false;
  bool endSignalReceived = false;
  unsigned long lastDataTime = millis();
  
  Serial.println("🔊 开始音频流播放...");
  
  // 接收和播放音频数据
  while (client.connected() && isPlaying && !needInterrupt) {
    // 检查中断请求
    if (needInterrupt) {
      Serial.println("⚠️ 检测到中断请求");
      break;
    }
    
    // 设置非阻塞读取，确保能够响应中断请求
    client.setTimeout(5); // 5ms超时
    
    // 检查可用数据
    int availableBytes = client.available();
    
    if (availableBytes > 0) {
      lastDataTime = millis(); // 更新接收时间
      
      // 读取数据
      int readSize = availableBytes < (int)sizeof(buffer) ? availableBytes : (int)sizeof(buffer);
      bytesRead = client.read(buffer, readSize);
      
      // 检查是否收到控制信号
      if (bytesRead >= 10) {
        // 检查是否是中断信号
        if (memcmp(buffer, "INTERRUPT_", 10) == 0) {
          Serial.println("⚠️ 收到服务器中断信号");
          receivedInterrupt = true;
          break;
        } 
        // 检查是否是结束标志
        else if (memcmp(buffer, "END_STREAM", 10) == 0) {
          Serial.println("✅ 收到播放结束标志");
          endSignalReceived = true;
          break;
        }
      }
      
      // 正常播放数据
      if (bytesRead > 0) {
        esp_err_t writeResult = i2s_write(I2S_NUM_1, buffer, bytesRead, &bytesWritten, 30 / portTICK_PERIOD_MS);
        if (writeResult != ESP_OK) {
          Serial.printf("❌ I2S写入失败, 错误码: %d\n", writeResult);
        }
      }
    } else {
      // 数据不可用时短暂等待
      vTaskDelay(1);
      
      // 检查连接是否超时
      if (millis() - lastDataTime > 5000) {
        Serial.println("⚠️ 接收超时，结束播放");
        break;
      }
    }
  }
  
  // 播放结束或中断处理
  if (needInterrupt || receivedInterrupt) {
    Serial.println("🚫 播放被中断");
  } else if (endSignalReceived) {
    Serial.println("✅ 播放正常结束");
  } else if (!client.connected()) {
    Serial.println("🔌 客户端连接已断开");
  }
  
  // 清理播放状态
  i2s_zero_dma_buffer(I2S_NUM_1);
  i2s_stop(I2S_NUM_1);
  client.stop();
  
  isPlaying = false;
  needInterrupt = false;
  playbackTaskHandle = NULL;
  Serial.println("📴 播放会话已结束");
  
  vTaskDelete(NULL);  // 删除当前任务
}

// 处理新的播放客户端
void handleClient(WiFiClient client) {
  Serial.println("📥 新客户端连接，准备播放");
  
  // 如果正在录音，先停止
  if (isRecording) {
    stopRecording();
    delay(100);
  }
  
  // 如果已经在播放，停止当前播放
  if (isPlaying) {
    Serial.println("⚠️ 已有播放正在进行，先停止它");
    stopPlayback();
    delay(100); // 给停止流程足够的时间
  }
  
  // 创建客户端副本
  WiFiClient* clientPtr = new WiFiClient(client);
  
  // 在单独的核心上启动播放任务
  xTaskCreatePinnedToCore(
    playbackTask,        // 任务函数
    "PlaybackTask",      // 任务名称
    8000,                // 栈大小
    (void*)clientPtr,    // 参数
    1,                   // 优先级
    &playbackTaskHandle, // 任务句柄
    0                    // 在核心0上运行
  );
}

// 按钮防抖动处理
bool debounceButton() {
  // 读取当前按钮状态
  bool reading = digitalRead(BUTTON_PIN);
  bool buttonChanged = false;
  
  // 如果状态改变，重置抖动定时器
  if (reading != lastButtonReading) {
    lastDebounceTime = millis();
  }
  
  // 如果经过了去抖动延迟，确认状态
  if ((millis() - lastDebounceTime) > debounceDelay) {
    if (reading != lastButtonState) {
      lastButtonState = reading;
      buttonChanged = true;
    }
  }
  
  lastButtonReading = reading;
  return buttonChanged;
}

// BLE iBeacon任务 - 在独立的核心上运行
void bleTask(void* parameter) {
  Serial.println("📶 BLE任务启动在核心1");
  
  // 构造 iBeacon 广播数据（25 字节标准格式）
  uint8_t beaconData[25] = {
    0x4C, 0x00,             // Apple 公司 ID (0x004C)
    0x02, 0x15,             // iBeacon 类型 + 长度
    // UUID: 12345678-1234-1234-1234-1234567890AB
    0x12, 0x34, 0x56, 0x78,
    0x12, 0x34,
    0x12, 0x34,
    0x12, 0x34,
    0x12, 0x34, 0x56, 0x78, 0x90, 0xAB,
    0x03, 0xE9,             // Major = 1001
    0x00, 0x2A,             // Minor = 42
    0xC5                    // Tx Power = -59 dBm
  };

  BLEAdvertisementData advData;
  advData.setFlags(0x04); // BR/EDR not supported
  advData.setManufacturerData(String((char*)beaconData, 25)); 

  pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->setScanResponse(false);
  pAdvertising->setAdvertisementType(ADV_TYPE_NONCONN_IND);
  pAdvertising->setMinInterval(1600);
  pAdvertising->setMaxInterval(1600);
  pAdvertising->setAdvertisementData(advData);
  pAdvertising->start();
  
  Serial.println("✅ BLE iBeacon广播已启动");
  Serial.println("📱 iBeacon参数:");
  Serial.println("   UUID: 12345678-1234-1234-1234-1234567890AB");
  Serial.println("   Major: 1001");
  Serial.println("   Minor: 42");
  Serial.println("   Tx Power: -59 dBm");
  
  // BLE任务主循环
  while (bleEnabled) {
    // 保持BLE运行，每秒检查一次状态
    vTaskDelay(1000 / portTICK_PERIOD_MS);
    
    // 可以在这里添加其他BLE相关的处理
    // 例如检查连接状态、更新广播数据等
  }
  
  // 停止BLE广播
  if (pAdvertising) {
    pAdvertising->stop();
    Serial.println("🛑 BLE广播已停止");
  }
  
  bleTaskHandle = NULL;
  Serial.println("📴 BLE任务已结束");
  vTaskDelete(NULL);
}

// 初始化BLE
void setupBLE() {
  if (!bleEnabled) {
    Serial.println("ℹ️ BLE功能已禁用");
    return;
  }
  
  Serial.println("🔧 初始化BLE...");
  
  // 使用设备MAC地址作为BLE设备名称
  String deviceName = "ESP32_Audio_" + getMACAddress().substring(12); // 取MAC地址后6位
  deviceName.replace(":", ""); // 移除冒号
  
  BLEDevice::init(deviceName.c_str());
  Serial.printf("✅ BLE设备初始化完成，设备名: %s\n", deviceName.c_str());
  
  // 在核心1上启动BLE任务，避免干扰音频处理
  xTaskCreatePinnedToCore(
    bleTask,           // 任务函数
    "BLETask",         // 任务名称
    4000,              // 栈大小（BLE需要较大栈空间）
    NULL,              // 参数
    1,                 // 优先级
    &bleTaskHandle,    // 任务句柄
    1                  // 在核心1上运行（与音频处理分离）
  );
  
  Serial.println("🚀 BLE任务已在核心1启动");
}

// 停止BLE功能
void stopBLE() {
  if (bleTaskHandle != NULL) {
    bleEnabled = false;
    Serial.println("⚠️ 正在停止BLE功能...");
    
    // 等待任务结束
    while (bleTaskHandle != NULL) {
      vTaskDelay(100 / portTICK_PERIOD_MS);
    }
    
    // 清理BLE资源
    BLEDevice::deinit(false);
    Serial.println("✅ BLE功能已完全停止");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000); // 确保串口稳定
  Serial.println("\n====== ESP32语音助手启动中 ======");
  
  if(SPIFFS.begin(true)) {
    Serial.println("✅ SPIFFS初始化成功");
  } else {
    Serial.println("❌ SPIFFS初始化失败");
  }
  
  setupI2S();
  connectWiFi();
  
  // 初始化BLE功能（在独立核心上运行）
  setupBLE();
  
  receiverServer.begin();
  Serial.printf("✅ 接收服务器已启动在端口 %d\n", receivePort);
  
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  Serial.println("✅ 按钮初始化完成");
  
  // 初始化按钮状态
  lastButtonState = digitalRead(BUTTON_PIN);
  lastButtonReading = lastButtonState;
  
  Serial.println("\n🚀 ESP32语音助手已启动");
  Serial.println("- 按下按钮开始录音");
  Serial.println("- 松开按钮结束录音");
  Serial.println("- 按下按钮可中断当前TTS播放");
  Serial.println("- BLE iBeacon广播已启用");
  Serial.println("====================================");
}

void loop() {
  // 使用改进的按钮防抖动功能
  if (debounceButton()) {
    // 按钮状态发生变化
    // 如果按钮被按下
    if (lastButtonState == LOW) {
      Serial.println("\n🔘 按钮按下");
      
      // 如果正在播放，立即中断播放并开始录音
      if (isPlaying) {
        Serial.println("🚫 用户请求中断当前播放");
        
        // 先发送中断信号再停止播放，确保信号能发出
        if (currentPlaybackClient) {
          Serial.println("📤 发送_INTERRUPT_信号给服务器");
          currentPlaybackClient.println("_INTERRUPT_");
          delay(20); // 给发送足够时间但不要太久
        }
        
        // 停止播放
        needInterrupt = true;
        stopPlayback();
        
        // 立即开始录音
        Serial.println("🔄 中断后立即开始录音");
        startRecording();
      } 
      // 没有在播放则开始录音
      else {
        startRecording();
      }
    }
    // 如果按钮被释放
    else {
      Serial.println("\n🔘 按钮释放");
      // 如果正在录音，停止录音
      if (isRecording) {
        stopRecording();
      }
    }
  }
  
  // 录音数据发送
  if (isRecording && senderClient.connected()) {
    uint8_t buffer[512];
    size_t bytesRead;
    esp_err_t readResult = i2s_read(I2S_NUM_0, buffer, sizeof(buffer), &bytesRead, 0);
    if (readResult == ESP_OK && bytesRead > 0) {
      int bytesSent = senderClient.write(buffer, bytesRead);
      if (bytesSent != bytesRead) {
        Serial.printf("⚠️ 只发送了 %d/%d 字节\n", bytesSent, bytesRead);
      }
    } else if (readResult != ESP_OK) {
      Serial.printf("❌ 麦克风读取失败, 错误码: %d\n", readResult);
    }
  } else if (isRecording && !senderClient.connected()) {
    // 如果客户端连接断开但还在录音状态
    Serial.println("⚠️ 录音客户端连接断开，停止录音");
    isRecording = false;
  }
  // 接收音频流
  WiFiClient client = receiverServer.available();
  if (client) {
    handleClient(client);
  }
  
  // 短暂延迟，避免CPU高负载
  delay(1);
}

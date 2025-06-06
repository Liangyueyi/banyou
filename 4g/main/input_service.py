#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ESP32 录音输入服务（优化版）
- 保留所有功能
- 增加 MAC 格式校验
- 增加 main 服务调用重试机制
- 请求失败打印完整 request_data，便于排查
"""

from flask import Flask, request, jsonify
import os
import datetime
import time
import threading
import requests
import logging
import socket
import base64
from pydub import AudioSegment
import json
import re
from typing import Dict, Optional

app = Flask(__name__)
UPLOAD_FOLDER = 'audio_mp3'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PORT = 8083
MAIN_API = "http://localhost:8000"
HEALTH_CHECK_INTERVAL = 60
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

service_status = {
    "started": False,
    "socket_server_active": False,
    "last_recording": None,
    "recordings_received": 0,
    "errors": 0
}
status_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("input_service")

def validate_mac_format(mac: str) -> bool:
    return re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", mac) is not None

def extract_mac_from_data(data: bytes) -> Optional[str]:
    try:
        if data.startswith(b'ESP32_'):
            mac_str = data.decode('utf-8').strip().replace('ESP32_', '')
            if validate_mac_format(mac_str):
                return mac_str
        data_str = data.decode('utf-8', errors='ignore')
        mac_match = re.search(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', data_str)
        if mac_match:
            return mac_match.group(0)
    except:
        pass
    return None

def send_file_info_to_main_service(filename: str, mac_address: str, ip_address: str, is_4g_device: bool = False):
    filepath = os.path.abspath(os.path.join(UPLOAD_FOLDER, filename))
    request_data = {
        "filename": filename,
        "mac_address": mac_address,
        "ip_address": ip_address,
        "file_location": filepath,
        "timestamp": datetime.datetime.now().isoformat(),
        "format": "mp3",
        "mac_position": "after_timestamp",
        "device_type": "4G" if is_4g_device else "WiFi"
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"📤 向 main 发送录音元数据 (设备类型: {'4G' if is_4g_device else 'WiFi'}, 尝试: {attempt + 1}/{max_retries})")
            session = requests.Session()
            session.trust_env = False  # 禁用环境变量中的代理设置
            response = session.post(
                f"{MAIN_API}/process/audio_file",
                json=request_data,
                timeout=15,  # 4G设备可能需要更长处理时间
                proxies={"http": None, "https": None}  # 明确禁用代理
            )
            if response.status_code == 200:
                logger.info(f"✅ 成功发送至 main 处理: {filename} ({'4G' if is_4g_device else 'WiFi'}设备)")
                return True
            else:
                logger.error(f"❌ HTTP {response.status_code}：{response.text}")
                if attempt == max_retries - 1:
                    return False
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ 请求异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                with status_lock:
                    service_status["errors"] += 1
                return False
            time.sleep(1 * (attempt + 1))  # 递增延迟重试
    return False

def handle_client_recording(client_socket, client_address):
    client_ip = client_address[0]
    mac_address = None
    audio_data = bytearray()
    is_4g_device = False

    try:
        # 4G网络需要更长的超时时间
        logger.info(f"🔗 开始处理客户端连接: {client_ip}")
        client_socket.settimeout(20.0)  # 进一步增加到20秒适应4G网络
        logger.info(f"⏰ 设置socket超时时间为20秒，等待首包数据...")
        
        try:
            initial_data = client_socket.recv(512)
            logger.info(f"✅ 成功接收首包数据，长度: {len(initial_data)} bytes")
            logger.info(f"【DEBUG】收到首包原始数据: {initial_data}")
        except socket.timeout:
            logger.error(f"❌ 超时20秒未收到首包数据: {client_ip} - 可能是ESP32端AT指令执行问题")
            return
        except Exception as e:
            logger.error(f"❌ 接收首包数据异常: {client_ip}, 错误: {e}")
            return
        if not initial_data:
            logger.warning(f"{client_ip} 未发送数据")
            return

        # 检查是否是4G设备的SESSION_START协议
        initial_str = initial_data.decode('utf-8', errors='ignore')
        if initial_str.startswith('SESSION_START:'):
            # 4G设备协议: SESSION_START:MAC地址
            mac_address = initial_str.replace('SESSION_START:', '').strip()
            is_4g_device = True
            logger.info(f"检测到4G设备会话开始: MAC={mac_address}, IP={client_ip}")
            
            # 通知main服务4G设备会话开始
            try:
                response = requests.post(
                    f"{MAIN_API}/api/4g/session_start",
                    params={"mac": mac_address, "ip": client_ip},
                    timeout=10,  # 增加超时时间
                    proxies={"http": None, "https": None}
                )
                if response.status_code == 200:
                    logger.info(f"✅ 4G设备会话已在main服务注册")
                else:
                    logger.warning(f"⚠️ 4G设备会话注册失败: {response.status_code}")
            except Exception as e:
                logger.error(f"❌ 4G设备会话注册异常: {e}")
            
            audio_data = bytearray()  # 4G设备不在初始消息中包含音频数据
        else:
            # WiFi设备：保持原有逻辑
            mac_address = extract_mac_from_data(initial_data)
            if not mac_address:
                logger.warning(f"未识别MAC，使用 fallback")
                # 将IP转为标准MAC格式，如192.168.18.177 -> 00:00:C0:A8:12:B1
                ip_parts = client_ip.split('.')
                mac_address = f"00:00:{int(ip_parts[0]):02X}:{int(ip_parts[1]):02X}:{int(ip_parts[2]):02X}:{int(ip_parts[3]):02X}"

            audio_data = bytearray(initial_data if not validate_mac_format(mac_address) else b"")

        # 4G设备需要更长的数据接收超时时间
        client_socket.settimeout(3.0 if is_4g_device else 0.5)
        consecutive_timeouts = 0
        max_consecutive_timeouts = 10 if is_4g_device else 5  # 4G设备允许更多超时
        
        while True:
            try:
                chunk = client_socket.recv(4096)
                if not chunk:
                    logger.info(f"客户端主动断开连接: {client_ip}")
                    break
                if b'RECORDING_COMPLETE' in chunk:
                    audio_data.extend(chunk.split(b'RECORDING_COMPLETE')[0])
                    logger.info(f"收到录音完成信号: {client_ip} ({'4G' if is_4g_device else 'WiFi'}设备)")
                    break
                audio_data.extend(chunk)
                consecutive_timeouts = 0  # 重置超时计数
                
                # 记录接收进度
                if len(audio_data) % 8192 == 0:  # 每8KB记录一次
                    logger.debug(f"录音数据接收中: {len(audio_data)} bytes ({'4G' if is_4g_device else 'WiFi'}设备)")
                    
            except socket.timeout:
                consecutive_timeouts += 1
                if consecutive_timeouts >= max_consecutive_timeouts:
                    logger.warning(f"连续超时{consecutive_timeouts}次，停止接收: {client_ip} ({'4G' if is_4g_device else 'WiFi'}设备)")
                    break
                logger.debug(f"接收超时{consecutive_timeouts}/{max_consecutive_timeouts}: {client_ip}")
                continue

        if len(audio_data) < 1000:  # 约0.1秒的音频数据
            logger.warning("音频过短，不予处理")
            return

        with status_lock:
            service_status["recordings_received"] += 1
            service_status["last_recording"] = datetime.datetime.now().isoformat()

        expected_size = SAMPLE_WIDTH * CHANNELS
        if len(audio_data) % expected_size != 0:
            trimmed = (len(audio_data) // expected_size) * expected_size
            audio_data = audio_data[:trimmed]

        filename = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{mac_address.replace(':', '-')}.mp3"
        filepath = os.path.join(UPLOAD_FOLDER, filename)

        audio_segment = AudioSegment(
            data=audio_data,
            sample_width=SAMPLE_WIDTH,
            frame_rate=SAMPLE_RATE,
            channels=CHANNELS
        )
        audio_segment.export(filepath, format="mp3")
        logger.info(f"✅ 保存音频: {filepath} ({'4G' if is_4g_device else 'WiFi'}设备)")
        send_file_info_to_main_service(filename, mac_address, client_ip, is_4g_device)

    except Exception as e:
        logger.error(f"录音处理出错: {e}")
        with status_lock:
            service_status["errors"] += 1
    finally:
        try:
            client_socket.close()
        except:
            pass

def start_socket_server():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind(('0.0.0.0', PORT))
        server_socket.listen(5)
        logger.info(f"🎤 Socket服务监听 {PORT}")
        with status_lock:
            service_status["socket_server_active"] = True
        while True:
            client, addr = server_socket.accept()
            logger.info(f"🔌 新TCP连接: {addr}, 活跃线程数: {threading.active_count()-1}")
            threading.Thread(target=handle_client_recording, args=(client, addr), daemon=True).start()
    except Exception as e:
        logger.error(f"Socket服务失败: {e}")
        with status_lock:
            service_status["socket_server_active"] = False
            service_status["errors"] += 1
    finally:
        server_socket.close()

@app.route('/health', methods=['GET'])
def health_check():
    with status_lock:
        return jsonify({
            "service": "input_service",
            "status": "healthy" if service_status["socket_server_active"] else "unhealthy",
            "port": PORT,
            "recordings_received": service_status["recordings_received"],
            "last_recording": service_status["last_recording"],
            "errors": service_status["errors"],
            "timestamp": datetime.datetime.now().isoformat()
        })

def start_service():
    with status_lock:
        service_status["started"] = True
    threading.Thread(target=start_socket_server, daemon=True).start()
    logger.info("🚀 input_service 启动完毕")
    app.run(host='0.0.0.0', port=8087, threaded=True)

if __name__ == '__main__':
    start_service()

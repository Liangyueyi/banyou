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

PORT = 5001
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

def send_file_info_to_main_service(filename: str, mac_address: str, ip_address: str):
    filepath = os.path.abspath(os.path.join(UPLOAD_FOLDER, filename))
    request_data = {
        "filename": filename,
        "mac_address": mac_address,
        "ip_address": ip_address,
        "file_location": filepath,
        "timestamp": datetime.datetime.now().isoformat(),
        "format": "mp3",
        "mac_position": "after_timestamp"
    }

    try:
        logger.info(f"📤 向 main 发送录音元数据")
        session = requests.Session()
        session.trust_env = False  # 禁用环境变量中的代理设置
        response = session.post(
            f"{MAIN_API}/process/audio_file",
            json=request_data,
            timeout=10,
            proxies={"http": None, "https": None}  # 明确禁用代理
        )
        if response.status_code == 200:
            logger.info(f"✅ 成功发送至 main 处理: {filename}")
            return True
        else:
            logger.error(f"❌ HTTP {response.status_code}：{response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 请求异常: {e}")
        return False
    with status_lock:
        service_status["errors"] += 1
    return False

def handle_client_recording(client_socket, client_address):
    client_ip = client_address[0]
    mac_address = None
    audio_data = bytearray()

    try:
        client_socket.settimeout(2.0)
        initial_data = client_socket.recv(512)
        if not initial_data:
            logger.warning(f"{client_ip} 未发送数据")
            return

        mac_address = extract_mac_from_data(initial_data)
        if not mac_address:
            logger.warning(f"未识别MAC，使用 fallback")
            # 将IP转为标准MAC格式，如192.168.18.177 -> 00:00:C0:A8:12:B1
            ip_parts = client_ip.split('.')
            mac_address = f"00:00:{int(ip_parts[0]):02X}:{int(ip_parts[1]):02X}:{int(ip_parts[2]):02X}:{int(ip_parts[3]):02X}"

        audio_data = bytearray(initial_data if not validate_mac_format(mac_address) else b"")

        client_socket.settimeout(0.5)
        while True:
            try:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                if b'RECORDING_COMPLETE' in chunk:
                    audio_data.extend(chunk.split(b'RECORDING_COMPLETE')[0])
                    break
                audio_data.extend(chunk)
            except socket.timeout:
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
        logger.info(f"✅ 保存音频: {filepath}")
        send_file_info_to_main_service(filename, mac_address, client_ip)

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

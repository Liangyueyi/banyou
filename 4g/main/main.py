from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from datetime import datetime
import httpx
import uvicorn
import re
import logging
import base64
import json
import asyncio
import os
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path
import ipaddress

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("main_service")

app = FastAPI()

SERVICE_CONFIG = {
    "asr": "http://localhost:8081",
    "llm": "http://localhost:8082", 
    "tts": "http://localhost:8085",
    "upload": "http://localhost:8086",  
    "input": "http://localhost:8087"
}

OUTPUT_DIR = "output"
INPUT_DIR = "audio_mp3"

SERVICE_STATUS = {name: {"status": "unknown", "last_check": None} for name in SERVICE_CONFIG}

http_client = httpx.AsyncClient(
    timeout=30.0,
    limits=httpx.Limits(max_connections=100),
    transport=httpx.AsyncHTTPTransport(retries=3),
    proxies=None  # 明确禁用代理
)

# 中断标志字典
INTERRUPT_FLAGS = {}

# 音频缓存字典 - 4G设备拉取模式
AUDIO_CACHE = {}  # {mac: [{'audio': base64_data, 'index': idx, 'timestamp': time}]}

# 设备会话管理 - 4G模式下按MAC地址管理
DEVICE_SESSIONS = {}  # {mac: {'ip': ip, 'last_activity': timestamp, 'status': 'idle/recording/processing/audio_ready'}}

@app.on_event("startup")
async def print_service_status():
    logger.info("\n================= 服务启动健康检查 =================")
    for name, base_url in SERVICE_CONFIG.items():
        max_retries = 3
        retry_delay = 1
        last_error = None
        
        for attempt in range(max_retries):
            try:
                resp = await http_client.get(
                    f"{base_url}/health",
                    timeout=5.0
                )
                if resp.status_code == 200:
                    logger.info(f"[{name.upper()}] ✅ ONLINE - {base_url}")
                    SERVICE_STATUS[name]["status"] = "online"
                    break
                else:
                    last_error = f"HTTP {resp.status_code}"
                    logger.warning(f"[{name.upper()}] ⚠️ 尝试 {attempt+1}/{max_retries}: 服务响应异常 - {base_url} ({last_error})")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[{name.upper()}] ⚠️ 尝试 {attempt+1}/{max_retries}: 连接失败 - {base_url} ({last_error})")
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))
        else:
            logger.error(f"[{name.upper()}] ❌ 最终状态: 离线 - {base_url} ({last_error})")
            SERVICE_STATUS[name]["status"] = "offline"
    
    logger.info("================================================\n")

@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()

def validate_mac(mac: str) -> bool:
    return re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", mac) is not None

def validate_ip(ip: str) -> bool:
    return re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip) is not None

def sanitize_filename(filename: str) -> str:
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename

async def call_service(service: str, endpoint: str, data: dict):
    try:
        response = await http_client.post(
            f"{SERVICE_CONFIG[service]}/{endpoint}",
            json=data
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as e:
        logger.error(f"服务调用错误 {service}/{endpoint}: {e}")
        raise HTTPException(status_code=503, detail=f"Service {service} unavailable: {str(e)}")

async def upload_audio_file(file_path: str, mac: str, ip: str):
    try:
        response = await http_client.post(
            f"{SERVICE_CONFIG['upload']}/api/upload/file",
            params={"file_path": file_path, "mac": mac, "ip": ip}
        )
        response.raise_for_status()
        while os.path.exists(file_path):
            await asyncio.sleep(0.2)
        return response.json()
    except Exception as e:
        logger.error(f"上传失败: {str(e)}")
        return {"status": "error", "message": str(e)}

# ============ 4G协议接口 ============

@app.post("/api/4g/session_start")
async def session_start_4g(mac: str, ip: str):
    """4G设备开始录音会话"""
    if not validate_mac(mac):
        raise HTTPException(status_code=400, detail="无效的MAC地址")
    if not validate_ip(ip):
        raise HTTPException(status_code=400, detail="无效的IP地址")
    
    import time
    DEVICE_SESSIONS[mac] = {
        'ip': ip,
        'last_activity': time.time(),
        'status': 'recording'
    }
    # 清除该设备之前的音频缓存
    if mac in AUDIO_CACHE:
        del AUDIO_CACHE[mac]
    
    logger.info(f"4G设备开始录音会话: MAC={mac}, IP={ip}")
    return {"status": "ok", "message": f"会话已开始: {mac}"}

@app.post("/api/4g/request_audio")
async def request_audio_4g(mac: str):
    """4G设备请求TTS音频信息（流式PCM模式）"""
    if not validate_mac(mac):
        raise HTTPException(status_code=400, detail="无效的MAC地址")
    
    import time
    if mac in DEVICE_SESSIONS:
        DEVICE_SESSIONS[mac]['last_activity'] = time.time()
        DEVICE_SESSIONS[mac]['status'] = 'waiting'
    
    # 检查是否有缓存的音频
    if mac in AUDIO_CACHE and len(AUDIO_CACHE[mac]) > 0:
        # 返回音频队列信息，不包含实际音频数据
        total_count = len(AUDIO_CACHE[mac])
        current_index = AUDIO_CACHE[mac][0]['index']
        
        logger.info(f"4G设备请求音频信息: MAC={mac}, 当前音频index={current_index}, 总数={total_count}")
        
        return {
            "status": "audio_ready",
            "total_count": total_count,
            "current_index": current_index,
            "message": f"准备开始流式传输音频，共{total_count}条"
        }
    else:
        # 没有音频可用
        logger.debug(f"4G设备请求音频: MAC={mac}, 暂无音频可用")
        return {"status": "no_audio", "message": "暂无音频"}

@app.post("/api/4g/start_stream")
async def start_stream_4g(mac: str, audio_index: int):
    """开始流式传输指定音频片段"""
    if not validate_mac(mac):
        raise HTTPException(status_code=400, detail="无效的MAC地址")
    
    if mac not in AUDIO_CACHE or len(AUDIO_CACHE[mac]) == 0:
        raise HTTPException(status_code=404, detail="没有可用的音频数据")
    
    # 查找指定索引的音频
    audio_item = None
    for item in AUDIO_CACHE[mac]:
        if item['index'] == audio_index:
            audio_item = item
            break
    
    if not audio_item:
        raise HTTPException(status_code=404, detail=f"找不到索引为{audio_index}的音频")
    
    # 获取设备IP
    device_ip = DEVICE_SESSIONS.get(mac, {}).get('ip', '')
    if not device_ip:
        raise HTTPException(status_code=400, detail="设备IP未知")
    
    logger.info(f"开始流式传输音频: MAC={mac}, index={audio_index}, IP={device_ip}")
    
    # 将Base64音频转换为PCM并流式传输到ESP32
    try:
        import base64
        import tempfile
        import os
        
        # 解码Base64音频数据
        audio_data = base64.b64decode(audio_item['audio'])
        
        # 创建临时WAV文件
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
            temp_file.write(audio_data)
            temp_file_path = temp_file.name
        
        # 使用upload service进行流式传输
        response = await http_client.post(
            f"{SERVICE_CONFIG['upload']}/api/upload/file",
            params={"file_path": temp_file_path, "mac": mac, "ip": device_ip}
        )
        
        if response.status_code == 200:
            logger.info(f"流式传输启动成功: MAC={mac}, index={audio_index}")
            return {
                "status": "streaming", 
                "index": audio_index,
                "message": "PCM流式传输已开始"
            }
        else:
            logger.error(f"流式传输启动失败: {response.status_code}")
            # 清理临时文件
            try:
                os.unlink(temp_file_path)
            except:
                pass
            raise HTTPException(status_code=500, detail="启动流式传输失败")
            
    except Exception as e:
        logger.error(f"流式传输错误: {str(e)}")
        raise HTTPException(status_code=500, detail=f"流式传输错误: {str(e)}")

@app.post("/api/4g/audio_complete")
async def audio_complete_4g(mac: str, audio_index: int):
    """ESP32通知音频播放完成"""
    if not validate_mac(mac):
        raise HTTPException(status_code=400, detail="无效的MAC地址")
    
    import time
    if mac in DEVICE_SESSIONS:
        DEVICE_SESSIONS[mac]['last_activity'] = time.time()
    
    logger.info(f"ESP32音频播放完成通知: MAC={mac}, index={audio_index}")
    
    # 从缓存中移除已播放的音频
    if mac in AUDIO_CACHE:
        AUDIO_CACHE[mac] = [item for item in AUDIO_CACHE[mac] if item['index'] != audio_index]
        
        remaining_count = len(AUDIO_CACHE[mac])
        logger.info(f"音频播放完成: MAC={mac}, index={audio_index}, 剩余音频数量={remaining_count}")
        
        if remaining_count == 0:
            # 所有音频播放完成
            if mac in DEVICE_SESSIONS:
                DEVICE_SESSIONS[mac]['status'] = 'idle'
            logger.info(f"所有音频播放完成: MAC={mac}")
            return {
                "status": "all_complete",
                "message": "所有音频播放完成"
            }
        else:
            # 还有更多音频
            next_audio = AUDIO_CACHE[mac][0]
            return {
                "status": "next_ready",
                "next_index": next_audio['index'],
                "remaining_count": remaining_count,
                "message": f"准备播放下一条音频，剩余{remaining_count}条"
            }
    
    return {"status": "ok", "message": "播放完成确认"}

@app.post("/api/4g/interrupt")
async def interrupt_4g(mac: str):
    """4G设备中断播放"""
    if not validate_mac(mac):
        raise HTTPException(status_code=400, detail="无效的MAC地址")
    
    INTERRUPT_FLAGS[mac] = True
    # 清除音频缓存
    if mac in AUDIO_CACHE:
        del AUDIO_CACHE[mac]
    # 更新设备状态
    if mac in DEVICE_SESSIONS:
        DEVICE_SESSIONS[mac]['status'] = 'idle'
        DEVICE_SESSIONS[mac]['last_activity'] = time.time()
    
    logger.info(f"4G设备中断播放: MAC={mac}")
    return {"status": "ok", "message": f"已中断播放: {mac}"}

@app.post("/interrupt")
async def interrupt(mac: str):
    """兼容性接口 - 原WiFi版本的中断接口"""
    INTERRUPT_FLAGS[mac] = True
    return {"status": "ok", "message": f"已请求中断 {mac}"}

@app.post("/process/audio_file")
async def process_audio_file(file_info: dict):
    logger.info(f"收到音频处理请求: {json.dumps(file_info, indent=2)}")
    
    mac = file_info.get("mac_address", "").strip()
    ip = file_info.get("ip_address", "").strip()
    device_type = file_info.get("device_type", "WiFi")  # 新增设备类型字段

    if not validate_mac(mac):
        logger.error(f"无效的MAC地址: {mac}")
        raise HTTPException(status_code=400, detail="无效的 MAC 地址")
    if not validate_ip(ip):
        logger.error(f"无效的IP地址: {ip}")
        raise HTTPException(status_code=400, detail="无效的 IP 地址")

    # 更新设备会话信息
    import time
    if device_type == "4G":
        DEVICE_SESSIONS[mac] = {
            'ip': ip,
            'last_activity': time.time(),
            'status': 'processing',
            'device_type': '4G'
        }
        logger.info(f"更新4G设备会话状态: MAC={mac}, 状态=processing")
    
    try:
        file_path = file_info["file_location"]
        logger.info(f"尝试读取音频文件: {file_path} (设备类型: {device_type})")
        
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        audio_data = base64.b64encode(audio_bytes).decode("utf-8")
        logger.info(f"成功读取音频文件，大小: {len(audio_bytes)} bytes (设备类型: {device_type})")
    except Exception as e:
        logger.error(f"无法读取音频文件: {str(e)}")
        if device_type == "4G" and mac in DEVICE_SESSIONS:
            DEVICE_SESSIONS[mac]['status'] = 'error'
        raise HTTPException(status_code=500, detail="无法读取音频文件")

    logger.info(f"开始处理音频: mac={mac}, ip={ip}, 设备类型={device_type}, 文件={file_path}")

    asr_result = await call_service("asr", "api/speech-to-text", {
        "audio_data": audio_data,
        "mac_address": mac,
        "ip_address": ip,
        "options": {}
    })

    if "text" not in asr_result or not asr_result["text"].strip():
        return {"status": "error", "message": "ASR 无有效返回", "asr_result": asr_result}

    INTERRUPT_FLAGS[mac] = False

    try:
        response = await http_client.post(
            f"{SERVICE_CONFIG['llm']}/process",
            json={"text": asr_result["text"], "mac": mac, "context": {}},
            timeout=None
        )
        if response.status_code != 200:
            raise Exception(f"LLM 响应失败: {response.status_code}")

        idx = 0
        results = []

        async for line in response.aiter_lines():
            if INTERRUPT_FLAGS.get(mac):
                logger.warning(f"播放被用户中断: {mac}")
                break
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                sentence = obj.get("content", "").strip()
                if not sentence:
                    continue
                
                # 过滤掉<think>标签内容
                sentence = re.sub(r'<think>.*?</think>', '', sentence, flags=re.DOTALL)
                sentence = sentence.strip()
                if not sentence:
                    continue

                logger.info(f"[TTS] 第{idx+1}句: {sentence}")

                tts_result = await call_service("tts", "api/tts/generate", {
                    "text": sentence,
                    "mac": mac,
                    "ip": ip,
                    "audio_format": "wav"
                })

                if "audio" not in tts_result:
                    logger.warning(f"TTS失败：跳过第{idx+1}句")
                    continue

                # 4G设备：缓存音频数据供设备轮询拉取
                if mac not in AUDIO_CACHE:
                    AUDIO_CACHE[mac] = []
                
                import time
                AUDIO_CACHE[mac].append({
                    'audio': tts_result["audio"],  # 直接存储base64数据
                    'index': idx,
                    'timestamp': time.time(),
                    'text': sentence
                })
                
                logger.info(f"[4G] 音频已缓存: MAC={mac}, index={idx}, 缓存队列长度={len(AUDIO_CACHE[mac])}")
                
                results.append({
                    "index": idx,
                    "text": sentence,
                    "cached": True,
                    "mode": "4G"
                })
                
                idx += 1
            except Exception as e:
                logger.warning(f"处理LLM响应失败: {e}")
                continue

        # 更新4G设备会话状态为等待播放
        if device_type == "4G" and mac in DEVICE_SESSIONS:
            DEVICE_SESSIONS[mac]['status'] = 'audio_ready'
            DEVICE_SESSIONS[mac]['last_activity'] = time.time()
            logger.info(f"4G设备音频处理完成: MAC={mac}, 缓存音频数量={len(AUDIO_CACHE.get(mac, []))}")

        return {
            "status": "success",
            "asr_text": asr_result["text"],
            "segments": results,
            "upload_status": "completed",
            "device_type": device_type
        }

    except Exception as e:
        logger.error(f"流式LLM处理失败: {str(e)}")
        # 更新4G设备错误状态
        if device_type == "4G" and mac in DEVICE_SESSIONS:
            DEVICE_SESSIONS[mac]['status'] = 'error'
            DEVICE_SESSIONS[mac]['last_activity'] = time.time()
        return {"status": "error", "message": str(e), "device_type": device_type}

# ============ BLE位置数据接收接口 ============

@app.post("/api/ble/location")
async def receive_ble_location(request: dict):
    """
    接收来自BLE服务的位置数据
    """
    logger.info("=" * 80)
    logger.info("🔵 收到BLE位置数据:")
    logger.info(f"   BLE设备地址: {request.get('ble_address', 'N/A')}")
    logger.info(f"   位置坐标: 纬度 {request.get('latitude', 'N/A')}, 经度 {request.get('longitude', 'N/A')}")
    logger.info(f"   定位精度: {request.get('accuracy', 'N/A')} 米")
    logger.info(f"   时间戳: {request.get('timestamp', 'N/A')}")
    logger.info(f"   请求ID: {request.get('request_id', 'N/A')}")
    
    # 如果有iBeacon信息，也打印出来
    if request.get('uuid'):
        logger.info(f"   iBeacon UUID: {request.get('uuid')}")
    if request.get('major') is not None:
        logger.info(f"   iBeacon Major: {request.get('major')}")
    if request.get('minor') is not None:
        logger.info(f"   iBeacon Minor: {request.get('minor')}")
    if request.get('tx_power') is not None:
        logger.info(f"   iBeacon 发射功率: {request.get('tx_power')} dBm")
    
    logger.info("=" * 80)
    
    # 返回成功响应
    return {
        "status": "success",
        "message": "BLE位置数据已接收并记录",
        "received_at": datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    results = {}
    for name, base_url in SERVICE_CONFIG.items():
        try:
            resp = await http_client.get(f"{base_url}/health")
            SERVICE_STATUS[name] = {"status": "online" if resp.status_code == 200 else "error", "last_check": datetime.now().isoformat(), "details": resp.json()}
        except Exception as e:
            SERVICE_STATUS[name] = {"status": "offline", "last_check": datetime.now().isoformat(), "details": {"error": str(e)}}
        results[name] = SERVICE_STATUS[name]
    return {"status": "ok", "service": "main_controller", "timestamp": datetime.now().isoformat(), "version": "1.0.0", "services": results}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

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

@app.post("/interrupt")
async def interrupt(mac: str):
    INTERRUPT_FLAGS[mac] = True
    return {"status": "ok", "message": f"已请求中断 {mac}"}

@app.post("/process/audio_file")
async def process_audio_file(file_info: dict):
    logger.info(f"收到音频处理请求: {json.dumps(file_info, indent=2)}")
    
    mac = file_info.get("mac_address", "").strip()
    ip = file_info.get("ip_address", "").strip()

    if not validate_mac(mac):
        logger.error(f"无效的MAC地址: {mac}")
        raise HTTPException(status_code=400, detail="无效的 MAC 地址")
    if not validate_ip(ip):
        logger.error(f"无效的IP地址: {ip}")
        raise HTTPException(status_code=400, detail="无效的 IP 地址")

    try:
        file_path = file_info["file_location"]
        logger.info(f"尝试读取音频文件: {file_path}")
        
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        audio_data = base64.b64encode(audio_bytes).decode("utf-8")
        logger.info(f"成功读取音频文件，大小: {len(audio_bytes)} bytes")
    except Exception as e:
        logger.error(f"无法读取音频文件: {str(e)}")
        raise HTTPException(status_code=500, detail="无法读取音频文件")

    logger.info(f"开始处理音频: mac={mac}, ip={ip}, 文件={file_path}")

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

                filename = f"{mac.replace(':', '_')}_{ip}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{idx}_{uuid.uuid4().hex[:6]}.wav"
                filepath = os.path.join(OUTPUT_DIR, filename)
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(tts_result["audio"]))

                await upload_audio_file(str(Path(filepath).as_posix()), mac, ip)

                results.append({
                    "index": idx,
                    "text": sentence,
                    "file": filepath
                })
                idx += 1
            except Exception as e:
                logger.warning(f"处理LLM响应失败: {e}")
                continue

        return {
            "status": "success",
            "asr_text": asr_result["text"],
            "segments": results,
            "upload_status": "completed"
        }

    except Exception as e:
        logger.error(f"流式LLM处理失败: {str(e)}")
        return {"status": "error", "message": str(e)}

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

import os
import re
import uuid
import time
import base64
import json
import asyncio
import logging
from typing import Dict, Any, Optional
from datetime import datetime

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("tts_service")

# 复用的全局 HTTP client
http_client = httpx.AsyncClient(timeout=10.0)


# 创建FastAPI应用
app = FastAPI(title="TTS Service", description="文本转语音服务API", version="1.0.0")

# 配置跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 火山引擎TTS配置
VOLCANO_TTS_CONFIG = {
    "appid": os.environ.get("VOLCANO_TTS_APPID", "7243323501"),
    "access_token": os.environ.get("VOLCANO_TTS_ACCESS_TOKEN", "O0Cmbm-uam8suT3kZtNXVnmgHIgi9xyy"),
    "cluster": os.environ.get("VOLCANO_TTS_CLUSTER", "volcano_tts"),
    "host": "openspeech.bytedance.com",
    "api_url": "https://openspeech.bytedance.com/api/v1/tts",
    "default_voice": "BV001_streaming",  
}

# TTS请求模型
class TTSRequest(BaseModel):
    text: str = Field(..., description="需要合成语音的文本")
    mac: str = Field(..., description="客户端MAC地址")
    ip: str = Field(None, description="客户端IP地址")
    audio_format: str = Field("wav", description="音频格式(mp3/wav)")
    speech_rate: float = Field(1.0, description="语速(0.5-2.0)", ge=0.5, le=2.0)
    volume: float = Field(1.0, description="音量(0.5-2.0)", ge=0.5, le=2.0)
    pitch: float = Field(1.0, description="音高(0.5-2.0)", ge=0.5, le=2.0)
    voice_type: Optional[str] = Field(None, description="声音类型")

    # 使用 @field_validator 替代 @validator
    @field_validator('mac')
    def validate_mac(cls, v):
        if not re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", v):
            raise ValueError('MAC地址格式无效')
        return v

    @field_validator('ip')
    def validate_ip(cls, v):
        if v is not None and not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", v):
            raise ValueError('IP地址格式无效')
        return v


# TTS响应模型
class TTSResponse(BaseModel):
    audio: str = Field(..., description="Base64编码的音频数据")
    format: str = Field(..., description="音频格式")
    duration: float = Field(..., description="音频时长(秒)")
    request_id: str = Field(..., description="请求ID")


# 设备会话管理器
class DeviceSessionManager:
    def __init__(self):
        self.devices = {}  # {mac: {ips: {ip: timestamp}, requests: []}}
    
    def register_request(self, mac, ip):
        """记录设备请求"""
        now = time.time()
        if mac not in self.devices:
            self.devices[mac] = {"ips": {}, "requests": []}

        # 更新IP记录(如果有提供)
        if ip:
            self.devices[mac]["ips"][ip] = now

        # 记录请求时间
        self.devices[mac]["requests"].append(now)

        # 清理过期请求记录（保留最近5分钟的）
        self.devices[mac]["requests"] = [
            t for t in self.devices[mac]["requests"] if now - t < 300
        ]

        return len(self.devices[mac]["requests"])  # 返回设备最近请求数量

    def get_device_requests(self, mac, window=300):
        """获取设备在指定时间窗口内的请求数"""
        if mac not in self.devices:
            return 0

        now = time.time()
        return len([t for t in self.devices[mac]["requests"] if now - t < window])

    def get_device_ips(self, mac):
        """获取设备使用过的IP列表"""
        if mac not in self.devices:
            return []
        return list(self.devices[mac]["ips"].keys())


# 限流器 - 简易版
class RateLimiter:
    def __init__(self, rate_limit=100, window_size=60):
        self.rate_limit = rate_limit  # 每个窗口允许的最大请求数
        self.window_size = window_size  # 窗口大小(秒)
        self.requests = {}  # {client_id: [timestamps]}
        
    async def allow_request(self, client_id):
        now = time.time()
        if client_id not in self.requests:
            self.requests[client_id] = []

        # 清理窗口外的请求
        self.requests[client_id] = [ts for ts in self.requests[client_id] if now - ts < self.window_size]

        # 检查是否超过限制
        if len(self.requests[client_id]) >= self.rate_limit:
            return False

        # 允许请求并记录
        self.requests[client_id].append(now)
        return True


# 创建设备管理器和限流器
device_manager = DeviceSessionManager()
rate_limiter = RateLimiter(rate_limit=20, window_size=60)  # 每分钟20次请求

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {
        "status": "ok",
        "service": "tts",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    }

@app.post("/api/tts/generate")
async def generate_speech(request: TTSRequest):
    """
    文本转语音API
    """
    # 生成请求ID
    request_id = str(uuid.uuid4())
    
    # 记录设备请求
    recent_requests = device_manager.register_request(request.mac, request.ip)
    
    logger.info(f"[{request_id}] 收到TTS请求: MAC={request.mac}, "
               f"IP={request.ip or '未提供'}, 文本长度={len(request.text)}, "
               f"最近请求数={recent_requests}")
    
    # 检查速率限制
    if not await rate_limiter.allow_request(request.mac):
        logger.warning(f"[{request_id}] 请求限速: MAC={request.mac}, 超过每分钟20次限制")
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    
    try:
        # 准备TTS请求
        voice_type = request.voice_type or VOLCANO_TTS_CONFIG["default_voice"]
        audio_format = request.audio_format.lower() if request.audio_format else "wav"
        
        # 检查音频格式
        if audio_format not in ["mp3", "wav"]:
            logger.error(f"[{request_id}] 格式错误: MAC={request.mac}, 格式={audio_format}")
            raise HTTPException(status_code=400, detail="不支持的音频格式，请使用mp3或wav")
        
        # 准备火山引擎请求
        volcano_request = {
            "app": {
                "appid": VOLCANO_TTS_CONFIG["appid"],
                "token": VOLCANO_TTS_CONFIG["access_token"],
                "cluster": VOLCANO_TTS_CONFIG["cluster"]
            },
            "user": {
                "uid": request.mac.replace(":", "").replace("-", "")  # 使用MAC地址作为用户ID
            },
            "audio": {
                "voice_type": voice_type,
                "encoding": audio_format,
                "speed_ratio": request.speech_rate,
                "volume_ratio": request.volume,
                "pitch_ratio": request.pitch,
            },
            "request": {
                "reqid": request_id,
                "text": request.text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson"
            }
        }
        
        headers = {"Authorization": f"Bearer;{VOLCANO_TTS_CONFIG['access_token']}"}
        
        # 发送请求到火山引擎TTS服务
        start_time = time.time()
        resp = await http_client.post(
            VOLCANO_TTS_CONFIG["api_url"], 
            json=volcano_request,
            headers=headers,
        )
        
        process_time = time.time() - start_time
        
        # 处理响应
        if resp.status_code != 200:
            logger.error(f"[{request_id}] TTS服务错误: MAC={request.mac}, 状态码={resp.status_code}, 响应={resp.text}")
            raise HTTPException(status_code=resp.status_code, detail="语音合成服务返回错误")
            
        result = resp.json()
        
        # 检查火山引擎响应是否包含语音数据
        if "data" not in result:
            error_msg = result.get("message", "未知错误")
            logger.error(f"[{request_id}] TTS服务错误: MAC={request.mac}, 错误={error_msg}")
            raise HTTPException(status_code=500, detail=f"语音合成失败: {error_msg}")
            
        # 估算音频时长（简单估算）
        text_length = len(request.text)
        estimated_duration = text_length * 0.2 / request.speech_rate  # 简单估算中文每字约0.2秒
        
        logger.info(f"[{request_id}] TTS请求完成: MAC={request.mac}, IP={request.ip or '未提供'}, "
                   f"耗时={process_time:.2f}秒, 预计音频时长={estimated_duration:.2f}秒")
        
        # 返回响应
        return {
            "audio": result["data"],  # 已经是Base64编码的
            "format": audio_format,
            "duration": estimated_duration,
            "request_id": request_id,
            "mac": request.mac  # 返回设备MAC地址，确保客户端可以对应请求
        }
    
    except httpx.TimeoutException:
        logger.error(f"[{request_id}] TTS请求超时: MAC={request.mac}, 文本={request.text[:20]}...")
        raise HTTPException(status_code=504, detail="语音合成服务超时")
    except httpx.HTTPError as e:
        logger.error(f"[{request_id}] TTS HTTP错误: MAC={request.mac}, 错误={str(e)}")
        raise HTTPException(status_code=502, detail=f"语音合成服务错误: {str(e)}")
    except Exception as e:
        logger.exception(f"[{request_id}] TTS处理异常: MAC={request.mac}, 错误={str(e)}")
        raise HTTPException(status_code=500, detail=f"处理过程错误: {str(e)}")


@app.post("/api/tts/batch")
async def batch_generate_speech(requests: list[TTSRequest]):
    """
    批量文本转语音API
    同时处理多条文本，适用于需要顺序播放的场景
    """
    if not requests:
        raise HTTPException(status_code=400, detail="请求列表不能为空")
        
    batch_id = str(uuid.uuid4())
    logger.info(f"[{batch_id}] 收到批量TTS请求: 项目数={len(requests)}, MAC={requests[0].mac}")
    
    results = []
    for i, request in enumerate(requests):
        try:
            # 为每个子请求生成唯一ID
            sub_request_id = f"{batch_id}-{i}"
            
            # 记录设备请求
            device_manager.register_request(request.mac, request.ip)
            
            # 处理单个请求
            result = await generate_speech(request)
            results.append({
                "index": i,
                "audio": result["audio"],
                "format": result["format"],
                "duration": result["duration"],
                "request_id": sub_request_id,
                "mac": request.mac,
                "status": "success"
            })
            
        except Exception as e:
            logger.error(f"[{batch_id}] 批量处理第{i}项出错: MAC={request.mac}, 错误={str(e)}")
            results.append({
                "index": i,
                "audio": "",
                "format": request.audio_format,
                "duration": 0,
                "request_id": f"{batch_id}-{i}",
                "mac": request.mac,
                "status": "error",
                "error": str(e)
            })
    
    logger.info(f"[{batch_id}] 批量TTS请求完成: 项目数={len(requests)}, 成功数={sum(1 for r in results if r['status'] == 'success')}")
    
    return {
        "batch_id": batch_id,
        "results": results,
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "success"),
        "mac": requests[0].mac if requests else None
    }

@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()


if __name__ == "__main__":
    logger.info("启动TTS服务...")
    uvicorn.run("tts_service:app", host="0.0.0.0", port=8085, reload=False)

import os
import base64
import tempfile
import logging
import json
import time
import uuid
import re
from typing import Optional, Dict, Any
import sys

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, validator

# 导入FunASR
try:
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
except ImportError:
    logging.warning("无法导入FunASR。使用模拟ASR功能。")
    # 创建模拟函数，用于在未安装FunASR时提供基本功能
    class MockAutoModel:
        def __init__(self, **kwargs):
            pass
        
        def generate(self, **kwargs):
            return [{"text": "这是一个模拟的ASR结果，实际部署时请安装FunASR库。", "confidence": 0.8}]
    
    AutoModel = MockAutoModel
    def rich_transcription_postprocess(text):
        return text

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("asr_service.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 请求模型 - 主调度服务使用的格式
class ASRRequest(BaseModel):
    audio_data: str  # Base64编码的音频数据
    mac_address: str  # 客户端MAC地址
    ip_address: str  # 客户端IP地址
    options: Optional[Dict[str, Any]] = None  # 其他选项,如语言设置、采样率等
    
    @validator('mac_address')
    def validate_mac(cls, v):
        if not re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", v):
            raise ValueError('MAC地址格式无效')
        return v
    
    @validator('ip_address')
    def validate_ip(cls, v):
        if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", v):
            raise ValueError('IP地址格式无效')
        return v

# 响应模型 - 返回给主调度服务的格式
class ASRResponse(BaseModel):
    text: str  # 识别出的文本
    confidence: float  # 识别置信度
    status: str  # 处理状态
    error: Optional[str] = None  # 错误信息

# 创建FastAPI应用
app = FastAPI(
    title="语音识别(ASR)服务",
    description="提供语音转文字功能的API服务",
    version="1.0.0"
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境中应该设置为特定的域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局变量
SUPPORTED_AUDIO_FORMATS = [".wav", ".mp3", ".ogg", ".flac", ".m4a", ".webm"]
DEFAULT_MODEL_NAME = "iic/SenseVoiceSmall"  # 默认使用SenseVoiceSmall模型
DEFAULT_DEVICE = os.environ.get("ASR_DEVICE", "cuda:0" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu")
DEFAULT_VAD_MODEL = "fsmn-vad"

# 缓存机制，避免频繁地相同请求处理
request_cache = {}
CACHE_TTL = 300  # 缓存有效期(秒)

# 设备会话存储
class DeviceSessionManager:
    def __init__(self):
        self.devices = {}  # {mac: {ip: last_activity_time, requests: [...]}}
    
    def register_request(self, mac, ip):
        """记录设备请求"""
        now = time.time()
        if mac not in self.devices:
            self.devices[mac] = {"ips": {}, "requests": []}
        
        # 更新IP记录
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

# 创建设备管理器
device_manager = DeviceSessionManager()

# SenseVoice ASR模型封装类
class SenseVoiceASR:
    """SenseVoice ASR模型封装类"""
    
    def __init__(self, 
                 model_dir: str = DEFAULT_MODEL_NAME, 
                 device: str = DEFAULT_DEVICE,
                 vad_model: str = DEFAULT_VAD_MODEL):
        """
        初始化SenseVoice ASR模型
        
        参数:
            model_dir: 模型目录或ModelScope模型ID
            device: 使用的设备，如"cuda:0"或"cpu"
            vad_model: VAD模型名称
        """
        self.model_dir = model_dir
        self.device = device
        
        # 初始化模型
        logger.info(f"正在加载SenseVoice模型：{model_dir}，设备：{device}")
        try:
            self.model = AutoModel(
                model=model_dir,
                trust_remote_code=True,
                vad_model=vad_model,
                vad_kwargs={"max_single_segment_time": 30000},
                device=device,
                disable_update=True,
                local_model_path="C:\\Users\\ASUS\\.cache\\modelscope\\hub\\models\\iic"
            )
            logger.info("SenseVoice模型加载完成")
        except Exception as e:
            logger.error(f"模型加载失败: {str(e)}")
            self.model = None
    
    def recognize_file(self, 
                       audio_file: str, 
                       language: str = "auto",
                       use_itn: bool = True,
                       batch_size_s: int = 60,
                       merge_vad: bool = True,
                       merge_length_s: int = 15) -> Dict[str, Any]:
        """
        从音频文件识别文本
        
        参数:
            audio_file: 音频文件路径
            language: 语言代码("auto", "zh", "en", "yue", "ja", "ko", "nospeech")
            use_itn: 是否使用逆文本规范化
            batch_size_s: 批处理大小(秒)
            merge_vad: 是否合并VAD结果
            merge_length_s: 合并长度(秒)
            
        返回:
            包含识别文本和元数据的字典
        """
        if not os.path.exists(audio_file):
            raise FileNotFoundError(f"音频文件不存在：{audio_file}")
        
        if self.model is None:
            # 模型未成功加载，返回模拟结果
            return {
                "text": "ASR模型未加载，无法进行语音识别。请检查日志获取详情。",
                "confidence": 0.1,
                "status": "error"
            }
        
        # 执行识别
        try:
            result = self.model.generate(
                input=audio_file,
                cache={},
                language=language,
                use_itn=use_itn,
                batch_size_s=batch_size_s,
                merge_vad=merge_vad,
                merge_length_s=merge_length_s,
            )
            
            if not result:
                return {"text": "", "confidence": 0.0, "status": "no_speech_detected"}
            
            # 后处理文本
            text = rich_transcription_postprocess(result[0]["text"])
            
            # 尝试获取置信度，如果没有则使用默认值
            confidence = 0.8
            if "confidence" in result[0]:
                confidence = result[0]["confidence"]
            
            # 构建响应
            return {
                "text": text,
                "confidence": confidence,
                "status": "success",
                "raw_result": result[0]  # 包含原始结果
            }
        except Exception as e:
            logger.error(f"识别过程出错: {str(e)}")
            return {
                "text": f"识别过程出错: {str(e)}",
                "confidence": 0.0,
                "status": "error"
            }
    
    def recognize_bytes(self, 
                       audio_bytes: bytes,
                       language: str = "auto",
                       use_itn: bool = True) -> Dict[str, Any]:
        """
        从音频二进制数据识别文本
        
        参数:
            audio_bytes: 音频文件的二进制数据
            language: 语言代码
            use_itn: 是否使用逆文本规范化
            
        返回:
            包含识别文本和元数据的字典
        """
        # 将二进制数据保存为临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_audio_path = temp_audio.name
            temp_audio.write(audio_bytes)
        
        try:
            # 调用文件识别方法
            result = self.recognize_file(temp_audio_path, language, use_itn)
            return result
        finally:
            # 清理临时文件
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)
    
    def recognize_base64(self, 
                        audio_base64: str,
                        language: str = "auto",
                        use_itn: bool = True) -> Dict[str, Any]:
        """
        从Base64编码的音频数据识别文本
        
        参数:
            audio_base64: Base64编码的音频数据
            language: 语言代码
            use_itn: 是否使用逆文本规范化
            
        返回:
            包含识别文本和元数据的字典
        """
        try:
            # 解码Base64数据
            if "," in audio_base64:
                # 处理可能的Data URL格式 (e.g. "data:audio/wav;base64,...")
                audio_base64 = audio_base64.split(",")[1]
                
            audio_bytes = base64.b64decode(audio_base64)
            return self.recognize_bytes(audio_bytes, language, use_itn)
        except Exception as e:
            logger.error(f"Base64解码错误: {str(e)}")
            return {
                "text": f"音频数据解码失败: {str(e)}",
                "confidence": 0.0,
                "status": "error"
            }

# 全局ASR实例
asr = None

# 初始化ASR模型
def initialize_asr():
    """初始化ASR模型"""
    global asr
    
    try:
        asr = SenseVoiceASR(
            model_dir=DEFAULT_MODEL_NAME,
            device=DEFAULT_DEVICE,
            vad_model=DEFAULT_VAD_MODEL
        )
    except Exception as e:
        logger.error(f"Failed to initialize ASR model: {e}")
        asr = None

# 启动时初始化模型
@app.on_event("startup")
async def startup_event():
    """服务启动时的初始化工作"""
    logger.info("Starting ASR service...")
    # 初始化模型
    initialize_asr()

# 健康检查端点
@app.get("/health")
async def health_check():
    """服务健康检查端点"""
    return {
        "status": "healthy", 
        "service": "语音识别(ASR)服务",
        "model": DEFAULT_MODEL_NAME,
        "device": DEFAULT_DEVICE,
        "models_loaded": asr is not None
    }

# 语音识别端点 - 接口与main.py中定义的保持一致
@app.post("/api/speech-to-text", response_model=ASRResponse)
async def speech_to_text(request: ASRRequest):
    """
    将语音转换为文本
    
    接收Base64编码的音频数据，返回识别的文本结果
    """
    # 生成请求ID用于日志跟踪
    request_id = str(uuid.uuid4())
    
    # 记录设备请求
    recent_requests = device_manager.register_request(request.mac_address, request.ip_address)
    
    logger.info(f"[{request_id}] 收到ASR请求: MAC={request.mac_address}, IP={request.ip_address}, "
                f"最近请求数={recent_requests}")
    
    # 检查缓存
    cache_key = hash(request.audio_data)
    if cache_key in request_cache:
        cache_entry = request_cache[cache_key]
        if time.time() - cache_entry["timestamp"] < CACHE_TTL:
            logger.info(f"[{request_id}] 命中缓存，返回缓存结果，设备MAC={request.mac_address}")
            return ASRResponse(**cache_entry["result"])
    
    # 检查ASR模型是否已初始化
    if asr is None:
        logger.error(f"[{request_id}] ASR模型未初始化! 设备MAC={request.mac_address}")
        return ASRResponse(
            text="ASR服务未正确初始化",
            confidence=0.0,
            status="failed",
            error="ASR model not initialized"
        )
    
    # 解析选项
    options = request.options or {}
    language = options.get("language", "auto")
    use_itn = options.get("use_itn", True)
    
    try:
        # 使用SenseVoice进行识别
        result = asr.recognize_base64(
            request.audio_data, 
            language=language,
            use_itn=use_itn
        )
        
        # 构建响应
        response = {
            "text": result["text"],
            "confidence": result.get("confidence", 0.8),
            "status": result["status"],
            "error": None if result["status"] == "success" else "语音识别出错"
        }
        
        # 更新缓存
        if result["status"] == "success":
            request_cache[cache_key] = {
                "result": response,
                "timestamp": time.time()
            }
            
            # 清理过期缓存
            current_time = time.time()
            expired_keys = [k for k, v in request_cache.items() 
                          if current_time - v["timestamp"] > CACHE_TTL]
            for key in expired_keys:
                del request_cache[key]
        
        logger.info(f"[{request_id}] ASR处理完成: MAC={request.mac_address}, 状态={result['status']}, "
                   f"文本长度={len(result.get('text', ''))}")
        
        return ASRResponse(**response)
    
    except Exception as e:
        logger.error(f"[{request_id}] ASR处理错误: MAC={request.mac_address}, 错误={str(e)}")
        return ASRResponse(
            text="",
            confidence=0.0,
            status="failed",
            error=str(e)
        )

# 上传音频文件端点
@app.post("/api/upload-audio", response_model=ASRResponse)
async def upload_audio(
    file: UploadFile = File(...),
    mac_address: str = Form(...),
    ip_address: str = Form(...),
    options: Optional[str] = Form("{}")
):
    """
    通过文件上传进行语音识别
    
    接收上传的音频文件，返回识别结果
    """
    request_id = str(uuid.uuid4())
    
    # 记录设备请求
    recent_requests = device_manager.register_request(mac_address, ip_address)
    
    logger.info(f"[{request_id}] 收到文件上传请求: MAC={mac_address}, IP={ip_address}, "
                f"文件名={file.filename}, 最近请求数={recent_requests}")
    
    # 检查ASR模型是否已初始化
    if asr is None:
        logger.error(f"[{request_id}] ASR模型未初始化! 设备MAC={mac_address}")
        return ASRResponse(
            text="ASR服务未正确初始化",
            confidence=0.0,
            status="failed",
            error="ASR model not initialized"
        )
    
    # 解析选项
    try:
        options_dict = json.loads(options) if options else {}
    except json.JSONDecodeError:
        logger.error(f"[{request_id}] 选项JSON格式无效: MAC={mac_address}")
        return ASRResponse(
            text="",
            confidence=0.0,
            status="failed",
            error="Invalid options JSON format"
        )
    
    language = options_dict.get("language", "auto")
    use_itn = options_dict.get("use_itn", True)
    
    # 保存上传的文件
    temp_dir = tempfile.gettempdir()
    temp_file_path = os.path.join(temp_dir, f"upload_{uuid.uuid4()}{os.path.splitext(file.filename)[1]}")
    
    try:
        # 保存上传的文件
        with open(temp_file_path, "wb") as f:
            content = await file.read()
            f.write(content)
            
        logger.info(f"[{request_id}] 文件已保存: MAC={mac_address}, 临时文件={temp_file_path}")
        
        # 使用SenseVoice进行识别
        result = asr.recognize_file(
            temp_file_path,
            language=language,
            use_itn=use_itn
        )
        
        # 构建响应
        response = {
            "text": result["text"],
            "confidence": result.get("confidence", 0.8),
            "status": result["status"],
            "error": None if result["status"] == "success" else "语音识别出错"
        }
        
        logger.info(f"[{request_id}] 文件处理完成: MAC={mac_address}, 状态={result['status']}, "
                   f"文本长度={len(result.get('text', ''))}")
        
        return ASRResponse(**response)
        
    except Exception as e:
        logger.error(f"[{request_id}] 文件处理错误: MAC={mac_address}, 错误={str(e)}")
        return ASRResponse(
            text="",
            confidence=0.0,
            status="failed",
            error=str(e)
        )
    
    finally:
        # 删除临时文件
        try:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                logger.debug(f"[{request_id}] 临时文件已删除: {temp_file_path}")
        except Exception as e:
            logger.warning(f"[{request_id}] 删除临时文件失败 {temp_file_path}: {e}")

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting ASR service...")
    uvicorn.run("asr_service:app", host="0.0.0.0", port=8081, reload=False)

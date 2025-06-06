from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import os
import wave
import socket
import threading
import time
import logging
from pydub import AudioSegment
from typing import List

app = FastAPI(title="Upload Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = "output"
ESP32_PORT = 5002
SERVICE_PORT = 8086
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2

active_uploads = {}  # 用mac地址管理上传状态
should_exit = False
upload_lock = threading.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("upload_service")


class InterruptRequest(BaseModel):
    ip: str


class BatchUploadRequest(BaseModel):
    file_paths: List[str]


class AudioUploader:
    def __init__(self, ip):
        self.ip = ip
        self.port = ESP32_PORT
        self.socket = None
        self.interrupt = False
        self.current_file = None

    def connect(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(2)
            self.socket.connect((self.ip, self.port))
            return True
        except Exception as e:
            logger.error(f"连接失败: {e}")
            self.close()
            return False

    def close(self):
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

    def upload_wav_as_pcm(self, file_path):
        self.current_file = file_path
        self.interrupt = False

        try:
            audio = AudioSegment.from_wav(file_path)
            audio = audio.set_frame_rate(TARGET_SAMPLE_RATE)
            audio = audio.set_channels(TARGET_CHANNELS)
            audio = audio.set_sample_width(TARGET_SAMPLE_WIDTH)
            pcm_data = audio.raw_data
        except Exception as e:
            logger.error(f"读取 WAV 失败: {e}")
            return False

        if not self.connect():
            return False

        try:
            self.socket.settimeout(0.1)
            total_bytes = len(pcm_data)
            bytes_sent = 0
            chunk_size = 512
            start_time = time.time()

            while bytes_sent < total_bytes and not self.interrupt and not should_exit:
                end = min(bytes_sent + chunk_size, total_bytes)
                chunk = pcm_data[bytes_sent:end]
                self.socket.sendall(chunk)
                bytes_sent += len(chunk)

                elapsed = time.time() - start_time
                if elapsed > 0:
                    rate_kbps = (bytes_sent / 4096) / elapsed
                    logger.debug(f"已发送: {bytes_sent} / {total_bytes} 字节, 平均速率: {rate_kbps:.2f} KB/s")
                time.sleep(chunk_size / (TARGET_SAMPLE_RATE * TARGET_CHANNELS * TARGET_SAMPLE_WIDTH))

            time.sleep(0.05)
            self.socket.sendall(b"END_STREAM")
            logger.info(f"上传完成: {file_path}")

            try:
                os.remove(file_path)
                logger.info(f"已删除文件: {file_path}")
            except Exception as e:
                logger.warning(f"删除文件失败: {e}")

            return True
        except Exception as e:
            logger.error(f"上传失败: {e}")
            return False
        finally:
            self.close()

    def interrupt_upload(self):
        self.interrupt = True
        if self.socket:
            try:
                self.socket.sendall(b"INTERRUPT_")
            except:
                pass


@app.post("/api/upload/file")
async def upload_file(file_path: str, mac: str, ip: str):
    if not os.path.exists(file_path) or not file_path.endswith(".wav"):
        raise HTTPException(status_code=400, detail="无效的 WAV 文件路径")

    esp32_ip = ip.strip()
    if not esp32_ip:
        raise HTTPException(status_code=400, detail="缺少 IP 参数")

    with upload_lock:
        if mac in active_uploads:
            active_uploads[mac].interrupt_upload()

    uploader = AudioUploader(esp32_ip)
    with upload_lock:
        active_uploads[mac] = uploader

    def run():
        try:
            uploader.upload_wav_as_pcm(file_path)
        finally:
            with upload_lock:
                if mac in active_uploads:
                    del active_uploads[mac]

    threading.Thread(target=run, daemon=True).start()
    return {"status": "uploading", "file": file_path, "mac": mac, "ip": ip}


@app.post("/api/upload/batch")
async def upload_batch(request: BatchUploadRequest):
    responses = []
    for path in request.file_paths:
        try:
            resp = await upload_file(file_path=path)
            responses.append(resp)
        except HTTPException as e:
            responses.append({"status": "error", "detail": e.detail, "file": path})
    return {"results": responses}


@app.get("/health")
async def health():
    return {"status": "healthy", "active_uploads": len(active_uploads)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)

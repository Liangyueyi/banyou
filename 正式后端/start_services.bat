@echo off
chcp 65001 > nul
echo === 智能语音对话服务启动脚本 ===

:: 检查并关闭可能占用的端口
echo 正在检查端口占用情况...
FOR %%P IN (8000 8081 8082 8085 8086) DO (
    netstat -ano | findstr ":%%P" > NUL
    IF NOT ERRORLEVEL 1 (
        echo 发现端口%%P被占用，正在尝试关闭...
        FOR /F "tokens=5" %%T IN ('netstat -ano ^| findstr ":%%P"') DO (
            IF NOT "%%T" == "" (
                echo 正在终止进程 %%T
                taskkill /F /PID %%T
            )
        )
    ) ELSE (
        echo 端口%%P 可用
    )
)

:: 设置当前目录为脚本所在目录
cd /d "%~dp0"
echo 当前工作目录: %CD%

:: 启动ASR服务
echo.
echo 正在启动ASR服务...
start cmd /k "title ASR服务 && python asr_service.py"

:: 等待ASR服务初始化
echo 等待ASR服务初始化（20秒）...
timeout /t 20 /nobreak > NUL

:: 启动LLM服务
echo 正在启动LLM服务...
start cmd /k "title LLM服务 && python llm_service.py"

:: 等待LLM服务初始化
echo 等待LLM服务初始化（5秒）...
timeout /t 5 /nobreak > NUL

:: 启动TTS服务
echo 正在启动TTS服务...
start cmd /k "title TTS服务 && python tts_service.py"

:: 等待TTS服务初始化
echo 等待TTS服务初始化（5秒）...
timeout /t 5 /nobreak > NUL

:: 启动上传服务
echo 正在启动上传服务...
start cmd /k "title 上传服务 && python upload_service.py"

:: 启动输入服务
echo 正在启动输入服务...
start cmd /k "title 输入服务 && python input_service.py"

:: 启动主服务
echo 正在启动主服务（控制器）...
start cmd /k "title 主服务控制器 && python main.py"

:: 等待主服务初始化
echo 等待主服务初始化（5秒）...
timeout /t 5 /nobreak > NUL


echo.
echo === 所有服务启动完成 ===
echo 主服务: http://localhost:8000
echo ASR服务: http://localhost:8081
echo LLM服务: http://localhost:8082
echo TTS服务: http://localhost:8085
echo 上传服务: http://localhost:8086
echo 输入服务: http://localhost:8087

echo.
echo 服务健康检查：
echo 等待所有服务就绪（10秒）...
timeout /t 10 /nobreak > NUL
curl http://localhost:8086/health

echo.
echo 按任意键退出此窗口，服务将继续在后台运行...
pause > nul
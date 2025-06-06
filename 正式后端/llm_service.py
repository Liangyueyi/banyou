from flask import Flask, request, jsonify, Response
import requests
import socket
import json
import time
import logging
import re

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("llm_service")

app = Flask(__name__)

# ==== 配置 Dify API ====
DIFY_API_URL = "http://localhost/v1/chat-messages"
API_KEY = "app-H1wmEnlM3V2dYpoNHSgvvfl5"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# ==== 存储每个设备（mac）的会话 ID 与对话历史 ====
session_store = {}  # {mac: conversation_id}
conversation_history = {}  # {mac: [ {role, content} ]}

# ==== 主聊天函数 ====
def talk_to_dify(mac, query, reset=False):
    logger.info(f"向Dify发送查询 - MAC: {mac} - 查询内容: {query[:100]}...")
    start_time = time.time()

    if reset or mac not in session_store:
        session_store[mac] = None
        conversation_history[mac] = []

    # 添加当前提问
    conversation_history.setdefault(mac, [])
    conversation_history[mac].append({"role": "user", "content": query})

    conversation_id = session_store.get(mac)
    history = conversation_history[mac]

    payload = {
        "query": query,
        "inputs": {},
        "response_mode": "blocking",
        "user": mac,
        "conversation_id": conversation_id,
        "conversation_history": history,
        "auto_generate_name": True
    }

    response = requests.post(DIFY_API_URL, headers=HEADERS, json=payload)
    process_time = time.time() - start_time

    if response.status_code == 200:
        data = response.json()
        session_store[mac] = data.get("conversation_id")
        answer = data.get("answer", "")

        # 添加回答
        conversation_history[mac].append({"role": "assistant", "content": answer})

        logger.info(f"Dify响应成功 - MAC: {mac} - 处理时间: {process_time:.2f}秒")
        logger.info(f"LLM输出内容: {answer[:100]}..." if len(answer) > 100 else f"LLM输出内容: {answer}")
        return answer
    else:
        error_msg = f"❌ 请求失败: {response.status_code} - {response.text}"
        logger.error(f"Dify响应失败 - MAC: {mac} - {error_msg}")
        return error_msg

# ==== 接收问题 ====
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    mac = data.get("mac")
    query = data.get("query")
    reset = data.get("reset", False)

    logger.info(f"收到聊天请求 - MAC: {mac} - 重置: {reset}")

    if not mac or not query:
        return jsonify({"error": "需要提供 mac 和 query"}), 400

    answer = talk_to_dify(mac, query, reset=reset)

    logger.info(f"聊天请求处理完成 - MAC: {mac}")
    return jsonify({"mac": mac, "answer": answer})

# ==== 重置对话（中止） ====
@app.route("/stop", methods=["POST"])
def stop():
    data = request.get_json()
    mac = data.get("mac")

    logger.info(f"收到对话重置请求 - MAC: {mac}")

    if not mac:
        return jsonify({"error": "需要提供 mac"}), 400

    session_store.pop(mac, None)
    conversation_history.pop(mac, None)
    logger.info(f"对话已重置 - MAC: {mac}")
    return jsonify({"mac": mac, "status": "对话已重置"})

# ==== 单轮转多轮流式处理 ====
@app.route("/process", methods=["POST"])
def process_stream():
    data = request.json
    text = data.get("text")
    mac = data.get("mac")
    context = data.get("context", {})

    request_id = context.get("request_id", "unknown")
    logger.info(f"收到流式处理请求: request_id={request_id}, mac={mac}, text_length={len(text) if text else 0}")

    if not text or not mac:
        return jsonify({"error": "缺少必要参数", "status": "failed"}), 400

    def generate():
        try:
            answer = talk_to_dify(mac, text)
            import re
            sentences = re.split(r'([。！？!?.])', answer)
            combined = [sentences[i] + sentences[i+1] for i in range(0, len(sentences)-1, 2)]
            if not combined and answer.strip():
                combined = [answer]

            for idx, sentence in enumerate(combined):
                if sentence.strip():
                    logger.info(f"流式输出句子[{idx+1}/{len(combined)}]: {sentence.strip()}")
                    yield json.dumps({"content": sentence.strip()}) + "\n"
                    time.sleep(0.1)

            if not combined:
                yield json.dumps({"content": answer.strip() or "我没有回答。"}) + "\n"
            logger.info(f"流式响应完成: request_id={request_id}")

        except Exception as e:
            logger.exception(f"流式处理时出错: request_id={request_id}, error={str(e)}")
            yield json.dumps({"error": str(e), "status": "error"}) + "\n"

    return Response(generate(), mimetype='application/x-ndjson')

# ==== 健康检查接口 ====
@app.route("/health", methods=["GET"])
def health_check():
    logger.info("健康检查请求")
    return jsonify({
        "status": "healthy",
        "service": "大语言模型(LLM)服务",
        "version": "1.0.0"
    })

if __name__ == "__main__":
    logger.info("启动大语言模型(LLM)服务，端口8082...")
    logger.info("健康检查接口: http://localhost:8082/health")
    logger.info("处理接口: http://localhost:8082/process")
    logger.info("聊天接口: http://localhost:8082/chat")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', 8082))
    sock.close()

    app.run(host="0.0.0.0", port=8082, use_reloader=False)
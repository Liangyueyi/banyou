import asyncio
import requests
import time
import json
import threading
import webbrowser
from datetime import datetime
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse

# 云服务器配置 - 请修改为您的云服务器地址
CLOUD_SERVER_URL = "http://124.223.102.137:8000/api/ble/location"

# 本地位置信息存储
current_location = {"latitude": None, "longitude": None, "accuracy": None, "timestamp": None}
location_lock = threading.Lock()

# 本地HTTP服务器用于获取位置信息
class LocationHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            
            html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>BLE扫描器位置获取</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .status { padding: 15px; margin: 10px 0; border-radius: 5px; }
        .success { background-color: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
        .error { background-color: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
        .info { background-color: #d1ecf1; border: 1px solid #bee5eb; color: #0c5460; }
        .location-info { background-color: #fff3cd; border: 1px solid #ffeaa7; color: #856404; }
        button { background-color: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background-color: #0056b3; }
        button:disabled { background-color: #6c757d; cursor: not-allowed; }
        .auto-update { margin-top: 20px; padding: 15px; background-color: #e9ecef; border-radius: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔵 BLE扫描器位置获取</h1>
        <p>此页面用于为BLE扫描器提供位置信息。点击下方按钮获取您的位置，然后保持页面开启。</p>
        
        <button id="getLocationBtn" onclick="getLocation()">获取位置信息</button>
        
        <div id="status"></div>
        
        <div class="auto-update">
            <h3>自动更新设置</h3>
            <label>
                <input type="checkbox" id="autoUpdate" onchange="toggleAutoUpdate()"> 
                每30秒自动更新位置
            </label>
            <p><small>建议开启自动更新以保持位置信息的准确性</small></p>
        </div>
    </div>

    <script>
        let autoUpdateInterval = null;
        
        function getLocation() {
            const statusDiv = document.getElementById('status');
            const btn = document.getElementById('getLocationBtn');
            
            statusDiv.innerHTML = '<div class="info">正在获取位置信息...</div>';
            btn.disabled = true;
            
            if (!navigator.geolocation) {
                statusDiv.innerHTML = '<div class="error">浏览器不支持地理定位功能</div>';
                btn.disabled = false;
                return;
            }
            
            navigator.geolocation.getCurrentPosition(
                function(position) {
                    const data = {
                        latitude: position.coords.latitude,
                        longitude: position.coords.longitude,
                        accuracy: position.coords.accuracy,
                        timestamp: new Date().toISOString()
                    };
                    
                    // 发送位置信息到本地服务器
                    fetch('/update_location', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify(data)
                    })
                    .then(response => response.json())
                    .then(result => {
                        statusDiv.innerHTML = `
                            <div class="success">
                                <h3>✅ 位置获取成功！</h3>
                                <p><strong>纬度:</strong> ${data.latitude.toFixed(6)}</p>
                                <p><strong>经度:</strong> ${data.longitude.toFixed(6)}</p>
                                <p><strong>精度:</strong> ${data.accuracy ? data.accuracy.toFixed(2) + 'm' : '未知'}</p>
                                <p><strong>更新时间:</strong> ${new Date().toLocaleString()}</p>
                            </div>
                            <div class="location-info">
                                📍 位置信息已更新，BLE扫描器现在可以使用此位置信息
                            </div>
                        `;
                        btn.disabled = false;
                    })
                    .catch(error => {
                        statusDiv.innerHTML = `<div class="error">更新位置失败: ${error.message}</div>`;
                        btn.disabled = false;
                    });
                },
                function(error) {
                    let errorMsg = '';
                    switch(error.code) {
                        case error.PERMISSION_DENIED:
                            errorMsg = '用户拒绝了位置获取请求';
                            break;
                        case error.POSITION_UNAVAILABLE:
                            errorMsg = '位置信息不可用';
                            break;
                        case error.TIMEOUT:
                            errorMsg = '获取位置超时';
                            break;
                        default:
                            errorMsg = '未知错误';
                    }
                    statusDiv.innerHTML = `<div class="error">位置获取失败: ${errorMsg}</div>`;
                    btn.disabled = false;
                },
                {
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 0
                }
            );
        }
        
        function toggleAutoUpdate() {
            const checkbox = document.getElementById('autoUpdate');
            
            if (checkbox.checked) {
                // 启动自动更新
                autoUpdateInterval = setInterval(getLocation, 30000);
                console.log('自动更新已启动（每30秒）');
            } else {
                // 停止自动更新
                if (autoUpdateInterval) {
                    clearInterval(autoUpdateInterval);
                    autoUpdateInterval = null;
                }
                console.log('自动更新已停止');
            }
        }
        
        // 页面加载时自动获取一次位置
        window.onload = function() {
            getLocation();
        };
    </script>
</body>
</html>
            """
            self.wfile.write(html_content.encode('utf-8'))
        else:
            super().do_GET()
    
    def do_POST(self):
        if self.path == '/update_location':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                location_data = json.loads(post_data.decode('utf-8'))
                
                # 更新全局位置信息
                with location_lock:
                    global current_location
                    current_location.update(location_data)
                
                print(f"📍 位置信息已更新: 纬度={location_data['latitude']:.6f}, 经度={location_data['longitude']:.6f}, 精度={location_data.get('accuracy', 'N/A')}")
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "message": "位置更新成功"}).encode('utf-8'))
            
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def start_location_server():
    """启动本地位置服务器"""
    server = HTTPServer(('localhost', 8080), LocationHandler)
    print("🌐 位置服务器已启动: http://localhost:8080")
    print("请在浏览器中打开此链接获取位置信息")
    server.serve_forever()

# iBeacon数据解析
def parse_ibeacon(manufacturer_data):
    if not manufacturer_data or 0x004C not in manufacturer_data:
        return None
        
    data = manufacturer_data[0x004C]
    if len(data) < 23 or data[0] != 0x02 or data[1] != 0x15:
        return None
        
    uuid = data[2:18].hex()
    major = int.from_bytes(data[18:20], byteorder='big')
    minor = int.from_bytes(data[20:22], byteorder='big')
    tx_power = int.from_bytes(data[22:23], byteorder='big', signed=True)
    
    return {
        'uuid': f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:32]}",
        'major': major,
        'minor': minor,
        'tx_power': tx_power
    }

def send_to_cloud_server(ble_address, ibeacon_data=None):
    """发送BLE数据和位置信息到云服务器"""
    with location_lock:
        if current_location['latitude'] is None or current_location['longitude'] is None:
            print(f"⚠️ 位置信息未获取，跳过发送: {ble_address}")
            return False
        
        # 准备发送到云服务器的数据
        payload = {
            "ble_address": ble_address,
            "latitude": current_location['latitude'],
            "longitude": current_location['longitude'],
            "accuracy": current_location.get('accuracy'),
            "timestamp": datetime.now().isoformat(),
            "client_location_timestamp": current_location.get('timestamp')
        }
        
        # 添加iBeacon信息
        if ibeacon_data:
            payload.update({
                "uuid": ibeacon_data['uuid'],
                "major": ibeacon_data['major'],
                "minor": ibeacon_data['minor'],
                "tx_power": ibeacon_data['tx_power']
            })
    
    try:
        response = requests.post(CLOUD_SERVER_URL, json=payload, timeout=5)
        if response.status_code == 200:
            print(f"✅ 已发送到云服务器: {ble_address}")
            return True
        else:
            print(f"❌ 云服务器响应错误 {response.status_code}: {ble_address}")
            return False
    except Exception as e:
        print(f"❌ 发送到云服务器失败: {ble_address} - {str(e)}")
        return False

# 发现设备回调
device_last_sent = {}

async def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
    current_time = time.time()
    
    # 检查是否为iBeacon
    ibeacon = parse_ibeacon(advertisement_data.manufacturer_data)
    
    # 限制发送频率（每个设备每10秒最多一次）
    if device.address not in device_last_sent or current_time - device_last_sent[device.address] >= 10.0:
        
        if ibeacon:
            print(f"🔵 发现iBeacon: {device.address}")
            print(f"   UUID: {ibeacon['uuid']}")
            print(f"   Major: {ibeacon['major']}, Minor: {ibeacon['minor']}")
            print(f"   Tx Power: {ibeacon['tx_power']} dBm")
        else:
            print(f"🔵 发现BLE设备: {device.address}")
        
        # 发送到云服务器
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None,
            lambda: send_to_cloud_server(device.address, ibeacon)
        )
        
        if success:
            device_last_sent[device.address] = current_time
        
        print("-" * 60)

async def main():
    print("=" * 80)
    print("🔵 BLE扫描器启动中...")
    print(f"云服务器地址: {CLOUD_SERVER_URL}")
    print("=" * 80)
    
    # 在后台线程中启动位置服务器
    location_server_thread = threading.Thread(target=start_location_server, daemon=True)
    location_server_thread.start()
    
    # 等待服务器启动
    await asyncio.sleep(2)
    
    # 自动打开浏览器获取位置
    try:
        webbrowser.open('http://localhost:8080')
        print("📱 已自动打开浏览器，请在页面中获取位置信息")
    except Exception as e:
        print(f"⚠️ 无法自动打开浏览器: {e}")
        print("请手动在浏览器中打开: http://localhost:8080")
    
    print("\n⏳ 请在浏览器中获取位置信息后，BLE扫描将开始发送数据到云服务器...")
    print("💡 建议在浏览器页面中开启'自动更新位置'选项\n")
    
    # 使用更积极的扫描参数
    scanner = BleakScanner(
        detection_callback,
        scanning_mode="active"  # 主动扫描模式
    )
    
    print("🔍 开始持续扫描BLE设备(按Ctrl+C停止)...")
    start_time = time.time()
    
    await scanner.start()
    print(f"✅ BLE扫描器已启动，耗时: {time.time()-start_time:.2f}秒")
    print("📡 等待发现BLE设备...")
    
    try:
        while True:
            await asyncio.sleep(1)  # 检查状态
            
            # 每30秒检查一次位置状态
            if int(time.time()) % 30 == 0:
                with location_lock:
                    if current_location['latitude'] is not None:
                        print(f"📍 当前位置: {current_location['latitude']:.6f}, {current_location['longitude']:.6f}")
                    else:
                        print("⚠️ 位置信息未获取，请在浏览器中获取位置")
                
    except KeyboardInterrupt:
        print("\n🛑 收到中断信号，正在停止...")
        await scanner.stop()
        print("✅ BLE扫描已停止")
        print("🔵 程序已退出")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 程序被用户中断")
    except Exception as e:
        print(f"\n❌ 程序出错: {e}")
        import traceback
        traceback.print_exc()

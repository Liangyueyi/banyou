import asyncio
import requests
import time
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

FLASK_URL = "http://localhost:5050/api/data"

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

# 发现设备回调
device_last_sent = {}

async def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
    current_time = time.time()
    
    ibeacon = parse_ibeacon(advertisement_data.manufacturer_data)
    if ibeacon:
        # 打印设备信息(每秒最多一次)
        if device.address not in device_last_sent or current_time - device_last_sent[device.address] >= 1.0:
            print(f"发现iBeacon: {device.address}")
            print(f"UUID: {ibeacon['uuid']}")
            print(f"Major: {ibeacon['major']}, Minor: {ibeacon['minor']}")
            print(f"Tx Power: {ibeacon['tx_power']} dBm")
            print("-" * 40)
        
        # 发送数据到Flask服务(每个设备每秒最多一次)
        if device.address not in device_last_sent or current_time - device_last_sent[device.address] >= 1.0:
            try:
                payload = {
                    'address': device.address,
                    'uuid': ibeacon['uuid'],
                    'major': ibeacon['major'],
                    'minor': ibeacon['minor'],
                    'tx_power': ibeacon['tx_power']
                }
                # 使用线程池异步发送数据，避免阻塞扫描
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: requests.post(FLASK_URL, json=payload, timeout=1)
                )
                device_last_sent[device.address] = current_time
                print(f"已发送数据: {device.address}")
            except Exception as e:
                print(f"发送数据到服务器失败: {str(e)[:100]}...")  # 截断错误信息

# 记录每个设备的上次发送时间
device_last_sent = {}

async def main():
    # 使用更积极的扫描参数
    scanner = BleakScanner(
        detection_callback,
        scanning_mode="active"  # 主动扫描模式
    )
    
    print("开始持续扫描iBeacon(按Ctrl+C停止)...")
    start_time = time.time()
    
    await scanner.start()
    print(f"扫描器已启动，耗时: {time.time()-start_time:.2f}秒")
    
    try:
        while True:
            await asyncio.sleep(0.1)  # 更短的等待时间
    except KeyboardInterrupt:
        await scanner.stop()
        print("扫描已停止")

if __name__ == "__main__":
    asyncio.run(main())

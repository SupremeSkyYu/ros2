"""
ROS+CARLA 恶劣雨雾天气下相机感知性能对比实验

依赖: carla==0.9.15, numpy, opencv-python, matplotlib, Pillow
使用: 1) 先启动 CARLA 服务端（双击解压目录下的 CarlaUE4.exe）
      2) 在 Python 3.10 环境中运行 python main.py
说明: 脚本以地面载具（ego 车辆）为感知平台，车顶相机采集前方道路图像。
      为保证四档天气下的画面严格可比，测量阶段车辆固定在同一参考位姿，
      每档天气切换后拍摄同一场景；测量结束后切换为自动驾驶，演示载具行驶。
      所有结果写入本脚本同级的 results/ 目录，与运行时的工作目录无关。
"""
import os
import time
import csv
import threading

import carla
import numpy as np
import cv2
import matplotlib.pyplot as plt
from PIL import Image

# 结果目录固定在脚本同级，避免从其他目录运行时把结果写到别处
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')

# ---------- 连接 CARLA ----------
client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()

# ---------- 生成车辆与相机 ----------
bp_lib = world.get_blueprint_library()
vehicle_bp = bp_lib.filter('vehicle.tesla.model3')[0]
spawn_point = world.get_map().get_spawn_points()[0]
vehicle = world.spawn_actor(vehicle_bp, spawn_point)

# 测量阶段车辆固定在参考位姿（保持刹停），保证四档天气拍摄的是同一场景
traffic_manager = client.get_trafficmanager(8000)
traffic_manager.set_global_distance_to_leading_vehicle(2.5)
vehicle.set_autopilot(False)
vehicle.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0))
print(f'车辆已生成，位置={spawn_point.location}')

camera_bp = bp_lib.find('sensor.camera.rgb')
camera_bp.set_attribute('image_size_x', '800')
camera_bp.set_attribute('image_size_y', '600')
camera_bp.set_attribute('fov', '90')
camera_transform = carla.Transform(carla.Location(x=1.5, z=2.4))
camera = world.spawn_actor(camera_bp, camera_transform, attach_to=vehicle)

image_store = {'img': None}


def on_image(image):
    arr = np.frombuffer(image.raw_data, dtype=np.uint8)
    arr = np.reshape(arr, (image.height, image.width, 4))  # BGRA
    image_store['img'] = arr[:, :, :3]                      # BGR


camera.listen(on_image)

# ---------- 后台录制演示动图（全程采帧，最后合成 gif）----------
recorded = []
recording = {'on': True}


def recorder():
    while recording['on']:
        img = image_store['img']
        if img is not None:
            recorded.append(img.copy())
        time.sleep(0.5)


threading.Thread(target=recorder, daemon=True).start()

# ---------- 四档天气 ----------
# 注意: carla.WeatherParameters.ClearNoon 等是共享的类属性对象，直接修改会污染该常量，
# 必须用构造函数生成独立对象，否则「晴朗」和「浓雾」会变成同一组参数。
base = carla.WeatherParameters.ClearNoon
clear = carla.WeatherParameters(
    cloudiness=base.cloudiness,
    precipitation=0.0,
    precipitation_deposits=0.0,
    wind_intensity=base.wind_intensity,
    sun_azimuth_angle=base.sun_azimuth_angle,
    sun_altitude_angle=base.sun_altitude_angle,
    fog_density=0.0,
    fog_distance=0.0,
    fog_falloff=0.0,
    wetness=0.0,
)
fog = carla.WeatherParameters(
    cloudiness=base.cloudiness,
    precipitation=0.0,
    precipitation_deposits=0.0,
    wind_intensity=base.wind_intensity,
    sun_azimuth_angle=base.sun_azimuth_angle,
    sun_altitude_angle=base.sun_altitude_angle,
    fog_density=80.0,
    fog_distance=0.75,
    fog_falloff=0.1,
    wetness=0.0,
)
weather_presets = [
    ('ClearNoon', clear),
    ('SoftRainNoon', carla.WeatherParameters.SoftRainNoon),
    ('HardRainNoon', carla.WeatherParameters.HardRainNoon),
    ('HeavyFog', fog),
]

os.makedirs(RESULTS_DIR, exist_ok=True)
results = []

for name, wp in weather_presets:
    # 每次测量前把车辆复位到同一参考位姿，确保四档天气看到的是同一场景
    vehicle.set_transform(spawn_point)
    vehicle.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0))
    world.set_weather(wp)
    time.sleep(3)  # 等待天气生效、相机刷新

    # 同一场景下连续采集 5 帧取平均，抑制雨滴闪烁带来的单帧波动
    frames = []
    for _ in range(5):
        img = image_store['img']
        if img is not None:
            frames.append(img)
        time.sleep(0.3)
    if not frames:
        print(f'{name}: 未收到图像，跳过')
        continue

    img = frames[-1]
    brightness = float(np.mean([np.mean(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)) for f in frames]))
    contrast = float(np.mean([np.std(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)) for f in frames]))
    edge_density = float(np.mean([
        np.sum(cv2.Canny(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), 50, 150) > 0)
        / (f.shape[0] * f.shape[1]) * 100 for f in frames
    ]))

    results.append({
        'weather': name,
        'brightness': round(brightness, 2),
        'contrast': round(contrast, 2),
        'edge_density': round(edge_density, 2),
    })
    cv2.imwrite(os.path.join(RESULTS_DIR, f'{name}.png'), img)
    print(f'{name}: 亮度={brightness:.1f} 对比度={contrast:.1f} 边缘密度={edge_density:.2f}%')

# ---------- 保存结果 ----------
with open(os.path.join(RESULTS_DIR, 'metrics.csv'), 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['weather', 'brightness', 'contrast', 'edge_density'])
    writer.writeheader()
    writer.writerows(results)

# ---------- 切换为自动驾驶：演示地面载具在恶劣天气下行驶 ----------
print('测量完成，车辆切换为自动驾驶（当前天气为浓雾），正在录制行驶演示动图')
vehicle.set_autopilot(True, traffic_manager.get_port())
time.sleep(10)

# ---------- 合成演示动图 ----------
recording['on'] = False
time.sleep(0.2)

if recorded:
    if len(recorded) > 60:  # 限制帧数，控制 gif 体积（<10MB）
        recorded = recorded[::len(recorded) // 60 + 1]
    frames = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)).resize((400, 300))
              for f in recorded]
    frames[0].save(os.path.join(RESULTS_DIR, 'demo.gif'), save_all=True,
                   append_images=frames[1:], duration=500, loop=0, optimize=True)
    print(f'演示动图已生成: results/demo.gif（{len(frames)} 帧）')

names = [r['weather'] for r in results]
fig, axes = plt.subplots(1, 3, figsize=(15, 4))
axes[0].bar(names, [r['brightness'] for r in results], color='skyblue')
axes[0].set_title('Brightness (mean)')
axes[1].bar(names, [r['contrast'] for r in results], color='orange')
axes[1].set_title('Contrast (std)')
axes[2].bar(names, [r['edge_density'] for r in results], color='green')
axes[2].set_title('Edge Density (%)')
plt.tight_layout()
plt.savefig(os.path.join(RESULTS_DIR, 'metrics.png'), dpi=100)
plt.show()

# ---------- 清理 ----------
camera.stop()
camera.destroy()
vehicle.destroy()
print('实验完成，结果保存在 results/ 目录')

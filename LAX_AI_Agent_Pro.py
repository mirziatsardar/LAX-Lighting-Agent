import tkinter as tk
from tkinter import ttk, messagebox
import socket
import time
import math
import random
import threading
import numpy as np
import pyaudio
import aubio

# ==========================================
# 核心底层变量
# ==========================================
ARTNET_HEADER = b'Art-Net\x00\x00\x50\x00\x0e'
is_running = False
beat_hit_flag = False
audio_energy = 0.0

# 预设灯具库 (真实软件中可以存在本地 JSON 文件里)
FIXTURE_LIB = {
    "LAX_Moving_Head (10CH)": {"Pan": 0, "Tilt": 1, "Dimmer": 4, "Strobe": 5, "Red": 6, "Green": 7, "Blue": 8, "Gobo": 9},
    "LAX_Wash (7CH)": {"Pan": 0, "Tilt": 1, "Dimmer": 4, "Strobe": 5, "Red": 6, "Green": 7, "Blue": 8},
    "Static_Par (5CH)": {"Dimmer": 0, "Strobe": 1, "Red": 2, "Green": 3, "Blue": 4}
}

active_fixtures = [] # 用户在界面上添加的灯具列表

# ==========================================
# 线程 1：AI 听觉引擎 (Aubio 实时节拍检测)
# ==========================================
def audio_listener_thread():
    global is_running, beat_hit_flag, audio_energy
    
    # 麦克风音频参数
    BUFFER_SIZE = 1024
    SAMPLE_RATE = 44100
    
    p = pyaudio.PyAudio()
    try:
        stream = p.open(format=pyaudio.paFloat32, channels=1, rate=SAMPLE_RATE, input=True, frames_per_buffer=BUFFER_SIZE)
    except Exception as e:
        print(f"音频输入设备打开失败: {e}")
        return

    # Aubio 节拍检测器
    tempo_detector = aubio.tempo("default", BUFFER_SIZE * 2, BUFFER_SIZE, SAMPLE_RATE)

    while is_running:
        try:
            audio_data = stream.read(BUFFER_SIZE, exception_on_overflow=False)
            samples = np.frombuffer(audio_data, dtype=np.float32)
            
            # 1. 计算能量 (音量大小)，用于控制亮度或运动幅度
            audio_energy = np.sum(samples**2) / len(samples)
            
            # 2. 检测节拍 (Drop 点 / 鼓点)
            is_beat = tempo_detector(samples)
            if is_beat[0]:
                beat_hit_flag = True  # 触发节拍标志
                
        except Exception as e:
            pass

    stream.stop_stream()
    stream.close()
    p.terminate()

# ==========================================
# 线程 2：AI 视觉与发送引擎 (Art-Net)
# ==========================================
def dmx_engine_thread(target_ip, universe):
    global is_running, beat_hit_flag, audio_energy, active_fixtures
    
    start_time = time.time()
    color_idx = 0
    colors = [(255,0,0), (0,0,255), (0,255,255), (255,0,255), (255,255,0)]
    
    while is_running:
        elapsed = time.time() - start_time
        dmx_universe = bytearray(512)
        
        # --- 情绪与节拍响应 ---
        current_r, current_g, current_b = colors[color_idx]
        
        if beat_hit_flag:
            color_idx = (color_idx + 1) % len(colors) # 踩准鼓点切颜色
            beat_hit_flag = False
            strobe_val = 200 # 爆闪
            gobo_val = random.choice([0, 30, 60, 90])
        else:
            strobe_val = 0
            gobo_val = 15
        
        # 音量越大，摇摆幅度越剧烈 (结合正弦波)
        dynamic_amp = min(1.0, audio_energy * 100)
        pan_val = int(127 + (60 * dynamic_amp + 20) * math.sin(elapsed * 2.0))
        tilt_val = int(127 + (40 * dynamic_amp + 10) * math.cos(elapsed * 1.5))

        # --- 数据打包 ---
        for fix in active_fixtures:
            base = fix["addr"] - 1
            ch = fix["channels"]
            
            if "Dimmer" in ch: dmx_universe[base + ch["Dimmer"]] = 255
            if "Red" in ch: dmx_universe[base + ch["Red"]] = current_r
            if "Green" in ch: dmx_universe[base + ch["Green"]] = current_g
            if "Blue" in ch: dmx_universe[base + ch["Blue"]] = current_b
            if "Strobe" in ch: dmx_universe[base + ch["Strobe"]] = strobe_val
            
            if "Pan" in ch: dmx_universe[base + ch["Pan"]] = pan_val
            if "Tilt" in ch: dmx_universe[base + ch["Tilt"]] = tilt_val
            if "Gobo" in ch and gobo_val is not None: dmx_universe[base + ch["Gobo"]] = gobo_val

        # --- 发送 Art-Net ---
        payload = bytes(dmx_universe) + b'\x00' * (512 - len(dmx_universe))
        packet = ARTNET_HEADER + b'\x00\x00' + int(universe).to_bytes(2, 'little') + len(payload).to_bytes(2, 'big') + payload[:512]
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(packet, (target_ip, 6454))
        except:
            pass
            
        time.sleep(0.03) # 约 30fps

# ==========================================
# 线程 0：软件 GUI 界面
# ==========================================
class LightAgentApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LAX 智能声光引擎 - 商业版 V1.0")
        self.root.geometry("500x550")
        
        # --- 网络设置区 ---
        frame_net = ttk.LabelFrame(root, text="第一步：选择控台与网络", padding=10)
        frame_net.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(frame_net, text="目标 IP (MA2/老虎台):").grid(row=0, column=0, sticky="w")
        self.ip_entry = ttk.Entry(frame_net, width=15)
        self.ip_entry.insert(0, "127.0.0.1")
        self.ip_entry.grid(row=0, column=1, padx=5)
        
        ttk.Label(frame_net, text="DMX 宇宙 (Universe):").grid(row=1, column=0, sticky="w", pady=5)
        self.universe_entry = ttk.Entry(frame_net, width=15)
        self.universe_entry.insert(0, "0")
        self.universe_entry.grid(row=1, column=1, padx=5)

        # --- 灯具配接区 ---
        frame_patch = ttk.LabelFrame(root, text="第二步：添加灯具 (Patch)", padding=10)
        frame_patch.pack(fill="both", expand=True, padx=10, pady=5)
        
        ttk.Label(frame_patch, text="灯具类型:").grid(row=0, column=0)
        self.fix_type_cb = ttk.Combobox(frame_patch, values=list(FIXTURE_LIB.keys()), state="readonly", width=20)
        self.fix_type_cb.current(0)
        self.fix_type_cb.grid(row=0, column=1, padx=5)
        
        ttk.Label(frame_patch, text="起始地址 (1-512):").grid(row=1, column=0, pady=5)
        self.addr_entry = ttk.Entry(frame_patch, width=10)
        self.addr_entry.insert(0, "1")
        self.addr_entry.grid(row=1, column=1, pady=5)
        
        ttk.Button(frame_patch, text="➕ 添加到阵列", command=self.add_fixture).grid(row=0, column=2, rowspan=2, padx=10)
        
        # 灯具列表显示
        self.listbox = tk.Listbox(frame_patch, height=8)
        self.listbox.grid(row=2, column=0, columnspan=3, sticky="we", pady=10)

        # --- 控制区 ---
        frame_ctrl = ttk.Frame(root, padding=10)
        frame_ctrl.pack(fill="x", padx=10, pady=10)
        
        self.btn_start = ttk.Button(frame_ctrl, text="🚀 启动 AI 引擎 (开始拾音与输出)", command=self.toggle_engine, width=40)
        self.btn_start.pack()
        
        self.status_var = tk.StringVar()
        self.status_var.set("状态：就绪。等待添加灯具。")
        ttk.Label(root, textvariable=self.status_var, foreground="blue").pack(pady=5)

    def add_fixture(self):
        f_type = self.fix_type_cb.get()
        try:
            addr = int(self.addr_entry.get())
            if addr < 1 or addr > 512: raise ValueError
        except ValueError:
            messagebox.showerror("错误", "地址必须是 1-512 之间的整数！")
            return
            
        active_fixtures.append({"name": f_type, "addr": addr, "channels": FIXTURE_LIB[f_type]})
        self.listbox.insert(tk.END, f"[地址: {addr:03d}] {f_type}")
        self.addr_entry.delete(0, tk.END)
        self.addr_entry.insert(0, str(addr + len(FIXTURE_LIB[f_type]))) # 自动往后推算地址

    def toggle_engine(self):
        global is_running
        if not is_running:
            if not active_fixtures:
                messagebox.showwarning("警告", "请先在上方添加至少一台灯具！")
                return
                
            is_running = True
            self.btn_start.config(text="🛑 停止 AI 引擎")
            self.status_var.set("状态：正在运行！音频监听中... Art-Net 发送中...")
            
            # 启动音频和DMX后台线程
            threading.Thread(target=audio_listener_thread, daemon=True).start()
            threading.Thread(target=dmx_engine_thread, args=(self.ip_entry.get(), self.universe_entry.get()), daemon=True).start()
        else:
            is_running = False
            self.btn_start.config(text="🚀 启动 AI 引擎 (开始拾音与输出)")
            self.status_var.set("状态：已停止。")

if __name__ == "__main__":
    root = tk.Tk()
    app = LightAgentApp(root)
    root.mainloop()

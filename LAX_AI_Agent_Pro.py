import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import socket
import time
import math
import random
import threading
import json
import os
import librosa
try:
    import numpy as np
    import pyaudio
    import aubio
    import sacn
except ImportError:
    pass # 忽略导入错误，方便在没有环境的电脑上查看代码结构

# ==========================================
# 核心底层变量
# ==========================================
ARTNET_HEADER = b'Art-Net\x00\x00\x50\x00\x0e'
is_running = False
beat_hit_flag = False
audio_energy = 0.0

# --- 商业级：基于类型的通用灯库 (中文) ---
# 每一类灯，定义了它们普遍拥有的基础通道顺序 (默认最常见的顺序，可供客户微调)
FIXTURE_TYPES = {
    "电脑摇头灯 (带图案)": {
        "type": "spot",
        "default_channels": {"水平(Pan)": 1, "垂直(Tilt)": 2, "调光(Dimmer)": 6, "频闪(Strobe)": 7, "红色(Red)": 8, "绿色(Green)": 9, "蓝色(Blue)": 10, "图案(Gobo)": 11},
        "channel_count": 16
    },
    "摇头染色灯 (洗墙)": {
        "type": "wash",
        "default_channels": {"水平(Pan)": 1, "垂直(Tilt)": 2, "调光(Dimmer)": 5, "频闪(Strobe)": 6, "红色(Red)": 7, "绿色(Green)": 8, "蓝色(Blue)": 9, "白色(White)": 10},
        "channel_count": 14
    },
    "LED 帕灯 (固定染色)": {
        "type": "par",
        "default_channels": {"调光(Dimmer)": 1, "频闪(Strobe)": 2, "红色(Red)": 3, "绿色(Green)": 4, "蓝色(Blue)": 5, "白色(White)": 6},
        "channel_count": 8
    },
    "单色频闪爆闪灯": {
        "type": "strobe",
        "default_channels": {"调光(Dimmer)": 1, "频闪速度(Strobe)": 2},
        "channel_count": 4
    }
}

active_fixtures = [] # 当前场景中已添加的灯具列表

# ==========================================
# 线程 1：AI 听觉引擎 (librosa 实时版 - 稳定打包)
# ==========================================
def audio_listener_thread():
    global is_running, beat_hit_flag, audio_energy
    try:
        import librosa
        BUFFER_SIZE = 1024
        SAMPLE_RATE = 44100
        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paFloat32, channels=1, rate=SAMPLE_RATE,
                        input=True, frames_per_buffer=BUFFER_SIZE)

        while is_running:
            audio_data = stream.read(BUFFER_SIZE, exception_on_overflow=False)
            samples = np.frombuffer(audio_data, dtype=np.float32)
            
            # 实时能量
            audio_energy = np.sum(samples**2) / len(samples)
            
            # 鼓点检测（librosa onset）
            onset_env = librosa.onset.onset_strength(y=samples, sr=SAMPLE_RATE)
            if len(onset_env) > 0 and onset_env[0] > 1.5:   # 阈值可调
                beat_hit_flag = True

    except Exception as e:
        print(f"实时麦克风出错: {e}，进入模拟模式")
        while is_running:
            audio_energy = 0.5 + 0.3 * math.sin(time.time() * 2)
            if int(time.time() * 2) % 2 == 0:
                beat_hit_flag = True
            time.sleep(0.5)

# ==========================================
# 线程 2：AI 视觉与发送引擎 (基于分类的智能分配)
# ==========================================
def dmx_engine_thread(target_ip, universe, protocol):
    global is_running, beat_hit_flag, audio_energy, active_fixtures
    
    start_time = time.time()
    color_idx = 0
    colors = [(255,0,0), (0,0,255), (0,255,255), (255,0,255), (255,255,0)]
    
    sender = None
    if protocol == "sACN":
        try:
            sender = sacn.sACNsender(bind_address="0.0.0.0", bind_port=5568)
            sender.start()
            sender.activate_output(int(universe))
            sender[int(universe)].name = "LAX AI Agent"
            sender[int(universe)].priority = 101 # 强制接管优先级
            if target_ip.lower() == "multicast" or target_ip == "":
                sender[int(universe)].multicast = True
            else:
                sender[int(universe)].multicast = False
                sender[int(universe)].destination = target_ip
        except:
            print("sACN 初始化失败，请检查网络或库。")

    while is_running:
        elapsed = time.time() - start_time
        dmx_universe = bytearray(512)
        
        # --- 全局情绪与节拍响应 ---
        current_r, current_g, current_b = colors[color_idx]
        
        if beat_hit_flag:
            color_idx = (color_idx + 1) % len(colors) # 踩准鼓点切全局颜色
            beat_hit_flag = False
            global_strobe = 200 # 爆闪速度 (取决于具体灯，通常 > 128 是快闪)
            global_gobo = random.choice([0, 20, 40, 60, 80]) # 随机切图案
        else:
            global_strobe = 0 # 不闪
            global_gobo = 10  # 默认图案
        
        # 运动幅度控制 (音量越大，摇摆越剧烈)
        dynamic_amp = min(1.0, audio_energy * 50)
        global_pan = int(127 + (80 * dynamic_amp + 30) * math.sin(elapsed * 1.5))
        global_tilt = int(127 + (50 * dynamic_amp + 20) * math.cos(elapsed * 1.2))

        # --- 核心：基于灯具类型的智能数据分配 ---
        for fix in active_fixtures:
            base = fix["addr"] - 1 # 数组从0开始
            ch_map = fix["channels"]
            f_type = fix["type"] # 获取这台灯是摇头、帕灯还是频闪
            
            # 1. 基础光输出 (所有能亮的灯都有)
            if "调光(Dimmer)" in ch_map: dmx_universe[base + ch_map["调光(Dimmer)"] - 1] = 255
            
            # 2. 颜色分配 (除了单色频闪灯，其他都有)
            if f_type in ["spot", "wash", "par"]:
                if "红色(Red)" in ch_map: dmx_universe[base + ch_map["红色(Red)"] - 1] = current_r
                if "绿色(Green)" in ch_map: dmx_universe[base + ch_map["绿色(Green)"] - 1] = current_g
                if "蓝色(Blue)" in ch_map: dmx_universe[base + ch_map["蓝色(Blue)"] - 1] = current_b
                if "白色(White)" in ch_map: dmx_universe[base + ch_map["白色(White)"] - 1] = 0 # 暂不混白光
                
                # 帕灯的特殊频闪逻辑：鼓点时才闪
                if f_type == "par" and "频闪(Strobe)" in ch_map:
                    dmx_universe[base + ch_map["频闪(Strobe)"] - 1] = global_strobe

            # 3. 运动分配 (只有电脑摇头灯和染色灯才接收 Pan/Tilt)
            # 绝对不会把摇头信号发给固定帕灯
            if f_type in ["spot", "wash"]:
                if "水平(Pan)" in ch_map: dmx_universe[base + ch_map["水平(Pan)"] - 1] = global_pan
                if "垂直(Tilt)" in ch_map: dmx_universe[base + ch_map["垂直(Tilt)"] - 1] = global_tilt
                if "频闪(Strobe)" in ch_map: dmx_universe[base + ch_map["频闪(Strobe)"] - 1] = global_strobe

            # 4. 图案分配 (只有带图案盘的 Spot 摇头灯才有)
            if f_type == "spot":
                if "图案(Gobo)" in ch_map: dmx_universe[base + ch_map["图案(Gobo)"] - 1] = global_gobo

            # 5. 单色爆闪灯的特殊逻辑
            if f_type == "strobe":
                if "频闪速度(Strobe)" in ch_map: dmx_universe[base + ch_map["频闪速度(Strobe)"] - 1] = global_strobe
                if "调光(Dimmer)" in ch_map: dmx_universe[base + ch_map["调光(Dimmer)"] - 1] = 255 if global_strobe > 0 else 0

        # --- 协议分发发送 ---
        if protocol == "Art-Net":
            payload = bytes(dmx_universe) + b'\x00' * (512 - len(dmx_universe))
            packet = ARTNET_HEADER + b'\x00\x00' + int(universe).to_bytes(2, 'little') + len(payload).to_bytes(2, 'big') + payload[:512]
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(packet, (target_ip, 6454))
            except: pass
        elif protocol == "sACN" and sender:
            sender[int(universe)].dmx_data = tuple(dmx_universe)

        time.sleep(0.03) # 约 30fps

    if protocol == "sACN" and sender:
        sender.stop()

# ==========================================
# 线程 0：软件 GUI 界面 (全中文，商业化)
# ==========================================
class LightAgentApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LAX 智能声光引擎 - 商业统配版 V3.0")
        self.root.geometry("600x650")
        
        # --- 顶部：工程管理区 ---
        frame_file = ttk.Frame(root)
        frame_file.pack(fill="x", padx=10, pady=5)
        ttk.Button(frame_file, text="📂 读取现场工程", command=self.load_show).pack(side="left", padx=5)
        ttk.Button(frame_file, text="💾 保存当前配置", command=self.save_show).pack(side="left", padx=5)
        ttk.Button(frame_file, text="🗑️ 清空所有灯具", command=self.clear_show).pack(side="right", padx=5)

        # --- 网络设置区 ---
        frame_net = ttk.LabelFrame(root, text="第一步：选择控制协议与网络", padding=10)
        frame_net.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(frame_net, text="通讯协议:").grid(row=0, column=0, sticky="w")
        self.protocol_cb = ttk.Combobox(frame_net, values=["sACN (推荐)", "Art-Net"], state="readonly", width=15)
        self.protocol_cb.current(0)
        self.protocol_cb.grid(row=0, column=1, padx=5)

        ttk.Label(frame_net, text="目标 IP:").grid(row=1, column=0, sticky="w", pady=5)
        self.ip_entry = ttk.Entry(frame_net, width=18)
        self.ip_entry.insert(0, "Multicast")
        self.ip_entry.grid(row=1, column=1, padx=5)
        ttk.Label(frame_net, text="(sACN默认组播 Multicast，Art-Net填目标IP)").grid(row=1, column=2, sticky="w")
        
        ttk.Label(frame_net, text="DMX 宇宙:").grid(row=2, column=0, sticky="w")
        self.universe_entry = ttk.Entry(frame_net, width=18)
        self.universe_entry.insert(0, "1")
        self.universe_entry.grid(row=2, column=1, padx=5)

        # --- 灯具配接区 (核心分类逻辑) ---
        frame_patch = ttk.LabelFrame(root, text="第二步：添加舞台灯具 (智能分类)", padding=10)
        frame_patch.pack(fill="both", expand=True, padx=10, pady=5)
        
        ttk.Label(frame_patch, text="灯具大类:").grid(row=0, column=0, sticky="w")
        self.fix_type_cb = ttk.Combobox(frame_patch, values=list(FIXTURE_TYPES.keys()), state="readonly", width=25)
        self.fix_type_cb.current(0)
        self.fix_type_cb.grid(row=0, column=1, padx=5)
        
        ttk.Label(frame_patch, text="起始地址码:").grid(row=1, column=0, sticky="w", pady=5)
        self.addr_entry = ttk.Entry(frame_patch, width=10)
        self.addr_entry.insert(0, "1")
        self.addr_entry.grid(row=1, column=1, sticky="w", padx=5, pady=5)
        
        ttk.Label(frame_patch, text="数量:").grid(row=1, column=1, sticky="e", padx=5)
        self.qty_entry = ttk.Entry(frame_patch, width=5)
        self.qty_entry.insert(0, "1")
        self.qty_entry.grid(row=1, column=2, sticky="w", pady=5)
        
        ttk.Button(frame_patch, text="➕ 批量添加到阵列", command=self.add_fixture).grid(row=0, column=3, rowspan=2, padx=10, sticky="nsew")
        
        # 灯具列表显示 (加入滚动条)
        scroll = ttk.Scrollbar(frame_patch)
        self.listbox = tk.Listbox(frame_patch, height=10, yscrollcommand=scroll.set, font=("Consolas", 10))
        scroll.config(command=self.listbox.yview)
        self.listbox.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=10)
        scroll.grid(row=2, column=4, sticky="ns", pady=10)

        # --- 底部：控制区 ---
        frame_ctrl = ttk.Frame(root, padding=10)
        frame_ctrl.pack(fill="x", padx=10, pady=5)
        
        self.btn_start = ttk.Button(frame_ctrl, text="🚀 启动 AI 自动声光引擎 (接管场地)", command=self.toggle_engine)
        self.btn_start.pack(fill="x", ipady=10)
        
        self.status_var = tk.StringVar()
        self.status_var.set("状态：系统就绪。请添加灯具或读取工程文件。")
        ttk.Label(root, textvariable=self.status_var, foreground="green", font=("Microsoft YaHei", 9, "bold")).pack(pady=5)

    # --- 商业功能：动态添加灯具 ---
    def add_fixture(self):
        type_name = self.fix_type_cb.get()
        fix_info = FIXTURE_TYPES[type_name]
        
        try:
            start_addr = int(self.addr_entry.get())
            qty = int(self.qty_entry.get())
            if start_addr < 1 or start_addr > 512 or qty < 1: raise ValueError
        except ValueError:
            messagebox.showerror("参数错误", "地址码必须是 1-512，数量必须大于 0！")
            return
            
        current_addr = start_addr
        for _ in range(qty):
            if current_addr + fix_info["channel_count"] - 1 > 512:
                messagebox.showwarning("地址超限", f"第 {_ + 1} 台灯地址码超过 512，停止添加。")
                break
                
            new_fix = {
                "name": f"{type_name} (Addr: {current_addr})",
                "type": fix_info["type"], # 提取底层分类标识 (spot/wash/par)
                "addr": current_addr,
                "channels": fix_info["default_channels"] # 载入该类灯的默认通道
            }
            active_fixtures.append(new_fix)
            self.refresh_listbox()
            current_addr += fix_info["channel_count"]
            
        self.addr_entry.delete(0, tk.END)
        self.addr_entry.insert(0, str(current_addr)) # 自动推算下一个空闲地址

    def refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for fix in active_fixtures:
            self.listbox.insert(tk.END, f"[DMX: {fix['addr']:03d}] {fix['name']}")

    # --- 商业功能：保存与读取现场工程 (JSON) ---
    def save_show(self):
        if not active_fixtures:
            messagebox.showinfo("提示", "当前没有灯具，无法保存。")
            return
        filepath = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON 工程文件", "*.json")], title="保存灯光配置")
        if filepath:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(active_fixtures, f, ensure_ascii=False, indent=4)
            messagebox.showinfo("成功", "现场工程配置已保存！")

    def load_show(self):
        global active_fixtures
        filepath = filedialog.askopenfilename(filetypes=[("JSON 工程文件", "*.json")], title="读取灯光配置")
        if filepath:
            with open(filepath, 'r', encoding='utf-8') as f:
                active_fixtures = json.load(f)
            self.refresh_listbox()
            messagebox.showinfo("成功", f"成功读取 {len(active_fixtures)} 台灯具配置！")

    def clear_show(self):
        global active_fixtures
        if messagebox.askyesno("确认", "确定要清空当前所有配接的灯具吗？"):
            active_fixtures = []
            self.refresh_listbox()

    # --- 启动引擎 ---
    def toggle_engine(self):
        global is_running
        if not is_running:
            if not active_fixtures:
                messagebox.showwarning("警告", "请先配接灯具，AI 引擎才能知道控制谁！")
                return
                
            is_running = True
            self.btn_start.config(text="🛑 紧急停止接管")
            self.status_var.set("状态：🔴 AI 引擎正在运行！音频监听中... 协议发送中...")
            
            proto = "sACN" if "sACN" in self.protocol_cb.get() else "Art-Net"
            threading.Thread(target=audio_listener_thread, daemon=True).start()
            threading.Thread(target=dmx_engine_thread, args=(self.ip_entry.get(), self.universe_entry.get(), proto), daemon=True).start()
        else:
            is_running = False
            self.btn_start.config(text="🚀 启动 AI 自动声光引擎 (接管场地)")
            self.status_var.set("状态：🟢 已停止。控制权已交还。")

if __name__ == "__main__":
    root = tk.Tk()
    app = LightAgentApp(root)
    root.mainloop()

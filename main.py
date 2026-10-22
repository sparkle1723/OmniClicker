"""
全能连点器 v2.0
================
功能说明：
  - 支持鼠标点击、键盘按键、延时等待三种动作类型
  - 支持动作序列的拖拽排序、编辑、删除
  - 支持脚本的保存/加载（JSON 格式）
  - 运行中显示悬浮窗 + 主界面实时状态
  - 游戏物理模式（底层 Scancode 模拟，应对游戏检测）
  - F10 启动/停止，F12 获取鼠标位置
  - 自动请求管理员权限（部分游戏需要更高权限）
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import threading
import time
import json
import random
import os
import ctypes
import sys
from ctypes import wintypes
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, KeyCode, Controller as KeyboardController, Listener as KeyboardListener


# ============================================================
# 全局常量配置
# ============================================================
# 打包为 exe（PyInstaller onefile）时 __file__ 指向临时解压目录，
# 必须以 exe 自身所在目录作为日志/配置文件的基准目录
if getattr(sys, "frozen", False):
    LOG_DIR = os.path.dirname(sys.executable)
else:
    LOG_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(LOG_DIR, exist_ok=True)

DEFAULT_PRESS_DURATION = 0.15
FLOAT_WIN_WIDTH = 280
FLOAT_WIN_HEIGHT = 180
FLOAT_WIN_NORMAL_ALPHA = 0.88
FLOAT_WIN_HOVER_ALPHA = 1.0
HOTKEY_DEBOUNCE_TIME = 1.5

# 键盘调试日志总开关：True 时额外写入高频「成功」类日志。
# 长时间高频序列下日志 I/O 过重时改为 False（错误类日志始终写入）。
DEBUG_KEY_LOG = False

# 用户配置文件路径（游戏物理模式 / 悬浮窗开关等设置持久化于此）
SETTINGS_FILE = os.path.join(LOG_DIR, "settings.json")

# 动作序列自动存档文件（启动时自动恢复上次编辑的序列，免手动导入配置文件）
SESSION_FILE = os.path.join(LOG_DIR, "last_session.json")

# 扩展键 VK 码集合（发送时需要 KEYEVENTF_EXTENDEDKEY 标志）
EXTENDED_KEYS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,   # PageUp/Down/End/Home/方向键
    0x2D, 0x2E,                                       # Insert / Delete
    0x90, 0x14,                                       # CapsLock / NumLock
    0xA1, 0xA3, 0xA5,                                 # 右 Ctrl / Alt / Shift
    0x5B, 0x5C, 0x5D,                                 # Win 键
    0x2C,                                             # PrintScreen
})

# 鼠标按键按下/释放 SendInput 标志表
MOUSE_FLAG_MAP = {
    "Left":   (0x0002, 0x0004),   # MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    "Right":  (0x0008, 0x0010),
    "Middle": (0x0020, 0x0040),
}

# 各动作类型的必需字段（加载脚本时用于校验）
REQUIRED_FIELDS = {
    'mouse': ('x', 'y', 'btn'),
    'key': ('key',),
    'delay': ('seconds',),
}


# ============================================================
# Windows SendInput 底层结构体定义
# ============================================================
# 这些结构体用于直接调用 Windows API SendInput，
# 可以实现物理级别的输入模拟，绕过大多数应用层的拦截。

class KEYBDINPUT(ctypes.Structure):
    """键盘输入结构体"""
    _fields_ = [
        ("wVk", wintypes.WORD),           # 虚拟键码（如 VK_SPACE=0x20）
        ("wScan", wintypes.WORD),          # 硬件扫描码
        ("dwFlags", wintypes.DWORD),       # 标志位（KEYEVENTF_xxx）
        ("time", wintypes.DWORD),          # 时间戳（0=自动）
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),  # 附加信息
    ]


class MOUSEINPUT(ctypes.Structure):
    """鼠标输入结构体"""
    _fields_ = [
        ("dx", wintypes.LONG),             # X 坐标偏移
        ("dy", wintypes.LONG),             # Y 坐标偏移
        ("mouseData", wintypes.DWORD),     # 滚轮数据等
        ("dwFlags", wintypes.DWORD),       # 标志位（MOUSEEVENTF_xxx）
        ("time", wintypes.DWORD),          # 时间戳
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_I(ctypes.Union):
    """联合体：可容纳键盘或鼠标结构"""
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    """顶层输入结构，type 区分键盘(1)/鼠标(0)"""
    _fields_ = [("type", wintypes.DWORD), ("ii", INPUT_I)]


# ============================================================
# SendInput 底层输入发送函数
# ============================================================

def send_key_event(vk_code, is_press, game_mode=False):
    """
    使用 SendInput 发送键盘事件。
    
    参数:
        vk_code (int): Windows 虚拟键码
        is_press (bool): True=按下, False=释放
        game_mode (bool):
            True  -> 三层覆盖：按键状态表 + SendInput 双事件，
                     最大化兼容各种游戏的输入检测方式
            False -> 标准模式
    """
    user32 = ctypes.windll.user32
    scan_code = user32.MapVirtualKeyW(vk_code, 0)

    flags = 0
    if vk_code in EXTENDED_KEYS:   # 模块级 frozenset，避免每次调用重建列表
        flags |= 0x0001

    if not is_press:
        flags |= 0x0002

    if not game_mode:
        # ====== 标准模式 ======
        ii_ = INPUT_I()
        ii_.ki = KEYBDINPUT(
            vk_code, scan_code, flags, 0, ctypes.pointer(ctypes.c_ulong(0))
        )
        input_obj = INPUT(1, ii_)
        user32.SendInput(1, ctypes.byref(input_obj), ctypes.sizeof(input_obj))
    else:
        # ====== 游戏模式：纯 SendInput 单路径（最低检测风险）======
        # 只使用 SendInput + scancode，这是最接近物理键盘输入的方式
        # 真实键盘按下 → 产生 scancode → Windows 转换为 VK → 应用接收
        # keybd_event 和 SetKeyboardState 已被移除（反作弊检测特征过于明显）

        inputs = (INPUT * 2)()
        # scancode 路径（主路径：模拟物理键盘行为）
        inputs[0].type = 1
        inputs[0].ii = INPUT_I()
        inputs[0].ii.ki = KEYBDINPUT(
            0, scan_code, flags | 0x0008,
            0, ctypes.pointer(ctypes.c_ulong(0))
        )
        # VK 路径（备用：覆盖通过 VK 检测的事件）
        inputs[1].type = 1
        inputs[1].ii = INPUT_I()
        inputs[1].ii.ki = KEYBDINPUT(
            vk_code, 0, flags,
            0, ctypes.pointer(ctypes.c_ulong(0))
        )
        r = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        if r == 0:
            err = ctypes.windll.kernel32.GetLastError()
            safe_log_write(f"SendInput失败 vk=0x{vk_code:02X} press={is_press} err={err}")
        elif DEBUG_KEY_LOG:
            # 成功日志为高频噪声，默认关闭（DEBUG_KEY_LOG=True 时再写）
            safe_log_write(f"SendInput成功 vk=0x{vk_code:02X} press={is_press} r={r}")


def send_mouse_click(btn_type, is_press):
    """
    使用 SendInput 发送鼠标事件。
    始终在当前鼠标位置执行操作（dx=0, dy=0）。
    
    参数:
        btn_type (str): "Left" / "Right" / "Middle"
        is_press (bool): True=按下, False=释放
    """
    down_flag, up_flag = MOUSE_FLAG_MAP[btn_type]  # 模块级常量，避免每次调用重建字典
    flag = down_flag if is_press else up_flag

    ii_ = INPUT_I()
    ii_.mi = MOUSEINPUT(0, 0, 0, flag, 0, ctypes.pointer(ctypes.c_ulong(0)))
    input_obj = INPUT(0, ii_)  # 0 = INPUT_MOUSE

    ctypes.windll.user32.SendInput(1, ctypes.byref(input_obj), ctypes.sizeof(input_obj))


# ============================================================
# DPI 感知设置（解决高 DPI 缩放导致鼠标坐标偏移的问题）
# ============================================================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ============================================================
# 安全日志写入函数
# ============================================================
def safe_log_write(message):
    """安全地写入日志文件，失败时静默忽略"""
    try:
        log_path = os.path.join(LOG_DIR, "key_debug.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass


def safe_log_clear():
    """安全地清空日志文件，失败时静默忽略"""
    try:
        log_path = os.path.join(LOG_DIR, "key_debug.log")
        with open(log_path, "w", encoding="utf-8") as f:
            pass
    except Exception:
        pass


# ============================================================
# 管理员权限相关
# ============================================================

def is_admin():
    """检查当前进程是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def hide_console():
    """
    隐藏当前进程附带的控制台窗口。
    以 python.exe（而非 pythonw.exe）启动时会带一个黑色终端，
    这里通过 ShowWindow(SW_HIDE) 将其隐藏，不影响程序运行。
    """
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # 0 = SW_HIDE
    except Exception:
        pass


def run_as_admin():
    """
    尝试以管理员权限重新启动本程序。
    返回 True 表示已是管理员，False 表示已触发 UAC 请求。
    某些游戏需要管理员权限才能注入模拟输入。
    """
    if is_admin():
        return True
    else:
        # 优先使用 pythonw.exe 重启，避免提权后弹出终端窗口
        exe = sys.executable
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if exe.lower().endswith("python.exe") and os.path.exists(pythonw):
            exe = pythonw
        # 参数中含空格时需加引号，避免路径被截断
        params = " ".join(
            f'"{arg}"' if " " in arg else arg for arg in sys.argv
        )
        # ShellExecuteW 的 runas 动词触发 UAC 提升请求
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 0  # 0 = SW_HIDE，不显示终端
        )
        if result <= 32:
            # 提权失败或用户拒绝 UAC：降级以普通权限继续运行，而不是静默退出
            # （ShellExecuteW 返回 HINSTANCE 数值，<=32 代表各种失败码）
            return True
        return False


# ============================================================
# 动作编辑对话框
# ============================================================

class EditDialog(simpledialog.Dialog):
    """
    编辑/查看已有动作的模态对话框。
    继承自 simpledialog.Dialog，显示时会阻塞主线程。
    
    功能：
        - 鼠标动作：编辑 X/Y 坐标、按键类型、按压时长
        - 键盘动作：重新录入按键、编辑按压时长
        - 延时动作：编辑等待秒数
    """

    def __init__(self, parent, title, action_data, mouse_controller, app_instance):
        self.action_data = action_data       # 要编辑的动作数据
        self.mouse_controller = mouse_controller
        self.app_instance = app_instance     # 主应用实例引用
        self.result = None                   # 编辑结果，None 表示取消
        self.is_capturing = False            # 是否正在键盘录入状态
        self.app_instance.active_dialog = self  # 注册到主应用，用于 F12 分发
        super().__init__(parent, title)

    def body(self, master):
        """构建对话框内容区域，根据动作类型显示不同控件"""
        self.type = self.action_data['type']

        if self.type == 'mouse':
            # --- 鼠标动作编辑界面 ---
            tk.Label(master, text="X 坐标:").grid(row=0, column=0, sticky="w", pady=5)
            self.entry_x = tk.Entry(master)
            self.entry_x.insert(0, str(self.action_data['x']))
            self.entry_x.grid(row=0, column=1, padx=5)

            tk.Label(master, text="Y 坐标:").grid(row=1, column=0, sticky="w", pady=5)
            self.entry_y = tk.Entry(master)
            self.entry_y.insert(0, str(self.action_data['y']))
            self.entry_y.grid(row=1, column=1, padx=5)

            self.btn_get_pos = ttk.Button(
                master, text="获取当前位置 (F12)", command=self.fetch_current_pos
            )
            self.btn_get_pos.grid(row=0, column=2, rowspan=2, padx=10)

            tk.Label(master, text="按键:").grid(row=2, column=0, sticky="w", pady=5)
            self.combo_btn = ttk.Combobox(master, values=["Left", "Right", "Middle"])
            self.combo_btn.set(self.action_data['btn'])
            self.combo_btn.grid(row=2, column=1, padx=5)

            tk.Label(master, text="按压时长(s):").grid(row=3, column=0, sticky="w", pady=5)
            self.entry_duration = tk.Entry(master)
            self.entry_duration.insert(0, str(self.action_data.get('duration', DEFAULT_PRESS_DURATION)))
            self.entry_duration.grid(row=3, column=1, padx=5)

        elif self.type == 'key':
            # --- 键盘动作编辑界面 ---
            tk.Label(master, text="所选按键:").grid(row=0, column=0, sticky="w", pady=5)
            self.lbl_key_display = tk.Label(
                master, text=self.action_data['key'],
                fg="blue", font=("微软雅黑", 9, "bold")
            )
            self.lbl_key_display.grid(row=0, column=1, padx=5)

            self.btn_capture = ttk.Button(
                master, text="点击开始录入按键", command=self.toggle_capture
            )
            self.btn_capture.grid(row=0, column=2, padx=5)

            tk.Label(master, text="按压时长(s):").grid(row=1, column=0, sticky="w", pady=5)
            self.entry_duration = tk.Entry(master)
            self.entry_duration.insert(0, str(self.action_data.get('duration', DEFAULT_PRESS_DURATION)))
            self.entry_duration.grid(row=1, column=1, padx=5)

        elif self.type == 'delay':
            # --- 延时动作编辑界面 ---
            tk.Label(master, text="等待秒数:").grid(row=0, column=0, sticky="w", pady=5)
            self.entry_delay = tk.Entry(master)
            self.entry_delay.insert(0, str(self.action_data['seconds']))
            self.entry_delay.grid(row=0, column=1, padx=5)

        return None

    def toggle_capture(self):
        """切换键盘录入状态"""
        self.is_capturing = not self.is_capturing
        if self.is_capturing:
            self.btn_capture.config(text="请按下键盘任意键...")
            self.lbl_key_display.config(text="等待输入...", fg="red")
        else:
            self.btn_capture.config(text="点击开始录入按键")

    def handle_captured_key(self, key_name):
        """接收到键盘录入结果后的回调"""
        if self.is_capturing:
            self.lbl_key_display.config(text=key_name, fg="green")
            self.is_capturing = False
            self.btn_capture.config(text="点击重新录入")

    def fetch_current_pos(self):
        """获取当前鼠标位置并填入 X/Y 编辑框"""
        x, y = self.mouse_controller.position
        self.entry_x.delete(0, tk.END)
        self.entry_x.insert(0, str(int(x)))
        self.entry_y.delete(0, tk.END)
        self.entry_y.insert(0, str(int(y)))

    def apply(self):
        """确认按钮回调：验证输入并保存结果到 self.result"""
        try:
            if self.type == 'mouse':
                dur = float(self.entry_duration.get())
                if dur < 0:
                    raise ValueError
                self.result = {
                    'type': 'mouse',
                    'x': int(self.entry_x.get()),
                    'y': int(self.entry_y.get()),
                    'btn': self.combo_btn.get(),
                    'duration': dur,
                }
            elif self.type == 'key':
                key_name = self.lbl_key_display.cget("text")
                # 防止录入未完成时把提示文字当成按键名存进去
                if self.is_capturing or key_name in ("等待输入...",):
                    messagebox.showerror("错误", "请先完成按键录入")
                    self.result = None
                    return
                dur = float(self.entry_duration.get())
                if dur < 0:
                    raise ValueError
                self.result = {
                    'type': 'key',
                    'key': key_name,
                    'duration': dur,
                }
            elif self.type == 'delay':
                val = float(self.entry_delay.get())
                if val < 0:
                    raise ValueError
                self.result = {
                    'type': 'delay',
                    'seconds': val,
                }
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数值")
            self.result = None

    def destroy(self):
        """销毁对话框时清理主应用的 active_dialog 引用"""
        self.app_instance.active_dialog = None
        super().destroy()


# ============================================================
# 运行状态悬浮窗
# ============================================================

class FloatWindow:
    """
    紧凑型悬浮窗 - 执行动作序列时显示的半透明状态窗口。
    
    功能：
      - 标题栏含关闭按钮（\u00d7），点击仅关闭窗口不停止执行
      - 进度指示（当前动作序号 / 总数）
      - 进度条（ttk.Progressbar 显示整体执行进度）
      - 倒计时颜色渐变（黄 \u2192 橙 \u2192 红）
      - 鼠标悬停透明度变化（88% \u2192 100%）
      - 停止按钮悬浮高亮效果
    """

    def __init__(self, parent, stop_callback, close_callback=None):
        self.window = tk.Toplevel(parent)

        # 窗口样式设置
        self.window.title("连点器 - 运行中")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.normal_alpha = FLOAT_WIN_NORMAL_ALPHA
        self.hover_alpha = FLOAT_WIN_HOVER_ALPHA
        self.window.attributes("-alpha", self.normal_alpha)

        self.stop_callback = stop_callback
        self.close_callback = close_callback

        # 鼠标悬停透明度防抖（防止划过子控件时闪烁）
        self._leave_timer = None

        # 窗口整体拖拽（绑定在标题栏以外的区域则不启动拖拽）
        self.window.bind("<Button-1>", self.start_move)
        self.window.bind("<B1-Motion>", self.do_move)

        # 鼠标悬停透明度效果（带 300ms 防抖）
        self.window.bind("<Enter>", self._on_enter)
        self.window.bind("<Leave>", self._on_leave)

        # ====== 主容器 ======
        self.frame = tk.Frame(
            self.window, bg="#2c3e50",
            highlightbackground="#34495e", highlightthickness=1
        )
        self.frame.pack(fill="both", expand=True)

        # ====== 标题栏 ======
        title_bar = tk.Frame(self.frame, bg="#34495e")
        title_bar.pack(fill="x")

        # 绿色状态指示灯
        tk.Label(
            title_bar, text="\u25cf", fg="#2ecc71", bg="#34495e",
            font=("Arial", 8)
        ).pack(side="left", padx=(8, 2), pady=4)

        tk.Label(
            title_bar, text="全能连点器",
            fg="#ecf0f1", bg="#34495e", font=("微软雅黑", 9)
        ).pack(side="left", pady=4)

        # 动作进度指示
        self.lbl_progress = tk.Label(
            title_bar, text="0/0",
            fg="#bdc3c7", bg="#34495e", font=("微软雅黑", 8)
        )
        self.lbl_progress.pack(side="right", padx=3, pady=4)

        # 关闭按钮（\u00d7）
        btn_close = tk.Label(
            title_bar, text="\u00d7", fg="#95a5a6", bg="#34495e",
            font=("微软雅黑", 12, "bold"), width=3, cursor="hand2"
        )
        btn_close.pack(side="right", padx=(0, 2), pady=2)
        btn_close.bind("<Button-1>", self.on_close_clicked)
        btn_close.bind("<Enter>", lambda e: btn_close.config(fg="#ecf0f1", bg="#e74c3c"))
        btn_close.bind("<Leave>", lambda e: btn_close.config(fg="#95a5a6", bg="#34495e"))

        # ====== 主体内容 ======
        body = tk.Frame(self.frame, bg="#2c3e50", padx=10, pady=5)
        body.pack(fill="both", expand=True)

        # 当前执行的动作
        self.lbl_current = tk.Label(
            body, text="当前: 准备开始...",
            fg="#ecf0f1", bg="#2c3e50",
            font=("微软雅黑", 9, "bold"), anchor="w"
        )
        self.lbl_current.pack(fill="x", pady=(0, 1))

        # 下一步动作
        self.lbl_next = tk.Label(
            body, text="下步: 脚本启动",
            fg="#95a5a6", bg="#2c3e50",
            font=("微软雅黑", 8), anchor="w"
        )
        self.lbl_next.pack(fill="x")

        # 分隔线
        tk.Frame(body, bg="#34495e", height=1).pack(fill="x", pady=4)

        # ====== 倒计时行 + 进度条 ======
        cd_row = tk.Frame(body, bg="#2c3e50")
        cd_row.pack(fill="x")

        self.lbl_countdown = tk.Label(
            cd_row, text="倒计时: 0.0s",
            fg="#f1c40f", bg="#2c3e50",
            font=("微软雅黑", 10, "bold")
        )
        self.lbl_countdown.pack(side="left")

        # 进度条
        self.progress = ttk.Progressbar(
            cd_row, mode="determinate", length=80, value=0
        )
        self.progress.pack(side="right", padx=(5, 0))

        # ====== 结束执行按钮 ======
        self.btn_stop = tk.Button(
            body, text="■ 结束执行", command=self.stop_callback,
            bg="#e74c3c", fg="white", relief="flat",
            padx=12, pady=3, activebackground="#c0392b",
            activeforeground="white", cursor="hand2",
            font=("微软雅黑", 9, "bold")
        )
        self.btn_stop.pack(pady=(4, 0))
        self.btn_stop.bind("<Enter>", lambda e: self.btn_stop.config(bg="#c0392b"))
        self.btn_stop.bind("<Leave>", lambda e: self.btn_stop.config(bg="#e74c3c"))

        # ====== 窗口定位：屏幕顶部中央 ======
        screen_w = self.window.winfo_screenwidth()
        pos_x = (screen_w - FLOAT_WIN_WIDTH) // 2
        pos_y = 50
        self.window.geometry(f"{FLOAT_WIN_WIDTH}x{FLOAT_WIN_HEIGHT}+{pos_x}+{pos_y}")

        # 强制显示
        self.window.update_idletasks()
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def on_close_clicked(self, event=None):
        """关闭按钮回调：仅关闭悬浮窗，不停止执行序列"""
        if self.close_callback:
            self.close_callback()
        self.destroy()
        return "break"  # 阻止事件冒泡，避免触发拖拽

    def start_move(self, event):
        """记录拖拽起始位置"""
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        """根据鼠标移动增量更新窗口位置"""
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.window.winfo_x() + deltax
        y = self.window.winfo_y() + deltay
        self.window.geometry(f"+{x}+{y}")

    def _on_enter(self, event):
        """鼠标进入窗口：取消延迟变暗，立即全透明"""
        if self._leave_timer:
            self.window.after_cancel(self._leave_timer)
            self._leave_timer = None
        self.window.attributes("-alpha", self.hover_alpha)

    def _on_leave(self, event):
        """
        鼠标离开窗口：延迟 300ms 再变暗。
        防止划过按钮/文字等子控件时频繁闪烁。
        """
        if self._leave_timer:
            self.window.after_cancel(self._leave_timer)
        self._leave_timer = self.window.after(
            100, lambda: self.window.attributes("-alpha", self.normal_alpha)
        )

    def update_status(self, current_text, next_text, countdown_val,
                      progress_text="", progress_pct=None):
        """
        更新悬浮窗的状态显示（从工作线程通过 root.after 调用）。
        
        参数:
            current_text: 当前动作描述
            next_text: 下一步动作描述
            countdown_val: 倒计时剩余秒数
            progress_text: 进度文字（如 "3/10"），空字符串则不更新
            progress_pct: 进度百分比（0~100），None 表示不更新进度条
        """
        try:
            self.lbl_current.config(text=f"当前: {current_text}")
            self.lbl_next.config(text=f"下步: {next_text}")

            # 倒计时颜色：>3s 黄色，1~3s 橙色，<1s 红色
            if countdown_val <= 1.0:
                color = "#e74c3c"    # 红色 - 即将执行
            elif countdown_val <= 3.0:
                color = "#f39c12"    # 橙色 - 接近中
            else:
                color = "#f1c40f"    # 黄色 - 正常
            self.lbl_countdown.config(text=f"倒计时: {countdown_val:.1f}s", fg=color)

            # 更新进度信息
            if progress_text:
                self.lbl_progress.config(text=progress_text)
            if progress_pct is not None:
                self.progress["value"] = progress_pct

            self.window.attributes("-topmost", True)
            self.window.lift()
        except Exception:
            pass

    def destroy(self):
        """安全销毁窗口"""
        try:
            if self.window:
                self.window.destroy()
        except Exception:
            pass


# ============================================================
# 主应用程序
# ============================================================

class ClickerApp:
    """
    全能连点器主程序。
    
    功能模块：
        1. 动作添加面板（左侧）：鼠标、键盘、延时三种动作
        2. 动作序列列表（右侧）：显示已添加动作，支持双击编辑和拖拽排序
        3. 脚本管理：保存/加载 JSON 格式的动作序列
        4. 执行引擎：多线程执行动作序列，实时更新界面状态
        5. 全局热键：F10 启动/停止，F12 获取鼠标位置
    """

    def __init__(self, root):
        self.root = root
        title = "全能连点器 v2.0"
        if is_admin():
            title += " (管理员模式)"
        self.root.title(title)
        self.root.geometry("850x900")
        self.root.resizable(True, True)   # 允许用户缩放窗口

        # ---------- 运行时状态变量 ----------
        self.running = False              # 是否正在执行
        self.active_dialog = None         # 当前打开的编辑对话框引用
        self.mouse_controller = MouseController()
        self.keyboard_controller = KeyboardController()
        self.actions = []                 # 动作序列列表
        self.float_win = None             # 悬浮窗实例
        self.is_main_capturing = False    # 主界面是否正在录入按键

        # ---------- UI 绑定变量 ----------
        self.mouse_button = tk.StringVar(value="Left")
        self.mouse_x = tk.StringVar(value="0")
        self.mouse_y = tk.StringVar(value="0")
        self.keyboard_key = tk.StringVar(value="f")    # 默认按键预设为 f
        self.delay_seconds = tk.DoubleVar(value=1.0)
        self.loop_count = tk.IntVar(value=0)           # 0 = 无限循环
        self.press_duration = tk.DoubleVar(value=DEFAULT_PRESS_DURATION)

        # 游戏物理模式开关
        self.game_mode_var = tk.BooleanVar(value=True)  # 默认开启游戏模式

        # 悬浮窗显示开关（默认开启）
        self.show_float_win_var = tk.BooleanVar(value=True)

        # 读取上次保存的用户设置（游戏物理模式 / 悬浮窗开关）
        self.load_settings()

        # ---------- 热键设置 ----------
        self.hotkey_start_stop = Key.f10   # F10: 启动/停止
        self.hotkey_add_pos = Key.f12      # F12: 获取鼠标位置
        self._pressed_keys = {}        # 热键按下时间戳字典（防抖），key→perf_counter

        # 初始化界面
        self.setup_ui()
        self.setup_hotkeys()
        self.setup_drag_and_drop()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 自动恢复上次会话的动作序列与循环次数
        self.load_session()

    def on_closing(self):
        """窗口关闭时的清理工作"""
        self.running = False
        self.save_settings()
        self.save_session()
        if hasattr(self, 'listener') and self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
        self.root.destroy()

    # ============================================================
    # UI 构建
    # ============================================================

    def setup_ui(self):
        """构建优化后的程序主界面（分页布局 + 集成状态栏）"""
        # ============================================================
        # 左侧面板：使用 Notebook 分页管理
        # ============================================================
        left_frame = ttk.Frame(self.root, padding=10)
        left_frame.pack(side="left", fill="y", expand=False)

        notebook = ttk.Notebook(left_frame)
        notebook.pack(fill="both", expand=True)

        # --------------------
        # Tab 1: 动作添加
        # --------------------
        tab_actions = ttk.Frame(notebook, padding=8)
        notebook.add(tab_actions, text="  动作添加  ")

        # -- 鼠标动作 --
        mouse_group = ttk.LabelFrame(tab_actions, text="鼠标动作", padding=8)
        mouse_group.pack(fill="x", pady=(0, 4))

        ttk.Label(mouse_group, text="按键:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Combobox(
            mouse_group, textvariable=self.mouse_button,
            values=["Left", "Right", "Middle"], width=9
        ).grid(row=0, column=1, sticky="w")

        pos_frame = ttk.Frame(mouse_group)
        pos_frame.grid(row=1, column=0, columnspan=2, pady=4, sticky="w")
        ttk.Label(pos_frame, text="X:").pack(side="left")
        ttk.Entry(pos_frame, textvariable=self.mouse_x, width=6).pack(side="left", padx=2)
        ttk.Label(pos_frame, text="Y:").pack(side="left")
        ttk.Entry(pos_frame, textvariable=self.mouse_y, width=6).pack(side="left", padx=2)

        dur_frame = ttk.Frame(mouse_group)
        dur_frame.grid(row=2, column=0, columnspan=2, pady=4, sticky="w")
        ttk.Label(dur_frame, text="按压时长(s):").pack(side="left")
        ttk.Entry(dur_frame, textvariable=self.press_duration, width=8).pack(side="left", padx=4)

        btn_row_mouse = ttk.Frame(mouse_group)
        btn_row_mouse.grid(row=3, column=0, columnspan=2, pady=(6, 0), sticky="ew")
        ttk.Button(
            btn_row_mouse, text="获取位置 (F12)", command=self.handle_f12_press
        ).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(
            btn_row_mouse, text="添加鼠标", command=self.add_mouse_action_from_vars
        ).pack(side="left", fill="x", expand=True, padx=1)

        ttk.Separator(tab_actions, orient="horizontal").pack(fill="x", pady=4)

        # -- 键盘动作 --
        kb_group = ttk.LabelFrame(tab_actions, text="键盘动作", padding=8)
        kb_group.pack(fill="x", pady=(0, 4))

        kb_row0 = ttk.Frame(kb_group)
        kb_row0.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        self.lbl_kb_capture = ttk.Label(
            kb_row0, text="所选按键: f", font=("微软雅黑", 9, "bold")
        )
        self.lbl_kb_capture.pack(side="left")
        self.btn_kb_capture = ttk.Button(
            kb_row0, text="录入按键", command=self.toggle_main_kb_capture, width=10
        )
        self.btn_kb_capture.pack(side="right")

        ttk.Label(kb_group, text="按压时长(s):").grid(row=1, column=0, sticky="w", padx=(0, 4))
        ttk.Entry(kb_group, textvariable=self.press_duration, width=12).grid(row=1, column=1, sticky="w")

        ttk.Button(
            kb_group, text="添加按键动作", command=self.add_key_action
        ).grid(row=2, column=0, columnspan=2, pady=(6, 0), sticky="ew")

        ttk.Separator(tab_actions, orient="horizontal").pack(fill="x", pady=4)

        # -- 延时等待 --
        delay_group = ttk.LabelFrame(tab_actions, text="延时等待", padding=8)
        delay_group.pack(fill="x")

        dl_row = ttk.Frame(delay_group)
        dl_row.pack(fill="x")
        ttk.Label(dl_row, text="等待秒数:").pack(side="left")
        ttk.Entry(dl_row, textvariable=self.delay_seconds, width=8).pack(side="left", padx=4)
        ttk.Button(dl_row, text="添加等待", command=self.add_delay_action).pack(side="right")

        # --------------------
        # Tab 2: 脚本工具
        # --------------------
        tab_tools = ttk.Frame(notebook, padding=8)
        notebook.add(tab_tools, text="  脚本工具  ")

        # -- 运行设置 --
        settings_group = ttk.LabelFrame(tab_tools, text="运行设置", padding=8)
        settings_group.pack(fill="x", pady=(0, 6))

        loop_row = ttk.Frame(settings_group)
        loop_row.pack(fill="x", pady=2)
        ttk.Label(loop_row, text="循环次数 (0=无限):").pack(side="left")
        ttk.Entry(loop_row, textvariable=self.loop_count, width=8).pack(side="right")

        ttk.Checkbutton(
            settings_group,
            text="游戏物理模式 (底层 Scancode)",
            variable=self.game_mode_var,
        ).pack(anchor="w", pady=(4, 0))

        # 悬浮窗开关
        ttk.Checkbutton(
            settings_group,
            text="显示执行悬浮窗",
            variable=self.show_float_win_var,
        ).pack(anchor="w", pady=(2, 0))

        # -- 脚本文件管理 --
        file_group = ttk.LabelFrame(tab_tools, text="脚本文件管理", padding=8)
        file_group.pack(fill="x", pady=6)

        ttk.Button(file_group, text="保存当前脚本", command=self.save_script).pack(fill="x", pady=2)
        ttk.Button(file_group, text="加载现有脚本", command=self.load_script).pack(fill="x", pady=2)

        # -- 主控制按钮 --
        ctrl_group = ttk.LabelFrame(tab_tools, text="控制", padding=8)
        ctrl_group.pack(fill="x", pady=(6, 0))

        self.btn_toggle = ttk.Button(ctrl_group, text="开始执行序列 (F10)", command=self.toggle_running)
        self.btn_toggle.pack(fill="x", pady=2)

        ctrl_btn_row = ttk.Frame(ctrl_group)
        ctrl_btn_row.pack(fill="x", pady=2)
        ttk.Button(ctrl_btn_row, text="清空所有动作", command=self.clear_actions).pack(
            side="left", fill="x", expand=True, padx=1
        )
        ttk.Button(ctrl_btn_row, text="测试悬浮窗", command=self.test_float_win).pack(
            side="left", fill="x", expand=True, padx=1
        )

        # ============================================================
        # 右侧面板：动作序列列表 + 底部集成状态栏
        # ============================================================
        right_frame = ttk.Frame(self.root, padding=(0, 10, 10, 10))
        right_frame.pack(side="left", fill="both", expand=True)

        # -- 标题 --
        ttk.Label(
            right_frame, text="执行序列（双击编辑 / 拖拽排序）",
            font=("微软雅黑", 10, "bold")
        ).pack(anchor="w", pady=(0, 4))

        # -- TreeView 动作列表 --
        columns = ("#", "类型", "内容")
        self.tree = ttk.Treeview(
            right_frame, columns=columns, show="headings", height=20
        )
        self.tree.heading("#", text="ID")
        self.tree.heading("类型", text="类型")
        self.tree.heading("内容", text="详细信息")

        self.tree.column("#", width=40, anchor="center")
        self.tree.column("类型", width=70, anchor="center")
        self.tree.column("内容", width=280)

        self.tree.pack(fill="both", expand=True, pady=(0, 4))

        # -- TreeView 操作按钮 --
        tree_btn_frame = ttk.Frame(right_frame)
        tree_btn_frame.pack(fill="x", pady=(0, 6))
        ttk.Button(
            tree_btn_frame, text="修改选中项", command=self.edit_selected_action
        ).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(
            tree_btn_frame, text="删除选中项", command=self.delete_selected_action
        ).pack(side="left", expand=True, fill="x", padx=2)

        # -- 底部集成状态栏 --
        status_bar = ttk.Frame(right_frame, relief="sunken", padding=6)
        status_bar.pack(fill="x")

        # 第一行：循环信息 + 运行状态
        status_top = ttk.Frame(status_bar)
        status_top.pack(fill="x")
        self.main_lbl_loop = ttk.Label(
            status_top, text="循环: 0 / 0", font=("微软雅黑", 9)
        )
        self.main_lbl_loop.pack(side="left")
        self.status_label = ttk.Label(
            status_top, text="状态: 停止",
            foreground="red", font=("微软雅黑", 9, "bold")
        )
        self.status_label.pack(side="right")

        # 第二行：当前动作
        self.main_lbl_current = ttk.Label(
            status_bar, text="当前动作: 等待开始",
            font=("微软雅黑", 9, "bold"), foreground="#2c3e50"
        )
        self.main_lbl_current.pack(anchor="w", pady=2)

        # 第三行：倒计时 + 提示
        status_bottom = ttk.Frame(status_bar)
        status_bottom.pack(fill="x")
        self.main_lbl_countdown = ttk.Label(
            status_bottom, text="倒计时: 0.0s",
            font=("微软雅黑", 9), foreground="#d35400"
        )
        self.main_lbl_countdown.pack(side="left")
        ttk.Label(
            status_bottom, text="提示: 若点击无效，请以管理员身份运行",
            foreground="#95a5a6", font=("微软雅黑", 8)
        ).pack(side="right")

        # -- TreeView 样式与事件绑定 --
        self.tree.tag_configure("executing", background="#e8f8f5")
        self.tree.bind("<Double-1>", lambda e: self.edit_selected_action())

    # ============================================================
    # 拖拽排序
    # ============================================================

    def setup_drag_and_drop(self):
        """绑定 TreeView 拖拽排序事件"""
        self.tree.bind("<ButtonPress-1>", self.on_drag_start, add="+")
        self.tree.bind("<B1-Motion>", self.on_drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_drag_release, add="+")
        self._drag_item = None

    def on_drag_start(self, event):
        """记录拖拽起始项"""
        item = self.tree.identify_row(event.y)
        if item:
            self._drag_item = item

    def on_drag_motion(self, event):
        """拖拽过程中不做事（但绑定是必需的）"""
        pass

    def on_drag_release(self, event):
        """
        拖拽释放：将源项移动到目标项位置。
        若目标位置无效或与源项相同，则不执行。
        """
        if not self._drag_item:
            return

        target_item = self.tree.identify_row(event.y)
        if target_item and target_item != self._drag_item:
            source_idx = self.tree.index(self._drag_item)
            target_idx = self.tree.index(target_item)

            action = self.actions.pop(source_idx)
            self.actions.insert(target_idx, action)
            self.update_tree()

        self._drag_item = None

    # ============================================================
    # 动作列表操作
    # ============================================================

    def edit_selected_action(self):
        """弹出编辑对话框修改选中的动作"""
        selected = self.tree.selection()
        if not selected:
            return

        idx = self.tree.index(selected[0])
        action = self.actions[idx]

        dialog = EditDialog(self.root, "修改动作", action, self.mouse_controller, self)

        if dialog.result:
            self.actions[idx] = dialog.result
            self.update_tree()

    def delete_selected_action(self):
        """删除选中的动作（支持多选）"""
        selected = self.tree.selection()
        if not selected:
            return

        # 从后往前删除，避免索引偏移
        indices = sorted([self.tree.index(item) for item in selected], reverse=True)
        for idx in indices:
            self.actions.pop(idx)

        self.update_tree()

    # ============================================================
    # F12 处理（获取鼠标位置）
    # ============================================================

    def handle_f12_press(self):
        """
        统一处理 F12 逻辑：
            1. 如果编辑对话框打开，更新对话框中的坐标
            2. 如果列表中选中了鼠标动作，更新该动作坐标
            3. 否则仅更新主界面坐标编辑框
        """
        x, y = self.mouse_controller.position

        # 优先转发到编辑对话框
        if self.active_dialog:
            self.root.after(0, self.active_dialog.fetch_current_pos)
            return

        # 如果选中了列表中的鼠标动作，直接更新该动作的坐标
        selected = self.tree.selection()
        if selected:
            idx = self.tree.index(selected[0])
            if self.actions[idx]['type'] == 'mouse':
                self.actions[idx]['x'] = int(x)
                self.actions[idx]['y'] = int(y)
                self.mouse_x.set(str(int(x)))
                self.mouse_y.set(str(int(y)))
                self.update_tree()
                self.tree.selection_set(self.tree.get_children()[idx])
                return

        # 默认只更新主界面编辑框
        self.mouse_x.set(str(int(x)))
        self.mouse_y.set(str(int(y)))

    # ============================================================
    # 动作添加
    # ============================================================

    def add_mouse_action_from_vars(self):
        """从主界面左侧面板获取参数，添加一个鼠标动作到序列"""
        try:
            x = int(self.mouse_x.get())
            y = int(self.mouse_y.get())
            btn_str = self.mouse_button.get()
            dur = self.press_duration.get()
            if dur < 0:
                raise ValueError
            action = {'type': 'mouse', 'x': x, 'y': y, 'btn': btn_str, 'duration': dur}
            self.actions.append(action)
            self.update_tree()
        except ValueError:
            # 不再静默失败，否则用户会误以为动作已添加
            messagebox.showerror("错误", "请输入有效的坐标和按压时长（时长需为非负数）")

    def add_key_action(self):
        """添加当前录入的按键为键盘动作"""
        key_str = self.keyboard_key.get()
        if not key_str or key_str == "None":
            messagebox.showwarning("警告", "请先录入一个按键")
            return
        try:
            dur = self.press_duration.get()
            if dur < 0:
                raise ValueError
            action = {'type': 'key', 'key': key_str, 'duration': dur}
            self.actions.append(action)
            self.update_tree()
        except ValueError:
            messagebox.showerror("错误", "请输入有效的按压时长（非负数）")

    def toggle_main_kb_capture(self):
        """切换主界面按键录入状态（点击按钮 -> 等待按键 -> 捕获）"""
        self.is_main_capturing = not self.is_main_capturing
        if self.is_main_capturing:
            self.btn_kb_capture.config(text="请按下键盘任意键...")
            self.lbl_kb_capture.config(foreground="red")
        else:
            self.btn_kb_capture.config(text="点击此处开始录入按键")
            self.lbl_kb_capture.config(foreground="black")

    def add_delay_action(self):
        """从延迟秒数输入框获取数值，添加一个等待动作"""
        try:
            s = self.delay_seconds.get()
            if s < 0:
                raise ValueError
            action = {'type': 'delay', 'seconds': s}
            self.actions.append(action)
            self.update_tree()
        except Exception:
            messagebox.showerror("错误", "请输入有效的等待秒数")

    # ============================================================
    # 全局热键
    # ============================================================

    def setup_hotkeys(self):
        """
        设置全局键盘监听器。
        使用 pynput 的底层键盘钩子实现全局热键。
        
        热键分配：
            - F10: 启动/停止执行序列
            - F12: 获取当前鼠标位置
            - 任意键: 按键录入（在录入模式下）
        """

        def on_press(key):
            """按键按下回调（在 pynput 工作线程中运行）"""
            # 将按键转换为字符串表示
            key_name = ""
            if hasattr(key, 'char') and key.char:
                key_name = key.char
            else:
                key_name = str(key).replace("Key.", "")

            # 主界面按键录入模式
            if self.is_main_capturing:
                self.root.after(0, lambda k=key_name: self.update_main_key_display(k))
                return

            # 编辑对话框按键录入模式
            dialog = self.active_dialog
            if dialog and dialog.is_capturing:
                self.root.after(0, lambda k=key_name, d=dialog: d.handle_captured_key(k))
                return

            # 正常热键处理（带防抖+超时兜底，防止 on_release 丢失导致卡死）
            if key in [self.hotkey_start_stop, self.hotkey_add_pos]:
                now = time.perf_counter()
                last_time = self._pressed_keys.get(key, 0)
                if key not in self._pressed_keys or (now - last_time > HOTKEY_DEBOUNCE_TIME):
                    self._pressed_keys[key] = now
                    if key == self.hotkey_start_stop:
                        self.root.after(0, self.toggle_running)
                    elif key == self.hotkey_add_pos:
                        self.root.after(0, self.handle_f12_press)

        def on_release(key):
            """按键释放回调：从热键字典中移除"""
            self._pressed_keys.pop(key, None)

        # 启动后台键盘监听器
        self.listener = KeyboardListener(on_press=on_press, on_release=on_release)
        self.listener.start()

    def update_main_key_display(self, key_name):
        """主界面按键录入完成后的回调：更新显示并重置状态"""
        self.keyboard_key.set(key_name)
        self.lbl_kb_capture.config(text=f"所选按键: {key_name}", foreground="green")
        self.is_main_capturing = False
        self.btn_kb_capture.config(text="点击重新录入")

    # ============================================================
    # 列表刷新
    # ============================================================

    def clear_actions(self):
        """清空所有已添加的动作（需用户确认）"""
        if messagebox.askyesno("确认", "确定要清空所有已添加的动作吗？"):
            self.actions = []
            self.update_tree()

    def update_tree(self):
        """刷新 TreeView 显示，同步 self.actions 的内容"""
        # 清空现有显示
        for item in self.tree.get_children():
            self.tree.delete(item)

        # 逐项插入
        for i, a in enumerate(self.actions):
            if a['type'] == 'mouse':
                dur = a.get('duration', DEFAULT_PRESS_DURATION)
                content = f"点击 {a['btn']} @ ({a['x']}, {a['y']}) [按压 {dur}s]"
            elif a['type'] == 'key':
                dur = a.get('duration', DEFAULT_PRESS_DURATION)
                content = f"按下按键: {a['key']} [按压 {dur}s]"
            else:
                content = f"等待 {a['seconds']} 秒"
            self.tree.insert("", "end", values=(i + 1, a['type'], content))

        # 列表每次变化后自动存档，意外退出/崩溃也不会丢失序列
        self.save_session()

    def save_session(self):
        """自动保存当前动作序列与循环次数到 last_session.json"""
        try:
            data = {
                "loop_count": self.loop_count.get(),
                "actions": self.actions,
            }
            with open(SESSION_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def load_session(self):
        """启动时自动恢复上次会话的动作序列；无存档文件时静默跳过"""
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                valid, _ = self.validate_actions(data.get("actions", []))
                if valid:
                    self.actions = valid
                    loops = data.get("loop_count", 0)
                    if isinstance(loops, int) and not isinstance(loops, bool) and loops >= 0:
                        self.loop_count.set(loops)
                    self.update_tree()
        except Exception:
            pass

    # ============================================================
    # 脚本文件管理
    # ============================================================

    def save_script(self):
        """将当前动作序列保存为 JSON 文件"""
        if not self.actions:
            messagebox.showwarning("警告", "当前没有可保存的动作序列")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="保存脚本",
        )

        if file_path:
            try:
                data = {
                    "loop_count": self.loop_count.get(),
                    "actions": self.actions,
                }
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                messagebox.showinfo("成功", f"脚本已成功保存至:\n{file_path}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {str(e)}")

    @staticmethod
    def validate_actions(actions):
        """
        校验动作列表的合法性，返回 (有效动作列表, 错误信息列表)。
        结构损坏的动作会被过滤掉，避免执行线程运行时 KeyError 崩溃。
        """
        valid, errors = [], []
        if not isinstance(actions, list):
            return [], ["脚本格式错误：actions 不是列表"]
        for i, a in enumerate(actions):
            if not isinstance(a, dict):
                errors.append(f"第{i+1}个动作不是对象")
                continue
            a_type = a.get('type')
            if a_type not in REQUIRED_FIELDS:
                errors.append(f"第{i+1}个动作类型未知: {a_type!r}")
                continue
            missing = [f for f in REQUIRED_FIELDS[a_type] if f not in a]
            if missing:
                errors.append(f"第{i+1}个动作({a_type})缺少字段: {missing}")
                continue
            if a_type == 'mouse':
                if not all(isinstance(a.get(k), (int, float)) for k in ('x', 'y')):
                    errors.append(f"第{i+1}个动作坐标必须为数字")
                    continue
                if a['btn'] not in MOUSE_FLAG_MAP:
                    errors.append(f"第{i+1}个动作按键无效: {a['btn']!r}")
                    continue
            elif a_type == 'delay':
                if not isinstance(a.get('seconds'), (int, float)) or a['seconds'] < 0:
                    errors.append(f"第{i+1}个动作延时秒数必须为非负数字")
                    continue
            elif a_type == 'key':
                if not isinstance(a.get('key'), str) or not a['key'].strip():
                    errors.append(f"第{i+1}个动作按键名无效")
                    continue
            valid.append(a)
        return valid, errors

    def load_settings(self):
        """从 settings.json 读取上次保存的用户设置，失败时静默使用默认值"""
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data.get("game_mode"), bool):
                    self.game_mode_var.set(data["game_mode"])
                if isinstance(data.get("show_float_win"), bool):
                    self.show_float_win_var.set(data["show_float_win"])
        except Exception:
            pass

    def save_settings(self):
        """关闭窗口时保存设置，重启软件后保留用户上次的选择"""
        try:
            data = {
                "game_mode": self.game_mode_var.get(),
                "show_float_win": self.show_float_win_var.get(),
            }
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def load_script(self):
        """从 JSON 文件加载动作序列"""
        file_path = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="加载脚本",
        )

        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                valid, errors = self.validate_actions(data.get("actions", []))
                if errors:
                    messagebox.showwarning(
                        "警告",
                        "脚本中部分动作无效，已自动过滤：\n" + "\n".join(errors)
                    )
                if not valid:
                    messagebox.showerror("错误", "脚本中没有找到任何有效动作")
                    return
                self.actions = valid
                loops = data.get("loop_count", 0)
                self.loop_count.set(
                    loops if isinstance(loops, int) and not isinstance(loops, bool) and loops >= 0 else 0
                )
                self.update_tree()
                if not errors:
                    messagebox.showinfo("成功", f"脚本已成功从以下位置加载:\n{file_path}")
            except Exception as e:
                messagebox.showerror("错误", f"加载失败: {str(e)}")

    # ============================================================
    # 执行控制
    # ============================================================

    def toggle_running(self):
        """切换执行状态（启动/停止）"""
        if not self.running:
            if not self.actions:
                messagebox.showwarning("警告", "请先添加或加载一些动作到序列中")
                return
            self.start_sequence()
        else:
            self.stop_sequence()

    def test_float_win(self):
        """手动测试悬浮窗（调试用）"""
        if self.float_win:
            self.float_win.destroy()
            self.float_win = None
            messagebox.showinfo("信息", "已关闭现有悬浮窗")
        else:
            self.float_win = FloatWindow(
                self.root, lambda: self.test_float_win(),
                close_callback=self.close_float_win
            )
            self.float_win.update_status("测试模式", "无", 99.9)
            messagebox.showinfo("信息", "尝试打开悬浮窗，请检查屏幕顶部中央")

    def start_sequence(self):
        """启动动作序列执行"""
        self.running = True
        self.status_label.config(text="状态: 运行中", foreground="green")
        self.btn_toggle.config(text="停止执行 (F10)")

        # 根据用户选择决定是否创建悬浮窗
        if self.show_float_win_var.get():
            self.float_win = FloatWindow(
                self.root, self.stop_sequence,
                close_callback=self.close_float_win
            )

        # 在主线程先读取参数做值快照，避免工作线程访问 tkinter 变量（线程不安全）
        loops = self.loop_count.get()
        game_mode_snapshot = self.game_mode_var.get()

        # 在工作线程中执行动作序列
        threading.Thread(
            target=self.sequence_worker,
            args=(loops, game_mode_snapshot),
            daemon=True,
        ).start()

    def stop_sequence(self):
        """停止动作序列执行（防重入：已停止则直接跳过）"""
        if not self.running:
            return
        self.running = False
        self.status_label.config(text="状态: 停止", foreground="red")
        self.btn_toggle.config(text="开始执行序列 (F10)")

        # 重置主界面状态显示
        self.main_lbl_loop.config(text="当前循环: 0 / 0")
        self.main_lbl_current.config(text="当前动作: 等待开始")
        self.main_lbl_countdown.config(text="倒计时: 0.0s")
        for item in self.tree.get_children():
            self.tree.item(item, tags=())

        # 关闭悬浮窗
        if self.float_win:
            self.float_win.destroy()
            self.float_win = None

    def close_float_win(self):
        """关闭悬浮窗的回调（不停止执行序列，仅清除引用）"""
        self.float_win = None

    def update_main_status(self, current_text, countdown_val, loop_info, highlight_idx=None):
        """
        更新主界面右侧的实时状态显示。
        此方法通过 root.after(0, ...) 从工作线程调用，确保线程安全。
        
        参数:
            current_text: 当前动作描述文本
            countdown_val: 倒计时剩余秒数
            loop_info: 循环信息字符串（如 "3 / ∞"）
            highlight_idx: 需要在 TreeView 中高亮的动作索引，None 表示不高亮
        """
        try:
            self.main_lbl_current.config(text=f"当前动作: {current_text}")
            self.main_lbl_countdown.config(text=f"倒计时: {countdown_val:.1f}s")
            self.main_lbl_loop.config(text=f"当前循环: {loop_info}")

            # 高亮当前正在执行的动作行
            if highlight_idx is not None:
                for i, item in enumerate(self.tree.get_children()):
                    if i == highlight_idx:
                        self.tree.item(item, tags=("executing",))
                        self.tree.see(item)  # 自动滚动到可见位置
                    else:
                        self.tree.item(item, tags=())
        except Exception:
            pass

    # ============================================================
    # 核心执行引擎
    # ============================================================

    def sequence_worker(self, loops, game_mode):
        """
        执行引擎异常保护包装层。
        无论执行过程中抛出什么异常，最后一定复位界面状态，
        绝不会让程序永远卡在「运行中」（stop_sequence 可重入安全）。
        loops / game_mode 为主线程传入的值快照，
        工作线程内不得直接读取 tkinter 变量（线程不安全）。
        """
        try:
            self._run_sequence(loops, game_mode)
        except Exception:
            import traceback
            safe_log_write(f"序列执行异常: {traceback.format_exc()}")
        finally:
            self.root.after(0, self.stop_sequence)

    def _run_sequence(self, loops, game_mode):
        """
        动作序列执行引擎（在独立工作线程中运行，由 sequence_worker 包装保护）。
        
        执行流程：
            1. 2 秒启动倒计时
            2. 逐循环遍历动作序列
            3. 每个动作执行前更新悬浮窗和主界面状态
            4. 鼠标动作：移动光标 -> 按下 -> 延时 -> 释放
            5. 键盘动作：查找虚拟键码 -> 按下 -> 延时 -> 释放
            6. 延时动作：精确到 0.01s 的等待
            7. 检测循环次数，达到后自动停止
        """
        # 常用特殊按键的 Windows 虚拟键码映射表
        # 这些键没有直接的字符表示，需要查表获取 VK 码
        VK_MAP = {
            'enter': 0x0D, 'space': 0x20, 'tab': 0x09, 'backspace': 0x08,
            'shift': 0x10, 'shift_l': 0xA0, 'shift_r': 0xA1,
            'ctrl': 0x11, 'ctrl_l': 0xA2, 'ctrl_r': 0xA3,
            'alt': 0x12, 'alt_l': 0xA4, 'alt_r': 0xA5,
            'esc': 0x1B, 'caps_lock': 0x14,
            'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
            'insert': 0x2D, 'delete': 0x2E, 'home': 0x24, 'end': 0x23,
            'page_up': 0x21, 'page_down': 0x22,
            'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74,
            'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79,
            'f11': 0x7A, 'f12': 0x7B,
        }

        safe_log_clear()
        safe_log_write(f"=== 序列启动 === 动作数={len(self.actions)}")
        for idx, act in enumerate(self.actions):
            safe_log_write(f"  [{idx}] type={act.get('type','?')} keys={list(act.keys())}")

        current_loop = 0

        # ====== 阶段一：启动倒计时 2 秒 ======
        start_delay = 2.0
        start_time_init = time.perf_counter()  # 高精度计时器
        while self.running:
            elapsed = time.perf_counter() - start_time_init
            if elapsed >= start_delay:
                break
            remaining = max(0, start_delay - elapsed)
            # 计算倒计时进度百分比
            countdown_pct = int((start_delay - remaining) / start_delay * 100)
            fw = self.float_win  # 取局部引用，避免判断后被主线程置 None（竞态）
            if fw:
                self.root.after(
                    0, fw.update_status,
                    "准备开始...", "脚本启动", remaining,
                    "", countdown_pct
                )
            self.root.after(
                0, self.update_main_status,
                "准备开始...", remaining, f"0 / {loops if loops > 0 else '∞'}"
            )
            time.sleep(0.05)

        # ====== 阶段二：主执行循环 ======
        total_actions = len(self.actions)  # 动作总数（用于进度计算）
        while self.running:
            # 检查循环次数限制
            if loops > 0 and current_loop >= loops:
                self.root.after(0, self.stop_sequence)
                break

            loop_display = f"{current_loop + 1} / {loops if loops > 0 else '∞'}"
            # 使用主线程启动时传入的游戏模式快照（tkinter 变量不可跨线程读取）
            current_game_mode = game_mode

            for i, action in enumerate(self.actions):
                if not self.running:
                    break

                safe_log_write(f"执行动作[{i}] type={action.get('type','?')} loop={current_loop}")

                current_text = self.get_action_desc(action)
                # 计算下一步动作描述（用于悬浮窗预览）
                if i + 1 < len(self.actions):
                    next_text = self.get_action_desc(self.actions[i + 1])
                else:
                    # 列表末尾：如果是无限循环或还有下一轮，则回到第一个动作
                    if loops == 0 or current_loop + 1 < loops:
                        next_text = self.get_action_desc(self.actions[0])
                    else:
                        next_text = "无"

                # 计算进度信息
                action_idx = i + 1  # 当前动作序号（从 1 开始）
                progress_text = f"{action_idx}/{total_actions}"
                progress_pct = int(action_idx / total_actions * 100)

                # 更新界面状态; 使用 root.after 确保线程安全
                fw = self.float_win  # 局部引用防竞态
                if fw:
                    self.root.after(
                        0, fw.update_status,
                        current_text, next_text, 0.0,
                        progress_text, progress_pct
                    )
                self.root.after(
                    0, self.update_main_status, current_text, 0.0, loop_display, i
                )

                # ------ 鼠标动作执行 ------
                if action['type'] == 'mouse':
                    target_x, target_y = action['x'], action['y']
                    btn_type = action['btn']
                    press_dur = action.get('duration', DEFAULT_PRESS_DURATION)
                    press_dur = max(0.05, press_dur + random.uniform(-0.04, 0.04))
                    if current_game_mode and press_dur < 0.2:
                        press_dur = 0.2
                    offset_x = random.randint(-2, 2)
                    offset_y = random.randint(-2, 2)

                    ctypes.windll.user32.SetCursorPos(
                        target_x + offset_x, target_y + offset_y
                    )
                    time.sleep(0.1)

                    # 2. 使用 SendInput 执行物理级点击
                    send_mouse_click(btn_type, True)   # 按下

                    # 3. 按压期间微抖动（仅游戏模式）
                    if current_game_mode:
                        jx = target_x + random.randint(-2, 2)
                        jy = target_y + random.randint(-2, 2)
                        ctypes.windll.user32.SetCursorPos(jx, jy)
                        ctypes.windll.user32.SetCursorPos(
                            target_x + random.randint(-1, 1),
                            target_y + random.randint(-1, 1)
                        )

                    time.sleep(press_dur)               # 按压保持
                    send_mouse_click(btn_type, False)   # 释放

                # ------ 键盘动作执行 ------
                elif action['type'] == 'key':
                    safe_log_write(f"进入键盘分支 key={action.get('key','?')} game_mode={current_game_mode}")
                    try:
                        key_raw = str(action['key']).strip()   # 保留原始大小写，用于符号/大写字母查表
                        key_val = key_raw.lower()
                        press_dur = action.get('duration', DEFAULT_PRESS_DURATION)
                        press_dur = max(0.05, press_dur + random.uniform(-0.04, 0.04))
                        if current_game_mode and press_dur < 0.25:
                            press_dur = 0.25

                        vk_code = 0
                        need_shift = False
                        if key_val in VK_MAP:
                            vk_code = VK_MAP[key_val]
                        elif len(key_raw) == 1:
                            ctypes.windll.user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
                            ctypes.windll.user32.VkKeyScanW.restype = wintypes.SHORT
                            scan = ctypes.windll.user32.VkKeyScanW(key_raw)
                            if scan != -1:
                                # 低字节为 VK 码，第 8 位为 Shift 修饰标志
                                # （旧代码对 -1 失败返回值直接 &0xFF 会得到 0xFF，发出幽灵按键）
                                vk_code = scan & 0xFF
                                need_shift = bool(scan & 0x0100)
                            else:
                                safe_log_write(f"VkKeyScanW无法映射'{key_raw}'(返回-1)，走pynput兜底")

                        safe_log_write(f"key='{key_raw}' vk=0x{vk_code:02X} shift={need_shift} game_mode={current_game_mode}")

                        if vk_code > 0:
                            if need_shift:
                                send_key_event(0x10, True, current_game_mode)  # 左 Shift
                                time.sleep(0.02)
                            send_key_event(vk_code, True, current_game_mode)
                            time.sleep(press_dur)
                            send_key_event(vk_code, False, current_game_mode)
                            if need_shift:
                                send_key_event(0x10, False, current_game_mode)
                            if DEBUG_KEY_LOG:
                                safe_log_write("按键发送完成")
                        else:
                            k = getattr(Key, key_val, key_val)
                            self.keyboard_controller.press(k)
                            time.sleep(press_dur)
                            self.keyboard_controller.release(k)

                    except Exception:
                        import traceback
                        safe_log_write(f"键盘异常: {traceback.format_exc()}")

                # ------ 延时动作执行 ------
                elif action['type'] == 'delay':
                    total_delay = action['seconds']
                    wait_start = time.perf_counter()
                    while self.running:
                        elapsed = time.perf_counter() - wait_start
                        if elapsed >= total_delay:
                            break
                        remaining = max(0, total_delay - elapsed)
                        # 延时进度百分比
                        delay_pct = int(elapsed / total_delay * 100)

                        # 实时更新倒计时和延时进度
                        fw = self.float_win  # 局部引用防竞态
                        if fw:
                            self.root.after(
                                0, fw.update_status,
                                current_text, next_text, remaining,
                                progress_text, delay_pct
                            )
                        self.root.after(
                            0, self.update_main_status,
                            current_text, remaining, loop_display, i
                        )

                        # 智能休眠：剩余时间多时用粗粒度，少时用细粒度提升响应
                        if remaining > 0.2:
                            time.sleep(0.1)
                        else:
                            time.sleep(0.01)

                # 动作间随机间隔 10~30ms，打破节律模式（仅鼠标/键盘动作）
                if action['type'] != 'delay' and self.running:
                    time.sleep(random.uniform(0.01, 0.03))

            # 循环计数自增
            current_loop += 1
            if not self.running:
                break

    @staticmethod
    def get_action_desc(action):
        """将动作字典转换为简短描述文本"""
        if action['type'] == 'mouse':
            return f"点击({action['x']},{action['y']})"
        elif action['type'] == 'key':
            return f"按键[{action['key']}]"
        elif action['type'] == 'delay':
            return f"等待{action['seconds']}s"
        return "未知"


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    # 启动时隐藏可能存在的终端窗口
    hide_console()
    # 尝试获取管理员权限（部分游戏需要）
    if run_as_admin():
        root = tk.Tk()
        app = ClickerApp(root)
        root.mainloop()
    else:
        # run_as_admin 已触发 UAC 并重新启动进程，当前进程退出
        sys.exit()

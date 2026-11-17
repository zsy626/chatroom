# -*- coding: utf-8 -*-
"""
网络聊天室 - 客户端(标签页版)

功能:
1. 登录、群聊
2. 私聊:一个窗口内用标签页切换(群聊 + 各私聊对象),类似微信
3. @私聊:群聊输入框输入「@用户名 消息」直接私聊
4. 消息气泡:自己靠右蓝色,别人靠左浅灰,@提及整条变黄;背景只贴合文字
5. 字体缩放:Ctrl+滚轮 / Ctrl+= / Ctrl+-
6. 文件/图片传输:分片 + 序号重组 + MD5 校验,图片自动内联显示

文件传输协议(与 server.py 配合,原服务端不支持):
  FILE|sender|receiver|filename|total|md5            (receiver 为 ALL 表示群发)
  FILEDATA|sender|receiver|md5|seq|base64data
"""
import sys

# Windows 高分屏适配:让界面在高 DPI 下清晰不模糊
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

import socket
import threading
import os
import math
import base64
import hashlib
import tkinter as tk
from tkinter import scrolledtext, messagebox, filedialog, ttk
from datetime import datetime

# ---------- 全局状态 ----------
client_socket = None
my_name = ""
is_connected = False

# 文件传输相关
CHUNK_SIZE = 48 * 1024        # 每个分片 48KB 原始数据 -> 64KB base64
RECEIVE_DIR = "received_files"  # 接收到的文件保存目录
pending_files = {}            # md5 -> {sender, receiver, filename, total, chunks}
_image_refs = []              # 持有 PhotoImage 引用,防止被 GC

# 私聊标签页
private_tabs = {}             # name -> {"tab":..., "text":..., "input":...}

# 气泡控件引用(用于字体缩放 + 防止 GC)
_bubble_labels = []
_meta_labels = []
_bubble_frames = []

# ---------- 配色 ----------
PRIMARY      = "#4f6ef7"
PRIMARY_DARK = "#3b57e0"

BG     = "#eef1f5"
CARD   = "#ffffff"
BORDER = "#e4e8f0"
TEXT   = "#1f2530"
SUB    = "#8a94a6"

HEADER_BG = "#232a3b"
HEADER_FG = "#ffffff"

BUBBLE_ME_BG    = "#4f6ef7"
BUBBLE_ME_FG    = "#ffffff"
BUBBLE_OTHER_BG = "#f0f2f7"
BUBBLE_HL_BG    = "#ffefb8"

ONLINE  = "#2fd36c"
ERROR   = "#e5484d"
PRIVATE = "#8b5cf6"
SYS     = "#8a94a6"
TIME    = "#aab2c0"

# ---------- 字体(可缩放) ----------
current_font_size = 10
FONT_MAIN  = ("微软雅黑", 10)
FONT_SMALL = ("微软雅黑", 9)
FONT_TITLE = ("微软雅黑", 12, "bold")


def now():
    return datetime.now().strftime("%H:%M:%S")


# ---------- 通用消息渲染(群聊 / 私聊共用) ----------
def _configure_tags(text_widget):
    text_widget.tag_configure("time",    foreground=TIME)
    text_widget.tag_configure("sys",     foreground=SYS)
    text_widget.tag_configure("me",      foreground=PRIMARY)
    text_widget.tag_configure("other",   foreground=TEXT)
    text_widget.tag_configure("private", foreground=PRIVATE)
    text_widget.tag_configure("error",   foreground=ERROR)
    text_widget.tag_configure("_right",  justify="right")
    text_widget.tag_configure("_left",   justify="left")


def _plain(chat_widget, text, tag="other"):
    """在指定聊天区插入普通文本(系统/文件提示),带时间戳。"""
    chat_widget.config(state="normal")
    chat_widget.insert(tk.END, f"[{now()}] ", "time")
    chat_widget.insert(tk.END, text + "\n", tag)
    chat_widget.see(tk.END)
    chat_widget.config(state="disabled")


def _bubble(chat_widget, name, text, is_me, highlight=False, chat_bg=CARD):
    """在指定聊天区插入一条气泡消息。背景只贴合文字长度。"""
    side_anchor = "e" if is_me else "w"
    bubble_bg = BUBBLE_ME_BG if is_me else (BUBBLE_HL_BG if highlight else BUBBLE_OTHER_BG)
    bubble_fg = BUBBLE_ME_FG if is_me else TEXT
    meta = f"{'我' if is_me else name} · {now()}" + (" 🔔" if highlight else "")

    w = chat_widget.winfo_width()
    if w <= 1:
        w = 560
    wrap = max(180, w - 150)

    bubble = tk.Frame(chat_widget, bg=chat_bg)
    meta_lbl = tk.Label(bubble, text=meta, bg=chat_bg, fg=SUB,
                        font=FONT_SMALL, anchor=side_anchor)
    meta_lbl.pack(fill="x")
    content = tk.Frame(bubble, bg=bubble_bg)
    lbl = tk.Label(content, text=text, bg=bubble_bg, fg=bubble_fg, font=FONT_MAIN,
                   padx=12, pady=7, justify="left", anchor="w", wraplength=wrap)
    lbl.pack()
    content.pack(anchor=side_anchor, pady=(3, 0))

    chat_widget.config(state="normal")
    chat_widget.window_create(tk.END, window=bubble, align="top")
    line_start = chat_widget.index("end-1c linestart")
    line_end = chat_widget.index("end-1c lineend")
    chat_widget.tag_add("_right" if is_me else "_left", line_start, line_end)
    chat_widget.insert(tk.END, "\n")
    chat_widget.see(tk.END)
    chat_widget.config(state="disabled")

    _bubble_labels.append(lbl)
    _meta_labels.append(meta_lbl)
    _bubble_frames.append(bubble)


def append_message(text, tag="other"):
    _plain(chat, text, tag)


def append_bubble(name, text, is_me, highlight=False):
    _bubble(chat, name, text, is_me, highlight, chat_bg=CARD)


# ---------- 在线用户列表 ----------
def update_user_list(names):
    user_list.delete(0, tk.END)
    for n in names:
        user_list.insert(tk.END, n)
    update_status_bar()


def on_user_click(event):
    """点击在线名单 -> 切换/打开与该用户的私聊标签页。"""
    sel = user_list.curselection()
    if not sel:
        return
    name = user_list.get(sel[0])
    open_private_chat(name)


def update_status_bar():
    online_label.config(text=f"在线 {user_list.size()} 人")
    me_label.config(text=f"当前用户:{my_name or '—'}")


# ---------- 连接状态 UI ----------
def set_status(connected):
    if connected:
        status_dot.config(fg=ONLINE)
        status_text.config(text=f"已连接 · {my_name}", fg="#9fe8b8")
        disconnect_btn.config(state="normal")
    else:
        status_dot.config(fg="#5a6478")
        status_text.config(text="未连接", fg="#9aa3b8")
        disconnect_btn.config(state="disabled")


def set_disconnected(notice=""):
    global client_socket, is_connected
    was_connected = is_connected
    if client_socket:
        try:
            client_socket.close()
        except OSError:
            pass
        client_socket = None
    is_connected = False
    connect_btn.config(state="normal")
    send_btn.config(state="disabled")
    send_file_btn.config(state="disabled")
    name_entry.config(state="normal")
    ip_entry.config(state="normal")
    port_entry.config(state="normal")
    set_status(False)
    update_status_bar()
    if notice and was_connected:
        append_message(f"[系统] {notice}", "sys")


def disconnect():
    set_disconnected("你已断开连接")


def on_close():
    if client_socket:
        try:
            client_socket.close()
        except OSError:
            pass
    root.destroy()


# ---------- 文件传输 ----------
def sanitize_filename(name):
    name = name.replace("|", "_").replace("\n", "_").replace("\r", "_")
    name = os.path.basename(name)
    return name or "file"


def handle_file_header(line):
    parts = line.split("|", 5)
    if len(parts) < 6:
        return
    _, sender, receiver, filename, total, md5 = parts[:6]
    try:
        total = int(total)
    except ValueError:
        return
    pending_files[md5] = {"sender": sender, "receiver": receiver,
                          "filename": filename, "total": total, "chunks": {}}


def handle_file_data(line):
    parts = line.split("|", 5)
    if len(parts) < 6:
        return
    _, _sender, _receiver, md5, seq, b64 = parts[:6]
    t = pending_files.get(md5)
    if t is None:
        return
    try:
        t["chunks"][int(seq)] = base64.b64decode(b64)
    except Exception:
        return
    if len(t["chunks"]) == t["total"]:
        raw = b"".join(t["chunks"][i] for i in range(t["total"]))
        del pending_files[md5]
        if hashlib.md5(raw).hexdigest() == md5:
            save_received_file(t["sender"], t["receiver"], t["filename"], raw)
        else:
            append_message(f"[系统] 文件 {t['filename']} 校验失败(数据损坏)", "error")


def save_received_file(sender, receiver, filename, raw):
    os.makedirs(RECEIVE_DIR, exist_ok=True)
    safe = sanitize_filename(filename)
    path = os.path.join(RECEIVE_DIR, safe)
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base}({n}){ext}"
        n += 1
    with open(path, "wb") as f:
        f.write(raw)
    info = f"[文件] 来自 {sender},已保存到 {os.path.abspath(path)}({len(raw) / 1024:.1f} KB)"
    if receiver == "ALL":
        _plain(chat, info, "sys")
        display_image_if_possible(path, chat)
    else:
        p = get_or_create_private_tab(sender)
        _plain(p["text"], info, "sys")
        display_image_if_possible(path, p["text"])
        if notebook.select() != str(p["tab"]):
            mark_private_unread(sender, True)


def display_image_if_possible(path, chat_widget):
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".png", ".gif"):
        return
    try:
        img = tk.PhotoImage(file=path)
        _image_refs.append(img)
        chat_widget.config(state="normal")
        chat_widget.image_create(tk.END, image=img)
        chat_widget.insert(tk.END, "\n")
        chat_widget.see(tk.END)
        chat_widget.config(state="disabled")
    except Exception:
        pass


def send_file(target="ALL"):
    global client_socket, my_name, is_connected
    if not is_connected or client_socket is None:
        messagebox.showwarning("提示", "请先连接服务器")
        return

    path = filedialog.askopenfilename(title="选择要发送的文件")
    if not path:
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        messagebox.showerror("读取失败", str(e))
        return
    if not data:
        messagebox.showwarning("提示", "文件为空")
        return

    filename = sanitize_filename(os.path.basename(path))
    md5 = hashlib.md5(data).hexdigest()
    total = math.ceil(len(data) / CHUNK_SIZE)

    try:
        client_socket.sendall(f"FILE|{my_name}|{target}|{filename}|{total}|{md5}\n".encode("utf-8"))
        for i in range(0, len(data), CHUNK_SIZE):
            b64 = base64.b64encode(data[i:i + CHUNK_SIZE]).decode("ascii")
            seq = i // CHUNK_SIZE
            client_socket.sendall(f"FILEDATA|{my_name}|{target}|{md5}|{seq}|{b64}\n".encode("utf-8"))

        size_kb = len(data) / 1024
        if target == "ALL":
            append_message(f"我向所有人发送文件:{filename}({size_kb:.1f} KB)", "me")
        else:
            p = get_or_create_private_tab(target)
            _plain(p["text"], f"我发送了文件 {filename}({size_kb:.1f} KB)", "me")
            notebook.select(p["tab"])
    except Exception as e:
        messagebox.showerror("发送失败", f"网络错误:\n{e}")


# ---------- 解析服务端协议(主线程执行) ----------
def handle_line(line):
    line = line.strip()
    if not line:
        return
    if line.startswith("GROUP|"):
        parts = line.split("|", 2)
        if len(parts) == 3:
            name, text = parts[1], parts[2]
            highlight = bool(my_name) and f"@{my_name}" in text
            if highlight:
                root.bell()
            append_bubble(name, text, is_me=False, highlight=highlight)
    elif line.startswith("PRIVATE|"):
        parts = line.split("|", 2)
        if len(parts) == 3:
            receive_private(parts[1], parts[2])
    elif line.startswith("ERROR|"):
        append_message(f"[系统] {line.split('|', 1)[1]}", "error")
    elif line.startswith("NOTICE|"):
        append_message(f"[系统] {line.split('|', 1)[1]}", "sys")
    elif line.startswith("USERS|"):
        update_user_list([n for n in line[len("USERS|"):].split("|") if n])
    elif line.startswith("FILE|"):
        handle_file_header(line)
    elif line.startswith("FILEDATA|"):
        handle_file_data(line)
    else:
        append_message(f"[系统] {line}", "sys")


# ---------- 连接 / 收发 ----------
def connect_server():
    global client_socket, my_name, is_connected
    my_name = name_entry.get().strip()
    if not my_name:
        messagebox.showwarning("提示", "请输入昵称!")
        return

    ip = ip_entry.get().strip() or "10.114.206.188"
    port_text = port_entry.get().strip() or "8888"
    try:
        port = int(port_text)
    except ValueError:
        messagebox.showwarning("提示", "端口必须是数字!")
        return

    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.connect((ip, port))
    except Exception as e:
        client_socket = None
        messagebox.showerror("连接失败", f"无法连接服务器:\n{e}")
        return

    is_connected = True
    client_socket.sendall(f"LOGIN|{my_name}\n".encode("utf-8"))

    threading.Thread(target=receive_messages, args=(client_socket,), daemon=True).start()

    connect_btn.config(state="disabled")
    send_btn.config(state="normal")
    send_file_btn.config(state="normal")
    name_entry.config(state="disabled")
    ip_entry.config(state="disabled")
    port_entry.config(state="disabled")
    set_status(True)
    append_message(f"[系统] 连接成功,欢迎 {my_name} 进入聊天室!", "sys")
    input_text.focus_set()


def receive_messages(sock):
    buffer = b""
    while True:
        try:
            data = sock.recv(8192)
        except OSError:
            data = b""
        if not data:
            root.after(0, set_disconnected, "与服务器断开连接")
            break
        buffer += data
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            root.after(0, handle_line, line)


def send_message(event=None):
    """群聊输入框:普通消息群发,@用户名 消息 则私聊。"""
    global client_socket, my_name, is_connected
    if not is_connected or client_socket is None:
        return
    msg = input_text.get().strip()
    if not msg:
        return
    try:
        if msg.startswith("@"):
            parts = msg[1:].split(" ", 1)
            if len(parts) != 2 or not parts[1].strip():
                messagebox.showwarning("格式错误", "私聊格式:@用户名 消息")
                return
            receiver, content = parts
            if receiver == my_name:
                messagebox.showwarning("提示", "不能私聊自己")
                return
            client_socket.sendall(f"PRIVATE|{my_name}|{receiver}|{content}\n".encode("utf-8"))
            open_private_chat(receiver)
            _bubble(private_tabs[receiver]["text"], my_name, content, is_me=True, chat_bg=CARD)
        else:
            client_socket.sendall(f"GROUP|{my_name}|{msg}\n".encode("utf-8"))
            append_bubble(my_name, msg, is_me=True)
        input_text.delete(0, tk.END)
    except Exception as e:
        messagebox.showerror("发送失败", f"网络错误:\n{e}")


# ---------- 私聊标签页 ----------
def get_or_create_private_tab(name):
    if name in private_tabs:
        return private_tabs[name]
    tab = tk.Frame(notebook, bg=BG)
    notebook.add(tab, text=name)
    text, entry, inp = _make_chat_tab(tab, "输入私聊消息,回车发送")

    tk.Button(inp, text="发送文件", command=lambda: send_file(name),
              bg="#eef1f6", fg=TEXT, relief="flat", padx=10, pady=5,
              font=FONT_MAIN, activebackground="#e2e7ef", cursor="hand2")\
        .grid(row=0, column=1, pady=6, padx=(0, 6))
    tk.Button(inp, text="发送", command=lambda: send_private(name, entry),
              bg=PRIMARY, fg="white", relief="flat", padx=18, pady=5,
              font=FONT_MAIN, activebackground=PRIMARY_DARK, cursor="hand2")\
        .grid(row=0, column=2, pady=6, padx=(0, 8))

    entry.bind("<Return>", lambda e: send_private(name, entry))
    private_tabs[name] = {"tab": tab, "text": text, "input": entry}
    return private_tabs[name]


def open_private_chat(name):
    if not name or name == my_name:
        return
    p = get_or_create_private_tab(name)
    notebook.select(p["tab"])
    p["input"].focus_set()
    mark_private_unread(name, False)


def mark_private_unread(name, unread=True):
    p = private_tabs.get(name)
    if not p:
        return
    try:
        idx = notebook.index(p["tab"])
    except tk.TclError:
        return
    notebook.tab(idx, text=("● " + name) if unread else name)


def on_tab_changed(_event):
    sel = notebook.select()
    if not sel:
        return
    for name, p in private_tabs.items():
        if str(p["tab"]) == sel:
            mark_private_unread(name, False)
            break


def send_private(name, entry):
    if not is_connected or client_socket is None:
        messagebox.showwarning("提示", "未连接服务器")
        return
    msg = entry.get().strip()
    if not msg:
        return
    try:
        client_socket.sendall(f"PRIVATE|{my_name}|{name}|{msg}\n".encode("utf-8"))
        _bubble(private_tabs[name]["text"], my_name, msg, is_me=True, chat_bg=CARD)
        entry.delete(0, tk.END)
    except Exception as e:
        messagebox.showerror("发送失败", f"网络错误:\n{e}")


def receive_private(sender, msg):
    p = get_or_create_private_tab(sender)
    _bubble(p["text"], sender, msg, is_me=False, chat_bg=CARD)
    root.bell()
    if notebook.select() != str(p["tab"]):
        mark_private_unread(sender, True)


# ---------- 字体缩放 ----------
def set_font_size(size):
    global current_font_size, FONT_MAIN, FONT_SMALL, FONT_TITLE
    size = int(size)
    size = max(8, min(24, size))
    if size == current_font_size:
        return
    current_font_size = size
    FONT_MAIN = ("微软雅黑", size)
    FONT_SMALL = ("微软雅黑", max(8, size - 1))
    FONT_TITLE = ("微软雅黑", size + 2, "bold")

    for w in (chat, input_text, user_list):
        try:
            w.config(font=FONT_MAIN)
        except Exception:
            pass
    for lbl in _bubble_labels:
        try:
            lbl.config(font=FONT_MAIN)
        except Exception:
            pass
    for lbl in _meta_labels:
        try:
            lbl.config(font=FONT_SMALL)
        except Exception:
            pass
    for p in private_tabs.values():
        try:
            p["text"].config(font=FONT_MAIN)
        except Exception:
            pass
        try:
            p["input"].config(font=FONT_MAIN)
        except Exception:
            pass
    try:
        style.configure("TNotebook.Tab", font=FONT_MAIN)
    except Exception:
        pass


def on_mousewheel(event):
    if not (event.state & 0x0004):  # Ctrl 键
        return
    set_font_size(current_font_size + (1 if event.delta > 0 else -1))
    return "break"


# ---------- 带占位提示的输入框 ----------
class PlaceholderEntry(tk.Entry):
    def __init__(self, master, placeholder="", **kwargs):
        super().__init__(master, **kwargs)
        self.placeholder = placeholder
        self._ph_color = "#a0a8b8"
        self._normal_color = kwargs.get("fg", TEXT)
        self._showing = False
        self.show_placeholder()
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)

    def show_placeholder(self):
        self.delete(0, tk.END)
        self.insert(0, self.placeholder)
        self.config(fg=self._ph_color)
        self._showing = True

    def clear_placeholder(self):
        if self._showing:
            self.delete(0, tk.END)
            self.config(fg=self._normal_color)
            self._showing = False

    def _on_focus_in(self, _event):
        self.clear_placeholder()

    def _on_focus_out(self, _event):
        if not self.get_real().strip():
            self.show_placeholder()

    def get_real(self):
        return super().get()

    def get(self):
        if self._showing:
            return ""
        return super().get()


# ================= 界面 =================
root = tk.Tk()
root.title("网络聊天室 - 客户端")
root.geometry("800x620")
root.minsize(700, 520)
root.configure(bg=BG)

style = ttk.Style(root)
try:
    style.theme_use("clam")
except tk.TclError:
    pass
style.configure("TNotebook", background=BG, borderwidth=0)
style.configure("TNotebook.Tab", padding=(16, 7), font=FONT_MAIN)
style.map("TNotebook.Tab",
          background=[("selected", CARD)],
          foreground=[("selected", PRIMARY)])


def _card(parent, **kw):
    f = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    f.pack(**kw)
    return f


# ---- 底部:状态栏 ----
status_bar = tk.Frame(root, bg="#e7eaf0")
status_bar.pack(side="bottom", fill="x")

online_label = tk.Label(status_bar, text="在线 0 人", font=FONT_SMALL, bg="#e7eaf0", fg=SUB)
online_label.pack(side="left", padx=16, pady=5)
me_label = tk.Label(status_bar, text="当前用户:—", font=FONT_SMALL, bg="#e7eaf0", fg=SUB)
me_label.pack(side="right", padx=16, pady=5)

# ---- 顶部:标题栏 ----
header = tk.Frame(root, bg=HEADER_BG)
header.pack(side="top", fill="x")

status_dot = tk.Label(header, text="●", font=("微软雅黑", 12), fg="#5a6478", bg=HEADER_BG)
status_dot.pack(side="left", padx=(16, 4), pady=10)
tk.Label(header, text="网络聊天室", font=FONT_TITLE, bg=HEADER_BG, fg=HEADER_FG).pack(side="left")
status_text = tk.Label(header, text="未连接", font=FONT_SMALL, bg=HEADER_BG, fg="#9aa3b8")
status_text.pack(side="left", padx=10)

disconnect_btn = tk.Button(header, text="断开连接", command=disconnect, state="disabled",
                           bg=HEADER_BG, fg="#ffffff", relief="flat", padx=12, pady=4,
                           font=FONT_SMALL, activebackground="#3a4256",
                           highlightbackground="#4a5268", highlightthickness=1, cursor="hand2")
disconnect_btn.pack(side="right", padx=16, pady=8)

# ---- 连接配置区 ----
config = _card(root, side="top", fill="x", padx=16, pady=(10, 0))


def _label(parent, text):
    tk.Label(parent, text=text, font=FONT_SMALL, bg=CARD, fg=SUB).pack(side="left", padx=(14, 0))


def _entry(parent, width, text=""):
    e = tk.Entry(parent, width=width, font=FONT_MAIN, fg=TEXT, bg="#f6f8fb",
                 relief="solid", bd=1, highlightthickness=0)
    e.pack(side="left", padx=(6, 14), pady=10, ipady=3)
    if text:
        e.insert(0, text)
    return e


_label(config, "昵称")
name_entry = _entry(config, 12)

_label(config, "服务器")
ip_entry = _entry(config, 15, "127.0.0.1")

_label(config, "端口")
port_entry = _entry(config, 6, "8888")

connect_btn = tk.Button(config, text="连接", command=connect_server,
                        bg=PRIMARY, fg="white", relief="flat", padx=22, pady=5,
                        font=FONT_MAIN, activebackground=PRIMARY_DARK, cursor="hand2")
connect_btn.pack(side="left", pady=10)

# ---- 中部:标签页(群聊 + 私聊)+ 在线用户 ----
main = tk.Frame(root, bg=BG)
main.pack(side="top", fill="both", expand=True, padx=16, pady=10)

notebook = ttk.Notebook(main)
notebook.pack(side="left", fill="both", expand=True)
notebook.bind("<<NotebookTabChanged>>", on_tab_changed)


def _make_chat_tab(parent, placeholder):
    """构建一个聊天标签页:聊天区(grid row0,可伸缩)+ 输入区(grid row1,固定)。"""
    parent.grid_rowconfigure(0, weight=1)
    parent.grid_columnconfigure(0, weight=1)

    text = scrolledtext.ScrolledText(parent, wrap="word", font=FONT_MAIN, state="disabled",
                                     relief="flat", bg=CARD, fg=TEXT, padx=12, pady=10,
                                     highlightthickness=0, cursor="arrow", height=10)
    text.grid(row=0, column=0, sticky="nsew")
    _configure_tags(text)

    inp = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    inp.grid(row=1, column=0, sticky="ew", pady=(8, 0))
    inp.grid_columnconfigure(0, weight=1)

    entry = PlaceholderEntry(inp, placeholder=placeholder, font=FONT_MAIN,
                             fg=TEXT, bg=CARD, relief="flat", highlightthickness=0, bd=0)
    entry.grid(row=0, column=0, sticky="ew", ipady=6, padx=(10, 8), pady=6)

    return text, entry, inp


# 群聊标签页
group_tab = tk.Frame(notebook, bg=BG)
notebook.add(group_tab, text="群聊")
chat, input_text, group_inp = _make_chat_tab(group_tab, "输入消息,回车发送 · @用户名 消息 私聊")

send_file_btn = tk.Button(group_inp, text="发送文件", command=lambda: send_file("ALL"),
                          bg="#eef1f6", fg=TEXT, relief="flat", padx=12, pady=5,
                          font=FONT_MAIN, activebackground="#e2e7ef", cursor="hand2")
send_file_btn.grid(row=0, column=1, pady=6, padx=(0, 6))

send_btn = tk.Button(group_inp, text="发送", command=send_message, state="disabled",
                     bg=PRIMARY, fg="white", relief="flat", padx=24, pady=5,
                     font=FONT_MAIN, activebackground=PRIMARY_DARK, cursor="hand2")
send_btn.grid(row=0, column=2, pady=6, padx=(0, 8))

input_text.bind("<Return>", send_message)

# 在线用户卡片
user_card = tk.Frame(main, bg=CARD, width=170, highlightbackground=BORDER, highlightthickness=1)
user_card.pack(side="right", fill="y", padx=(12, 0))
user_card.pack_propagate(False)

tk.Label(user_card, text="在线用户 · 点击私聊", font=FONT_SMALL, bg=CARD, fg=SUB)\
    .pack(anchor="w", padx=14, pady=(12, 4))
user_list = tk.Listbox(user_card, font=FONT_MAIN, relief="flat", bg=CARD, fg=TEXT,
                       highlightthickness=0, selectbackground=PRIMARY, selectforeground="white",
                       activestyle="none")
user_list.pack(fill="both", expand=True, padx=10, pady=(0, 12))
user_list.bind("<<ListboxSelect>>", on_user_click)

root.protocol("WM_DELETE_WINDOW", on_close)

# 字体缩放:Ctrl+滚轮 / Ctrl+= / Ctrl+-
root.bind_all("<MouseWheel>", on_mousewheel)
root.bind_all("<Control-equal>", lambda e: set_font_size(current_font_size + 1))
root.bind_all("<Control-plus>", lambda e: set_font_size(current_font_size + 1))
root.bind_all("<Control-minus>", lambda e: set_font_size(current_font_size - 1))

append_message("[系统] 欢迎!输入昵称后点击「连接」进入聊天室。", "sys")
append_message("[系统] 私聊:点击右侧名单或输入「@用户名 消息」;Ctrl+滚轮 调节字号。", "sys")

root.mainloop()

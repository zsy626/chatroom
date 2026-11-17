# -*- coding: utf-8 -*-
"""
网络聊天室 - 服务端(支持文件转发版)

在原服务端基础上新增:
1. FILE / FILEDATA 消息转发(文件传输)
2. recv 缓冲区加大到 8192,适应文件分片
3. SO_REUSEADDR,重启不用等端口释放

协议(全部以 | 分隔、\\n 结尾):
  LOGIN|昵称
  GROUP|昵称|消息
  PRIVATE|发送者|接收者|消息
  FILE|发送者|接收者|文件名|分片总数|md5          (接收者可为 ALL 表示群发)
  FILEDATA|发送者|接收者|md5|分片序号|base64数据
  服务端下发:
  USERS|昵称1|昵称2|...
  ERROR|提示
  NOTICE|提示
"""
import socket
import threading

HOST = "0.0.0.0"
PORT = 8888

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((HOST, PORT))
server.listen(5)

clients = []
clients_lock = threading.Lock()
users = {}


def send_user_list():
    with clients_lock:
        names = list(users.keys())
        targets = list(users.values())
    message = "USERS|" + "|".join(names) + "\n"
    for c in set(targets):
        try:
            c.sendall(message.encode("utf-8"))
        except OSError:
            pass


def broadcast(message, sender):
    """发给除 sender 之外的所有人。"""
    with clients_lock:
        targets = clients.copy()
    for c in targets:
        if c == sender:
            continue
        try:
            c.sendall(message.encode("utf-8"))
        except OSError:
            pass


def send_to_user(name, message):
    """发给指定用户,成功返回 True。"""
    with clients_lock:
        target = users.get(name)
    if target:
        try:
            target.sendall(message.encode("utf-8"))
            return True
        except OSError:
            pass
    return False


def handle_client(client, address):
    print(f"用户{address}已连接")
    with clients_lock:
        clients.append(client)
    logged_in_user = None
    buffer = b""
    while True:
        try:
            data = client.recv(8192)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                parts = text.split("|", 3)
                head = parts[0]

                if head == "LOGIN" and len(parts) == 2:
                    username = parts[1]
                    if logged_in_user is not None:
                        client.sendall("ERROR|请勿重复登录\n".encode("utf-8"))
                        continue
                    with clients_lock:
                        if username in users:
                            duplicate = True
                        else:
                            users[username] = client
                            duplicate = False
                    if duplicate:
                        client.sendall("ERROR|用户名已被占用\n".encode("utf-8"))
                        raise ConnectionError("用户名重复")
                    logged_in_user = username
                    print(f"{username} 已上线!")
                    send_user_list()

                elif head == "GROUP" and len(parts) == 3:
                    username = parts[1]
                    message = parts[2]
                    if logged_in_user != username:
                        client.sendall("ERROR|用户名不匹配\n".encode("utf-8"))
                        continue
                    print(f"{username}: {message}")
                    broadcast(text + "\n", client)

                elif head == "PRIVATE" and len(parts) == 4:
                    sender = parts[1]
                    receiver = parts[2]
                    message = parts[3]
                    if logged_in_user != sender:
                        client.sendall("ERROR|用户名不匹配\n".encode("utf-8"))
                        continue
                    if not send_to_user(receiver, f"PRIVATE|{sender}|{message}\n"):
                        client.sendall(f"ERROR|用户{receiver}不在线\n".encode("utf-8"))

                elif head in ("FILE", "FILEDATA"):
                    # FILE|sender|receiver|filename|total|md5
                    # FILEDATA|sender|receiver|md5|seq|base64data
                    p = text.split("|", 3)
                    if len(p) < 3:
                        continue
                    sender, receiver = p[1], p[2]
                    if logged_in_user != sender:
                        client.sendall("ERROR|用户名不匹配\n".encode("utf-8"))
                        continue
                    if receiver == "ALL":
                        broadcast(text + "\n", client)
                    else:
                        if not send_to_user(receiver, text + "\n"):
                            client.sendall(f"ERROR|用户{receiver}不在线\n".encode("utf-8"))

        except (ConnectionError, OSError):
            break

    with clients_lock:
        if client in clients:
            clients.remove(client)
        for name, conn in list(users.items()):
            if conn is client:
                del users[name]
    client.close()
    print(f"用户{address}已断开")

    if logged_in_user:
        broadcast(f"NOTICE|{logged_in_user}已下线\n", client)

    send_user_list()


print(f"服务器启动成功,监听 {PORT} 端口,等待客户端连接...")

while True:
    client, address = server.accept()
    threading.Thread(target=handle_client, args=(client, address), daemon=True).start()

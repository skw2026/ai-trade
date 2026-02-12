#!/usr/bin/env python3
"""
AI-Trade 外部看门狗 (Watchdog)

功能：
1. 检查 Docker 容器是否处于 Running 状态。
2. 检查 runtime.log 中的 RUNTIME_STATUS 心跳是否超时。
3. 异常时通过 Webhook 发送告警。

使用：
  export AI_TRADE_WEBHOOK_URL="https://hooks.slack.com/..."
  python3 ops/watchdog.py
"""

import datetime
import json
import socket
import os
import sys
import time
import urllib.request
import urllib.error

# 配置
CONTAINER_NAME = "ai-trade"
# 日志路径需与 docker-compose 挂载或输出路径一致
LOG_PATH = "./data/reports/closed_loop/latest/runtime.log"
# 心跳超时阈值（秒），应大于 system.status_log_interval_ticks * tick_interval
HEARTBEAT_THRESHOLD_SEC = 120
WEBHOOK_URL = os.getenv("AI_TRADE_WEBHOOK_URL")


def send_alert(message: str) -> None:
    print(f"[ALERT] {message}")
    if not WEBHOOK_URL:
        return
    try:
        payload = {"text": f"🚨 **AI-Trade Watchdog** 🚨\n\n{message}"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as res:
            if res.status >= 400:
                print(f"[ERROR] Webhook failed with status: {res.status}")
    except Exception as e:
        print(f"[ERROR] Failed to send webhook: {e}")


def check_container() -> tuple[bool, str]:
    """通过 Unix Socket 直接查询 Docker API，无需 docker 客户端"""
    socket_path = "/var/run/docker.sock"
    if not os.path.exists(socket_path):
        return False, f"Docker socket not found at {socket_path}"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(socket_path)
            # Docker Engine API: GET /containers/{name}/json
            request = f"GET /containers/{CONTAINER_NAME}/json HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            sock.sendall(request.encode())

            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            # 分离 HTTP 头和体
            header, body = response.split(b"\r\n\r\n", 1)
            if b"200 OK" not in header.split(b"\r\n")[0]:
                return False, f"Container not found or API error"

            info = json.loads(body.decode("utf-8", errors="ignore"))
            state = info.get("State", {})
            if state.get("Running"):
                return True, "OK"
            return False, f"State: {state.get('Status', 'unknown')}"
    except Exception as e:
        return False, str(e)


def parse_log_time(line: str) -> datetime.datetime | None:
    # 格式示例: 2026-02-12 12:34:56 [INFO] ...
    try:
        parts = line.split()
        if len(parts) < 2:
            return None
        ts_str = f"{parts[0]} {parts[1]}"
        return datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def check_logs() -> tuple[bool, str]:
    if not os.path.exists(LOG_PATH):
        return False, f"Log file not found: {LOG_PATH}"

    last_heartbeat = None
    try:
        # 读取文件末尾 10KB
        file_size = os.path.getsize(LOG_PATH)
        read_size = min(file_size, 1024 * 10)

        with open(LOG_PATH, "rb") as f:
            if file_size > read_size:
                f.seek(-read_size, 2)
            # 忽略解码错误，只关注最后几行
            lines = f.read().decode("utf-8", errors="ignore").splitlines()

        for line in reversed(lines):
            if "RUNTIME_STATUS" in line:
                ts = parse_log_time(line)
                if ts:
                    last_heartbeat = ts
                    break
    except Exception as e:
        return False, f"Error reading log: {e}"

    if not last_heartbeat:
        return False, "No RUNTIME_STATUS found in recent logs"

    # 假设日志时间为本地时间，与系统时间一致
    now = datetime.datetime.now()
    delta = (now - last_heartbeat).total_seconds()

    if delta > HEARTBEAT_THRESHOLD_SEC:
        return (
            False,
            f"Heartbeat delayed by {int(delta)}s (Threshold: {HEARTBEAT_THRESHOLD_SEC}s)",
        )

    return True, f"OK (Last: {last_heartbeat}, Delta: {int(delta)}s)"


def main() -> int:
    print(f"[Watchdog] Checking {CONTAINER_NAME}...")
    if not WEBHOOK_URL:
        print("[Watchdog] Webhook not configured. Alerts will only be logged to stdout.")

    # 1. 检查容器状态
    ok, msg = check_container()
    if not ok:
        send_alert(f"Container Status: {msg}")
        return 1

    # 2. 检查日志心跳
    ok, msg = check_logs()
    if not ok:
        send_alert(f"Log Heartbeat: {msg}")
        return 1

    print(f"[Watchdog] All systems operational. {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
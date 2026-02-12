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


def get_docker_logs(tail: int = 50) -> str:
    """通过 Unix Socket 获取容器标准输出日志"""
    socket_path = "/var/run/docker.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(socket_path)
            # Docker Engine API: GET /containers/{name}/logs
            # params: stdout=1, stderr=1, tail=N
            query = f"stdout=1&stderr=1&tail={tail}"
            request = f"GET /containers/{CONTAINER_NAME}/logs?{query} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            sock.sendall(request.encode())

            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            # 分离 HTTP 头和体
            parts = response.split(b"\r\n\r\n", 1)
            if len(parts) < 2:
                return ""
            # 忽略 Docker 流格式头 (8 bytes)，直接作为文本解码尝试查找关键词
            return parts[1].decode("utf-8", errors="ignore")
    except Exception:
        return ""

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
    last_heartbeat = None
    try:
        # 直接从 Docker 获取最近日志
        lines = get_docker_logs(tail=100).splitlines()

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
    # [加固] 顶层异常捕获，防止看门狗进程崩溃退出
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[Watchdog] CRITICAL ERROR: {e}")
        # 返回 0 让 shell 循环继续，或者返回 1 让 Docker 重启（取决于 entrypoint 策略）
        # 这里配合 docker-compose 的 || true 策略，我们打印错误即可
        sys.exit(1)
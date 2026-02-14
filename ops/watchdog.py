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

from __future__ import annotations

import datetime
import json
import socket
import os
import sys
import urllib.request
from typing import Dict, Tuple

# 配置
CONTAINER_NAME = "ai-trade"
# 心跳超时阈值（秒），应大于 system.status_log_interval_ticks * tick_interval
HEARTBEAT_THRESHOLD_SEC = 120
WEBHOOK_URL = os.getenv("AI_TRADE_WEBHOOK_URL")


def decode_chunked_body(body: bytes) -> bytes:
    decoded = bytearray()
    idx = 0
    while True:
        line_end = body.find(b"\r\n", idx)
        if line_end < 0:
            return bytes(decoded) if decoded else body
        size_hex = body[idx:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_hex, 16)
        except ValueError:
            return bytes(decoded) if decoded else body
        idx = line_end + 2
        if size == 0:
            return bytes(decoded)
        if idx + size > len(body):
            return bytes(decoded) if decoded else body
        decoded.extend(body[idx : idx + size])
        idx += size
        if body[idx : idx + 2] == b"\r\n":
            idx += 2


def decode_docker_log_stream(body: bytes) -> str:
    if not body:
        return ""
    idx = 0
    decoded = bytearray()
    parsed_any = False
    while idx + 8 <= len(body):
        stream_type = body[idx]
        if stream_type not in (0, 1, 2):
            break
        if body[idx + 1 : idx + 4] != b"\x00\x00\x00":
            break
        frame_size = int.from_bytes(body[idx + 4 : idx + 8], byteorder="big")
        idx += 8
        if idx + frame_size > len(body):
            break
        decoded.extend(body[idx : idx + frame_size])
        idx += frame_size
        parsed_any = True
    if parsed_any and idx == len(body):
        return decoded.decode("utf-8", errors="ignore")
    return body.decode("utf-8", errors="ignore")


def docker_http_get(path: str) -> Tuple[str, Dict[str, str], bytes]:
    socket_path = "/var/run/docker.sock"
    if not os.path.exists(socket_path):
        raise RuntimeError(f"Docker socket not found at {socket_path}")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        sock.sendall(request.encode())

        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

    parts = response.split(b"\r\n\r\n", 1)
    if len(parts) != 2:
        raise RuntimeError("Invalid Docker API response")
    header_blob, body = parts
    header_lines = header_blob.split(b"\r\n")
    if not header_lines:
        raise RuntimeError("Missing Docker API status line")
    status_line = header_lines[0].decode("utf-8", errors="ignore")

    headers: Dict[str, str] = {}
    for line in header_lines[1:]:
        if b":" not in line:
            continue
        key_raw, value_raw = line.split(b":", 1)
        key = key_raw.decode("utf-8", errors="ignore").strip().lower()
        value = value_raw.decode("utf-8", errors="ignore").strip()
        headers[key] = value

    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = decode_chunked_body(body)

    return status_line, headers, body


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
    try:
        status_line, _, body = docker_http_get(f"/containers/{CONTAINER_NAME}/json")
        if " 200 " not in status_line:
            return False, "Container not found or API error"
        info = json.loads(body.decode("utf-8", errors="ignore"))
        state = info.get("State", {})
        if state.get("Running"):
            return True, "OK"
        return False, f"State: {state.get('Status', 'unknown')}"
    except Exception as e:
        return False, str(e)


def get_docker_logs(tail: int = 50) -> str:
    """通过 Unix Socket 获取容器标准输出日志"""
    try:
        query = f"stdout=1&stderr=1&tail={tail}"
        status_line, _, body = docker_http_get(f"/containers/{CONTAINER_NAME}/logs?{query}")
        if " 200 " not in status_line:
            return ""
        return decode_docker_log_stream(body)
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

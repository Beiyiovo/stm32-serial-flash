# -*- coding: utf-8 -*-
"""串口监视器 —— 烧完直接看打印，不用再开串口助手。

它和 flash.py 用的是同一根 USB-TTL：
烧录时占用 COM 口，烧完关掉烧录窗口，立刻就能在这里看板子的串口输出。

用法：
    monitor.bat                        自动找 USB-TTL，115200
    monitor.bat --port COM7 --baud 9600
    monitor.bat --log run.log          同时写文件（掉线重连也不覆盖）
    monitor.bat --list                 只列出串口
"""
import argparse
import os
import sys
import time
from datetime import datetime

import serial
from serial.tools import list_ports

try:
    from flash import find_port, USB_TTL_HINTS
except ImportError:                      # 单独拷走 monitor.py 时也能跑
    find_port, USB_TTL_HINTS = None, ("CH340", "CH341", "CP210", "FT232")


def show_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("  没找到任何串口。板子插上了吗？USB-TTL 的驱动装了吗？")
        return False
    print("  可用串口：\n")
    for p in ports:
        desc = p.description or ""
        mark = "  ← 看着像 USB-TTL" if any(k in (desc + (p.manufacturer or "")).upper()
                                        for k in USB_TTL_HINTS) else ""
        print(f"    {p.device:<8} {desc}{mark}")
    print()
    return True


def pick_port(explicit):
    if explicit:
        return explicit
    if find_port:
        p = find_port()
        if p:
            return p
    ports = list(list_ports.comports())
    if len(ports) == 1:
        return ports[0].device
    return None


def main():
    ap = argparse.ArgumentParser(description="串口监视器（和 flash.py 共用同一根 USB-TTL）")
    ap.add_argument("--port", help="串口号，如 COM7；不给就自动找 USB-TTL")
    ap.add_argument("--baud", type=int, default=115200, help="波特率，默认 115200")
    ap.add_argument("--log", help="把输出同时写进这个文件")
    ap.add_argument("--list", action="store_true", help="只列出串口，不打开")
    ap.add_argument("--no-reconnect", action="store_true", help="掉线就退出，不自动重连")
    args = ap.parse_args()

    if args.list:
        show_ports()
        return 0

    port = pick_port(args.port)
    if not port:
        print("\n  找到多个串口，但认不出哪个是 USB-TTL。用 --list 看一眼，再 --port COMx 指定。\n")
        show_ports()
        return 2

    known = {p.device.upper() for p in list_ports.comports()}
    if port.upper() not in known:
        print(f"\n  [X] 系统里没有 {port}。板子插上了吗？用 --list 看看现在有哪些串口。\n")
        show_ports()
        return 2

    logf = open(args.log, "a", encoding="utf-8") if args.log else None
    total = 0
    try:
        while True:
            try:
                ser = serial.Serial(port, args.baud, timeout=0.2)
            except serial.SerialException as e:
                print(f"\n  [X] 打不开 {port}：{e}")
                print("      串口被占用是最常见的原因——烧录窗口、串口助手还开着吗？\n")
                return 2

            print(f"\n  已连接 {port} @ {args.baud}    （Ctrl+C 退出）")
            if logf:
                print(f"  日志同时写入：{os.path.abspath(args.log)}")
            print("  " + "-" * 52)

            buf = b""
            try:
                while True:
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    total += len(chunk)
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        text = raw.decode("utf-8", "replace").rstrip("\r")
                        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        out = f"[{stamp}] {text}"
                        print(out)
                        if logf:
                            logf.write(out + "\n")
                            logf.flush()
            except serial.SerialException:
                print("\n  [!] 串口断了（板子复位 / 线松了？）")
            except KeyboardInterrupt:
                raise
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
            if args.no_reconnect:
                break
            print("  1 秒后重连……")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"\n\n  已退出，共收到 {total} 字节。")
    finally:
        if logf:
            logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

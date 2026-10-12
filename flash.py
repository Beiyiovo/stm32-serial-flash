# -*- coding: utf-8 -*-
"""
STM32 串口 ISP 一键烧录工具
============================

把「编译 → 转 bin → 找串口 → 握手 → 烧录」压成一次运行。
专门针对**面包板 + 长杜邦线**这种不稳定环境做了优化。

用法：
    flash.py                        在当前目录及子目录里找 .uvprojx，列出来让你选
    flash.py <工程目录>              直接烧这个工程（会先编译）
    flash.py <xxx.uvprojx>          同上
    flash.py --bin <xxx.bin>        不编译，直接烧现成的 bin

常用选项：
    --port COM3      指定串口（默认自动找 CH340 / CP210x / FT232）
    --baud 115200    只用这个波特率（默认自动扫描）
    --no-build       跳过编译，直接用上次生成的 .axf/.bin
    -y               跳过"按回车开始"的确认

核心是那个握手函数 try_sync_fast()：STM32 的 bootloader 只要收到一个完整的
0x7F 就会回 0x79，所以根本不用"发一个等一个"——连发一批再一次性读，避开每次
读超时的等待，握手速度翻倍。这正是不稳定线路上能不能烧进去的关键。
"""

import argparse
import glob
import os
import subprocess
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("缺少 pyserial。先装依赖：  pip install -r requirements.txt")
    sys.exit(1)

LINE = "=" * 64

# 握手扫描顺序。作者实测（面包板 + 20cm 杜邦线）：
#   38400 烧 27KB 约 7 秒；19200 约 15 秒；9600 约 30 秒；115200 从未成功过。
# 所以不是"越高越好"，而是"能握手的最快那个"。
PLAN = [
    (38400, 100),
    (19200, 80),
    (9600, 60),
]

# 烧录阶段若失败，按这个顺序降速重试
FALLBACK_BAUDS = [38400, 19200, 9600, 4800]

USB_TTL_HINTS = ("CH340", "CH341", "CP210", "FT232", "USB-SERIAL", "USB SERIAL", "USB 串行")


# ============================================================
#  环境探测：Keil 装在谁家都不一样，所以按 环境变量 → 注册表 → 常见目录 找
# ============================================================
def _keil_reg_paths():
    """
    Keil 注册表里 Products\\MDK 的 Path。
    坑：这个值通常指向 <Keil根>\\ARM，不是 Keil 根目录本身，
    所以下面既把它当根试，也把它的父目录当根试。
    """
    out = []
    try:
        import winreg
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Keil\Products\MDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Keil\Products\MDK"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Keil\Products\MDK"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p, _ = winreg.QueryValueEx(k, "Path")
                    if p:
                        out.append(p.rstrip("\\/"))
            except OSError:
                continue
    except ImportError:
        pass
    return out


def _candidate_roots():
    roots = []
    for env in ("KEIL_ROOT", "KEIL_MDK_ROOT"):
        v = os.environ.get(env)
        if v and os.path.isdir(v):
            roots.append(v.rstrip("\\/"))
    for p in _keil_reg_paths():
        roots.append(p)
        parent = os.path.dirname(p)
        if parent:
            roots.append(parent)
    roots += [r"C:\Keil_v5", r"C:\Keil", r"D:\Keil_v5", r"D:\Keil", r"E:\Keil_v5"]
    seen, out = set(), []
    for r in roots:
        if r and r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def find_uv4():
    v = os.environ.get("KEIL_UV4")
    if v and os.path.exists(v):
        return v
    for root in _candidate_roots():
        for rel in (("UV4", "UV4.exe"), ("UV4", "uv4.exe")):
            p = os.path.join(root, *rel)
            if os.path.exists(p):
                return p
    return None


def find_fromelf():
    v = os.environ.get("KEIL_FROMELF")
    if v and os.path.exists(v):
        return v
    for root in _candidate_roots():
        for rel in (("ARMCLANG", "bin", "fromelf.exe"),
                    ("ARM", "ARMCLANG", "bin", "fromelf.exe"),
                    ("ARM", "ARMCC", "bin", "fromelf.exe")):
            p = os.path.join(root, *rel)
            if os.path.exists(p):
                return p
    return None


# ============================================================
#  串口
# ============================================================
def find_port():
    for p in serial.tools.list_ports.comports():
        d = ((p.description or "") + (p.manufacturer or "")).upper()
        if any(k in d for k in USB_TTL_HINTS):
            return p.device
    return None


def try_sync_fast(port, baud, max_tries=80, show=True):
    """
    ★★★ 核心：保持串口打开 + 批量轰炸 0x7F。

    原理：STM32 的 bootloader 只要收到一个完整的 0x7F 字节就会回 0x79。
    所以不用"发一个等一个"——可以连发一批，再一次性读，
    这样避开了每次读超时的等待，速度翻倍。

    返回 (是否成功, 发了几批)
    """
    BATCH = 8
    BATCH_DELAY = 0.01
    READ_WAIT = 0.04

    try:
        s = serial.Serial(port, baud, timeout=0.05)
        time.sleep(0.06)
        s.reset_input_buffer()

        n_batch = max(1, max_tries // BATCH)

        for b in range(n_batch):
            for _ in range(BATCH):
                s.write(b"\x7f")
                time.sleep(BATCH_DELAY)

            time.sleep(READ_WAIT)
            r = s.read(64)

            if r and 0x79 in r:
                s.close()
                if show:
                    print("  ★★★ 第 %d 批就成功了！（波特率 %d）" % (b + 1, baud))
                return True, b + 1

            if show and ((b + 1) % 10 == 0):
                sys.stdout.write("      已发 %d 批...\r\n" % (b + 1))
                sys.stdout.flush()

        s.close()
        return False, n_batch
    except Exception as e:
        if show:
            print("      （串口出错：%s）" % str(e)[:50])
        return False, 0


def check_port_free(port):
    """烧录失败最常见的原因是串口被别的程序占着，但原始报错完全看不出来。"""
    for try_n in range(1, 4):
        try:
            t = serial.Serial(port, 9600, timeout=0.1)
            t.close()
            return True
        except Exception:
            if try_n < 3:
                time.sleep(1.0)
    return False


# ============================================================
#  找工程
# ============================================================
def find_projects(root):
    return sorted(
        (os.path.basename(os.path.dirname(f)), f)
        for f in glob.glob(os.path.join(root, "**", "*.uvprojx"), recursive=True)
    )


def pick_project(target):
    if target:
        if os.path.isfile(target) and target.lower().endswith(".uvprojx"):
            return os.path.dirname(os.path.abspath(target)), os.path.abspath(target)
        if os.path.isdir(target):
            projs = find_projects(target)
            if len(projs) == 1:
                return os.path.dirname(projs[0][1]), projs[0][1]
            if not projs:
                print("  X 这个目录里没找到 .uvprojx：%s" % target)
                sys.exit(1)
            return choose(projs)
        print("  X 路径不存在：%s" % target)
        sys.exit(1)

    projs = find_projects(os.getcwd())
    if not projs:
        print("  X 当前目录及子目录里没找到 .uvprojx")
        print("     可以显式指定：  flash.py D:\\path\\to\\project")
        sys.exit(1)
    return choose(projs)


def choose(projs):
    print("  找到这些工程：")
    print()
    for i, (name, path) in enumerate(projs, start=1):
        print("    [%d]  %s" % (i, name))
    print()
    sel = input("  选哪个？(输入 1~%d，直接回车 = 1): " % len(projs)).strip()
    idx = int(sel) - 1 if (sel.isdigit() and 1 <= int(sel) <= len(projs)) else 0
    name, path = projs[idx]
    return os.path.dirname(path), path


# ============================================================
#  编译 + 转 bin
# ============================================================
def build(projdir, projfile):
    uv4 = find_uv4()
    if not uv4:
        print("  X 找不到 Keil 的 UV4.exe")
        print("     装 Keil MDK 后重试，或指定：  set KEIL_UV4=D:\\Keil_v5\\UV4\\UV4.exe")
        print("     也可以加 --no-build 跳过编译（直接烧上次的产物）")
        sys.exit(1)

    print(LINE)
    print("  [1/4] 编译")
    print(LINE)
    logf = os.path.join(projdir, "_build_log.txt")
    subprocess.run([uv4, "-b", os.path.basename(projfile), "-j0", "-o", os.path.basename(logf)],
                   cwd=projdir, capture_output=True, text=True, errors="replace")

    if os.path.exists(logf):
        txt = open(logf, encoding="utf-8", errors="replace").read()
        os.remove(logf)
        for ln in txt.splitlines():
            if "Program Size" in ln or "Error(s)" in ln:
                print("    " + ln.strip())
        if "0 Error(s)" not in txt:
            print()
            print("  X 编译有错误，先在 Keil 里改好")
            sys.exit(1)
    return True


def make_bin(projdir, projfile):
    print()
    print(LINE)
    print("  [2/4] 生成 .bin")
    print(LINE)
    fromelf = find_fromelf()
    if not fromelf:
        print("    X 找不到 fromelf.exe")
        print("      指定：  set KEIL_FROMELF=D:\\Keil_v5\\ARM\\ARMCLANG\\bin\\fromelf.exe")
        sys.exit(1)

    outname = os.path.splitext(os.path.basename(projfile))[0]
    axf = os.path.join(projdir, "Objects", outname + ".axf")
    binf = os.path.join(projdir, "Objects", outname + ".bin")

    if not os.path.exists(axf):
        print("    X 找不到 %s" % axf)
        print("      先编译一次，或去掉 --no-build")
        sys.exit(1)

    subprocess.run([fromelf, "--bin", "--output", binf, axf],
                   cwd=os.path.dirname(axf), capture_output=True)
    if not os.path.exists(binf):
        print("    X fromelf 转换失败")
        sys.exit(1)
    print("    OK %s (%.1f KB)" % (os.path.basename(binf), os.path.getsize(binf) / 1024))
    return binf


# ============================================================
#  烧录
# ============================================================
def flash(binf, port, only_baud=None, assume_yes=False):
    print()
    print(LINE)
    print("  [3/4] 找串口")
    print(LINE)

    if not port:
        port = find_port()
    if not port:
        print("    X 没找到 USB-TTL 串口（CH340 / CP210x / FT232）")
        print("      -> USB-TTL TXD -> STM32【A10】(USART1_RX)")
        print("         USB-TTL RXD -> STM32【A9】(USART1_TX)")
        print("         USB-TTL GND -> STM32【GND】")
        print("      或用 --port COM3 手动指定")
        sys.exit(1)
    print("    OK %s" % port)

    print()
    print("  ★ 检查串口有没有被别的程序占着...")
    if not check_port_free(port):
        print()
        print(LINE)
        print("  ★★★ 串口被占用了！打不开 %s" % port)
        print(LINE)
        print()
        print("  ★★ 这是烧录失败的常见原因，请检查：")
        print("     1. 串口助手 / 串口监视器还开着吗？ → 关掉它")
        print("     2. 上一次的烧录窗口没关？        → 关掉")
        print("     3. 别的东西占着这个 COM 口？      → 关掉")
        print()
        sys.exit(1)
    print("    OK 串口空闲，可以使用")

    print()
    print(LINE)
    print("  [4/4] 烧录")
    print(LINE)
    print()
    print("  ★★ 请先做好：")
    print("       1. BOOT0 跳线帽拨到【1】那边")
    print("       2. 按一下复位键（RST）")
    print()
    print("  ★ 接下来：")
    print("       屏幕会飞快地试（每 50 毫秒一批），你【看着屏幕按复位键】")
    print("       一旦握上手，立刻自动开始烧录")
    print()
    if not assume_yes:
        input("  做好了按回车，开始...")
    print()

    print(LINE)
    print("  [1/2] 找连接（保持串口打开，连续快速试探）")
    print(LINE)
    print()

    plan = [(only_baud, 100)] if only_baud else PLAN
    good_baud = None
    for baud, tries in plan:
        if good_baud:
            break
        print("  ┌──────────────────────────────────────────┐")
        print("  │  试波特率 %-6d （最多 %3d 次）           │" % (baud, tries))
        print("  └──────────────────────────────────────────┘")
        print("     ★ 请现在开始反复按 STM32 的复位键...")
        print()
        ok, _ = try_sync_fast(port, baud, max_tries=tries)
        if ok:
            good_baud = baud
        else:
            print("     X 这个波特率没成功，换下一个")
            print()

    if not good_baud:
        print()
        print(LINE)
        print("  X 所有波特率都没连上")
        print(LINE)
        print()
        print("  检查：")
        print("    1. BOOT0 是不是在【1】")
        print("    2. 按过复位键吗（★ 边看屏幕边按，不要停）")
        print("    3. USB-TTL 三根线（TXD->A10、RXD->A9、GND->G），试试把 TXD/RXD 对调")
        print("    4. 模块的线全拔掉（只留 USB-TTL）")
        print("    5. 杜邦线换成短的")
        sys.exit(1)

    print()
    print("  ★ 握手成功！可用波特率 = %d" % good_baud)
    print()
    print(LINE)
    print("  [2/2] 开始烧录（★ 别按复位、别碰线）")
    print(LINE)
    print()

    ok, used = False, None
    cands = [good_baud] + [x for x in FALLBACK_BAUDS if x < good_baud]
    for b in cands:
        print("  ---- 试波特率 %d ----" % b)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "stm32loader", "-p", port, "-b", str(b),
                 "-e", "-w", "-v", binf, "-g", "0x08000000"],
                capture_output=True, text=True, errors="replace", timeout=180)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            rc = r.returncode
        except subprocess.TimeoutExpired:
            print("    超时")
            continue

        for ln in out.splitlines():
            s = ln.strip()
            if s and "progress" not in s.lower():
                print("    " + s)

        if rc == 0:
            ok, used = True, b
            break
        print("    X 失败，换下一个波特率")
        print()

    print()
    print(LINE)
    if ok:
        print("  ★★★ 烧录成功！（波特率 %d）" % used)
        print(LINE)
        print()
        print("  ★ 现在做这两步（漏了程序不会跑）：")
        print("       1. BOOT0 跳线帽拨回【0】")
        print("       2. 按一下复位键")
    else:
        print("  X 烧录失败")
        print(LINE)
        print()
        print("  排查：")
        print("    1. BOOT0 是否真的在【1】")
        print("    2. USB-TTL 三根线，试试 TXD/RXD 对调")
        print("    3. 杜邦线换短的")
        print("    4. 模块的线全部拔掉")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description="STM32 串口 ISP 一键烧录（编译 + 转换 + 握手 + 烧录）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", help="工程目录 或 .uvprojx 文件（不填则自动搜索）")
    ap.add_argument("--bin", dest="binfile", help="直接烧这个 bin，不编译")
    ap.add_argument("--port", help="串口，如 COM3（默认自动找 USB-TTL）")
    ap.add_argument("--baud", type=int, help="只用这个波特率（默认自动扫描）")
    ap.add_argument("--no-build", action="store_true", help="跳过编译，用上次的产物")
    ap.add_argument("-y", action="store_true", help="跳过确认，直接开始")
    a = ap.parse_args()

    print()
    print(LINE)
    print("      STM32 串口 ISP 一键烧录工具")
    print(LINE)
    print()

    if a.binfile:
        if not os.path.exists(a.binfile):
            print("  X bin 不存在：%s" % a.binfile)
            sys.exit(1)
        binf = a.binfile
        print("  直接烧 %s" % binf)
    else:
        projdir, projfile = pick_project(a.project)
        print()
        print("  工程：%s" % os.path.basename(projfile))
        if not a.no_build:
            build(projdir, projfile)
        binf = make_bin(projdir, projfile)

    flash(binf, a.port, a.baud, a.y)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  已中断。")
        sys.exit(130)

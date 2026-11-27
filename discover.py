#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
掃描 aaeon 所在網段的 RTSP 攝像頭 (只用標準函式庫, Python 3.6+)
  python3 discover.py                     # 掃描本機所有網段 (每個介面的 /24)
  python3 discover.py 192.0.2.0/24        # 範例網段，請換成實際網段

做法：
  1. ONVIF WS-Discovery 多播探測 (會回應的攝像頭直接列入)
  2. 對整個 /24 的 RTSP port (554) 做 TCP 連線測試
  3. 對有開 port 的主機用 RTSP DESCRIBE 嘗試常見串流路徑，找出可播放的網址
只找攝像頭、不錄影。
"""
import argparse
import concurrent.futures
import ipaddress
import json
import re
import socket
import subprocess
import time
import uuid

RTSP_PORTS = [554, 8554]
# 常見品牌的 RTSP 路徑 (依序嘗試，先成功的為準)
PATHS = [
    "/h264",                                   # 目前這兩台 (nessyMediaServer)
    "/",
    "/stream1", "/stream0", "/live", "/live.sdp", "/live/ch00_0", "/ch0_0.h264",
    "/Streaming/Channels/101",                 # Hikvision
    "/cam/realmonitor?channel=1&subtype=0",    # Dahua
    "/h264Preview_01_main",                    # Reolink
    "/onvif1", "/media/video1", "/video1", "/profile1", "/main", "/11", "/av0_0",
    "/videoMain", "/mpeg4", "/unicast/c1/s0/live",
]
SKIP_IFACES = ("lo", "docker", "l4tbr", "rndis", "usb", "veth", "br-")


def local_networks():
    """回傳本機各介面所在的 /24 網段 (網段較大時只掃自己那段 /24)。"""
    out = subprocess.run(["ip", "-4", "-o", "addr"], stdout=subprocess.PIPE).stdout.decode()
    nets = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1].startswith(SKIP_IFACES):
            continue
        iface = ipaddress.ip_interface(parts[3])
        prefix = max(iface.network.prefixlen, 24)
        net = ipaddress.ip_network("{}/{}".format(iface.ip, prefix), strict=False)
        if net not in [n for n, _ in nets]:
            nets.append((net, str(iface.ip)))
    return nets


def ws_discovery(timeout=2.0):
    """ONVIF WS-Discovery：回傳 {ip: 型號描述}。"""
    msg = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
           'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
           'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
           '<e:Header><w:MessageID>uuid:{}</w:MessageID>'
           '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
           '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
           '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>'
           '</e:Body></e:Envelope>').format(uuid.uuid4()).encode()
    found = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.3)
        s.sendto(msg, ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            txt = data.decode(errors="ignore")
            m = re.search(r"onvif://www\.onvif\.org/(?:hardware|name)/([^\s<]+)", txt)
            found[addr[0]] = urlunquote(m.group(1)) if m else "ONVIF"
        s.close()
    except OSError:
        pass
    return found


def urlunquote(s):
    try:
        from urllib.parse import unquote
        return unquote(s)
    except Exception:
        return s


def port_open(ip, port, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def rtsp_describe(ip, port, path, timeout=1.5):
    """回傳 (狀態碼, headers 文字, SDP 內容)。"""
    req = ("DESCRIBE rtsp://{}:{}{} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
           "User-Agent: cam-discover\r\n\r\n").format(ip, port, path).encode()
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(req)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            head = head.decode(errors="ignore")
            m = re.search(r"Content-Length:\s*(\d+)", head, re.I)
            need = int(m.group(1)) if m else 0
            while len(body) < need:
                chunk = s.recv(4096)
                if not chunk:
                    break
                body += chunk
            code = int(head.split(" ", 2)[1]) if head.startswith("RTSP/") else 0
            return code, head, body.decode(errors="ignore")
    except (OSError, ValueError, IndexError):
        return 0, "", ""


def probe_host(ip, onvif_name=None, stop=None):
    """找出這台主機可用的 RTSP 網址；不是攝像頭回傳 None。"""
    first_open = auth_port = None
    for port in RTSP_PORTS:
        if stop is not None and stop.is_set():
            return None
        if not port_open(ip, port):
            continue
        first_open = first_open or port
        for path in PATHS:
            if stop is not None and stop.is_set():
                return None
            code, head, sdp = rtsp_describe(ip, port, path)
            if code == 200 and "m=video" in sdp:
                server = re.search(r"^s=(.*)$", sdp, re.M)
                codec = re.search(r"a=rtpmap:\d+ ([A-Za-z0-9]+)/", sdp)
                return {"ip": ip, "port": port, "path": path, "status": "ok",
                        "codec": codec.group(1) if codec else "",
                        "name": onvif_name or (server.group(1).strip() if server else "")}
            if code == 401:
                auth_port = auth_port or port
                break  # 需要帳密，路徑試不出來
    if first_open:
        return {"ip": ip, "port": auth_port or first_open, "path": "",
                "status": "auth" if auth_port else "unknown",
                "codec": "", "name": onvif_name or ""}
    if onvif_name:
        return {"ip": ip, "port": 0, "path": "", "status": "unknown", "codec": "", "name": onvif_name}
    return None


def scan(networks=None, workers=64):
    """掃描網段，回傳 {"time", "networks", "cameras": [...]}。"""
    if networks:
        nets = [(ipaddress.ip_network(n, strict=False), None) for n in networks]
    else:
        nets = local_networks()
    return scan_interfaces(nets, workers)


def scan_interfaces(nets, workers=64, stop=None):
    t0 = time.time()
    me = {m for _, m in nets if m}
    onvif = ws_discovery()
    hosts = []
    for net, _ in nets:
        hosts += [str(h) for h in net.hosts() if str(h) not in me]
    hosts += [ip for ip in onvif if ip not in hosts and ip not in me]
    cams = []
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for r in ex.map(lambda ip: probe_host(ip, onvif.get(ip), stop), hosts):
            if r:
                cams.append(r)
    cams.sort(key=lambda c: tuple(int(x) for x in c["ip"].split(".")))
    return {"time": time.time(), "seconds": round(time.time() - t0, 1),
            "networks": [str(n) for n, _ in nets], "cameras": cams}


def main(argv=None):
    parser = argparse.ArgumentParser(description="探索 RTSP 攝影機")
    parser.add_argument("networks", nargs="*", help="IPv4 網段")
    parser.add_argument("--json", action="store_true", help="只輸出 JSON")
    args = parser.parse_args(argv)
    res = scan(args.networks or None)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print("掃描 {} ，花 {} 秒，找到 {} 台：".format(", ".join(res["networks"]), res["seconds"],
                                              len(res["cameras"])))
    for c in res["cameras"]:
        url = "rtsp://{}:{}{}".format(c["ip"], c["port"], c["path"]) if c["path"] else "-"
        print("  {:<16} {:<8} {:<45} {}".format(c["ip"], c["status"], url, c["name"]))


if __name__ == "__main__":
    main()

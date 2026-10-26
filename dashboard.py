"""本地看板: 127.0.0.1, 同时跑网页服务和 QQ 消息监听, 并自动拉起/管理 SnowLuma."""

import asyncio
import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import qq_filter_route_b
from qq_filter_route_b import (
    OUT_FILE,
    PUSH_STATE_FILE,
    RECENT_FILE,
    SECRET_KEYS,
    SENSITIVE_KEYS,
    get_onebot_connected,
    get_secret,
    load_config,
    read_today_records,
    save_config,
    save_secrets,
    secret_status,
    send_email,
    send_wxpusher,
)

HOST = "127.0.0.1"
PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
SNOWLUMA_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snowluma-launch.log")
WEBUI_PORT = 5099
ONEBOT_PORT = 3001
MASK = "••••••"

CONTROLLER = None


def _port_open(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.8)
        s.close()
        return True
    except OSError:
        return False


class SnowLumaController:
    def __init__(self):
        self._lock = threading.Lock()
        self.cmd_pid = None
        self.node_pid = None
        self.launched = False
        self.starting = False
        self.last_launcher = None

    def _running(self):
        return _port_open(WEBUI_PORT) or _port_open(ONEBOT_PORT)

    def _resolve_launcher(self):
        cfg = load_config()
        base = os.path.dirname(os.path.abspath(__file__))
        cands = []
        d = (cfg.get("snow_luma_dir") or "").strip()
        if d:
            cands.append(d)
        cands.append(os.path.join(base, "SnowLuma"))
        cands.append(os.path.join(os.path.dirname(base), "SnowLuma"))
        for c in cands:
            if not c:
                continue
            p = c if c.lower().endswith("launcher.bat") else os.path.join(c, "launcher.bat")
            if os.path.exists(p):
                return os.path.dirname(p), p
        return None, None

    def _find_pid_on_port(self, port):
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return None
        for line in out.splitlines():
            if f"127.0.0.1:{port}" in line and "LISTENING" in line.upper():
                m = re.search(r"(\d+)\s*$", line.strip())
                if m:
                    return int(m.group(1))
        return None

    def ensure(self):
        if self._running():
            print("[snowluma] 已在运行, 跳过自动拉起")
            return
        print("[snowluma] SnowLuma 未运行, 尝试自动拉起")
        self.launch()

    def launch(self):
        with self._lock:
            if self._running():
                return self.status()
            dirpath, launcher = self._resolve_launcher()
            if not launcher:
                time.sleep(1)
                dirpath, launcher = self._resolve_launcher()
            if not launcher:
                print(
                    f"[snowluma] 未找到 launcher.bat。"
                    f"请在浏览器打开 http://127.0.0.1:{PORT} 进入「SnowLuma 连接」页填写 SnowLuma 目录，再点启动。"
                )
                return self.status()
            self.starting = True
            self.last_launcher = launcher
            try:
                logf = open(SNOWLUMA_LOG, "ab")
                proc = subprocess.Popen(
                    ["cmd.exe", "/c", launcher],
                    cwd=dirpath,
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                logf.close()
                self.cmd_pid = proc.pid
                self.launched = True
                self.node_pid = None
                print(f"[snowluma] 已拉起 launcher.bat, cmd_pid={proc.pid}")
                threading.Thread(target=self._discover_after_launch, daemon=True).start()
            except Exception as e:
                print(f"[snowluma] 拉起失败: {e}")
                self.starting = False
            return self.status()

    def _discover_after_launch(self):
        start = time.time()
        while time.time() - start < 60:
            if _port_open(WEBUI_PORT):
                self.node_pid = self._find_pid_on_port(WEBUI_PORT)
                self.starting = False
                print(f"[snowluma] 已启动, node_pid={self.node_pid}")
                return
            print(f"[snowluma] 等待 5099 就绪... {int(time.time() - start)}s")
            time.sleep(2)
        self.starting = False
        print("[snowluma] 等待超时: 5099 未就绪")

    def _log_tail(self, n=10):
        try:
            with open(SNOWLUMA_LOG, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return [ln.rstrip("\n")[-300:] for ln in lines[-n:]]
        except FileNotFoundError:
            return []

    def status(self):
        port3001 = _port_open(ONEBOT_PORT)
        port5099 = _port_open(WEBUI_PORT)
        onebot = get_onebot_connected()
        if onebot:
            state = "connected"
        elif port5099 or port3001:
            state = "qq_not_loaded"
        else:
            state = "stopped"
        launcher = None
        if state == "stopped":
            _, launcher = self._resolve_launcher()
        elif self.launched:
            launcher = self.last_launcher
        return {
            "state": state,
            "starting": self.starting,
            "port_3001": port3001,
            "port_5099": port5099,
            "onebot_connected": onebot,
            "launched_by_dashboard": self.launched,
            "launcher_path": launcher,
            "snow_luma_dir": (load_config().get("snow_luma_dir") or ""),
            "log_tail": self._log_tail(),
        }

    def stop(self):
        with self._lock:
            pid5099 = self._find_pid_on_port(WEBUI_PORT)
            if pid5099:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid5099), "/F"], capture_output=True, timeout=10)
                    print(f"[snowluma] 停止监听: 已结束 SnowLuma PID={pid5099}")
                except Exception as e:
                    print(f"[snowluma] 停止监听失败: {e}")
            self.node_pid = None
            self.cmd_pid = None
            self.launched = False
            self.starting = False
        return self.status()

    def shutdown(self):
        with self._lock:
            pids = []
            try:
                kill_all = bool(load_config().get("close_snowluma_on_exit", False))
            except Exception:
                kill_all = False
            if kill_all:
                pid5099 = self._find_pid_on_port(WEBUI_PORT)
                if pid5099:
                    pids.append(pid5099)
            else:
                if self.node_pid:
                    pids.append(self.node_pid)
                if self.cmd_pid:
                    pids.append(self.cmd_pid)
            for pid in pids:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
                    print(f"[snowluma] 已结束进程 PID={pid}")
                except Exception as e:
                    print(f"[snowluma] 结束 PID {pid} 失败: {e}")


def _secrets_view():
    conf = secret_status()
    values = {}
    for k in SECRET_KEYS:
        if k in SENSITIVE_KEYS:
            values[k] = MASK if conf[k] else ""
        else:
            values[k] = get_secret(k)
    return {"sensitive": list(SENSITIVE_KEYS), "configured": conf, "values": values}


def _recent_senders(group_id):
    out = {}
    if not os.path.exists(RECENT_FILE):
        return []
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if group_id is not None and str(r.get("group_id")) != str(group_id):
                    continue
                sid = r.get("sender_id")
                if sid is None:
                    continue
                out[sid] = {
                    "sender_id": sid,
                    "sender_name": r.get("sender_name") or str(sid),
                    "_ts": r.get("time") or "",
                }
    except Exception:
        return []
    senders = sorted(out.values(), key=lambda x: x["_ts"], reverse=True)
    for s in senders:
        s.pop("_ts", None)
    return senders


def _status():
    last_message = None
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
            if lines:
                last_message = json.loads(lines[-1]).get("time")
        except Exception:
            pass
    last_push = None
    if os.path.exists(PUSH_STATE_FILE):
        try:
            with open(PUSH_STATE_FILE, encoding="utf-8") as f:
                last_push = json.load(f).get("last_batch_time")
        except Exception:
            pass
    return {"last_message_time": last_message, "last_push_epoch": last_push}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        try:
            with open(HTML_FILE, encoding="utf-8") as f:
                body = f.read().encode("utf-8")
        except OSError as e:
            body = f"dashboard.html 读取失败: {e}".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            return self._send_html()
        if path == "/api/messages":
            date = (query.get("date") or [None])[0]
            return self._send_json({"messages": read_today_records(date)})
        if path == "/api/config":
            return self._send_json(load_config())
        if path == "/api/secrets":
            return self._send_json(_secrets_view())
        if path == "/api/recent-senders":
            gid = (query.get("group_id") or [None])[0]
            return self._send_json({"senders": _recent_senders(gid)})
        if path == "/api/status":
            return self._send_json(_status())
        if path == "/api/snowluma/status":
            return self._send_json(CONTROLLER.status())
        return self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/secrets":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                data = json.loads(raw)
            except Exception as e:
                return self._send_json({"error": f"请求体解析失败: {e}"}, 400)
            try:
                save_secrets(data)
            except Exception as e:
                return self._send_json({"error": f"保存失败: {e}"}, 500)
            return self._send_json(_secrets_view())
        if path != "/api/config":
            return self._send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            cfg = json.loads(raw)
        except Exception as e:
            return self._send_json({"error": f"请求体解析失败: {e}"}, 400)
        try:
            norm = save_config(cfg)
        except Exception as e:
            return self._send_json({"error": f"保存失败: {e}"}, 500)
        return self._send_json(norm)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/secrets/reveal":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                return self._send_json({"error": f"请求体解析失败: {e}"}, 400)
            key = body.get("key")
            if key not in SENSITIVE_KEYS:
                return self._send_json({"error": "字段不是敏感字段或不存在"}, 400)
            return self._send_json({"key": key, "value": get_secret(key)})
        if path == "/api/snowluma/start":
            return self._send_json(CONTROLLER.launch())
        if path == "/api/snowluma/stop":
            return self._send_json(CONTROLLER.stop())
        if path == "/api/snowluma/exit":
            self._send_json({"state": "exiting"})

            def _do_exit():
                time.sleep(0.4)
                try:
                    CONTROLLER.shutdown()
                finally:
                    os._exit(0)

            threading.Thread(target=_do_exit, daemon=True).start()
            return
        if path == "/api/push/test":
            cfg = load_config()
            content = "【测试推送】这是一条测试消息, 用于验证推送通道是否可用。"
            results = {}
            channels = cfg["push"]["channels"]
            if "wxpusher" in channels:
                results["wxpusher"] = send_wxpusher(content, "测试推送")
            if "email" in channels:
                results["email"] = send_email("测试推送", content)
            if "qq_private" in channels:
                results["qq_private"] = (False, "QQ 私聊随监听运行, 发一条真实消息即可验证")
            if not channels:
                results["local"] = (True, "当前只看本地, 未开启任何推送通道")
            return self._send_json({"results": results})
        return self._send_json({"error": "not found"}, 404)


def main():
    global CONTROLLER
    CONTROLLER = SnowLumaController()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"看板: http://{HOST}:{PORT}")
    threading.Thread(target=CONTROLLER.ensure, daemon=True).start()
    try:
        asyncio.run(qq_filter_route_b.main())
    except KeyboardInterrupt:
        pass
    finally:
        CONTROLLER.shutdown()
        print("看板已退出")


if __name__ == "__main__":
    main()

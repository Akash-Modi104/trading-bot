import subprocess, os
from flask import Flask, Response, request, stream_with_context

app = Flask(__name__)
BASE_DIR = "/opt/trading-bot"
AIDER    = f"{BASE_DIR}/venv/bin/aider"
MODEL    = "ollama/qwen2.5:7b-instruct-q4_K_M"
FILES    = ["dashboard.py","intraday_bot_v2.py","local_scanner.py","watchlist.py"]

HTML = r"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>Aider Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e17;color:#e2e8f0;font-family:'Inter',sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 20px}
h1{font-size:24px;font-weight:700;margin-bottom:4px}
h1 span{color:#10b981}
.sub{color:#64748b;font-size:13px;margin-bottom:32px}
.card{background:#111827;border:1px solid #1f2d42;border-radius:12px;padding:24px;width:100%;max-width:900px;margin-bottom:16px}
label{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:8px}
textarea{width:100%;background:#0a0e17;border:1px solid #243047;border-radius:8px;color:#e2e8f0;font-family:'Inter',sans-serif;font-size:14px;padding:14px;resize:vertical;min-height:110px;outline:none;transition:border-color .2s}
textarea:focus{border-color:#3b82f6}
.files{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0}
.filechk{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:500;color:#94a3b8;cursor:pointer;background:#1a2235;border:1px solid #243047;border-radius:6px;padding:7px 14px;transition:all .15s}
.filechk input{accent-color:#3b82f6;cursor:pointer}
.filechk.on{border-color:#3b82f6;color:#e2e8f0;background:#0f1f3d}
.row{display:flex;gap:10px;margin-top:16px;align-items:center}
button{display:flex;align-items:center;gap:8px;background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:11px 22px;font-size:14px;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;transition:background .15s}
button:hover{background:#2563eb}
button:disabled{background:#1e3a6e;opacity:.5;cursor:not-allowed}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:4px 12px;border-radius:99px;background:#1a2235;color:#64748b}
.pill.running{background:#0f1f3d;color:#3b82f6}
.pill.done{background:#0d2e22;color:#10b981}
.pill.error{background:#2d1515;color:#ef4444}
#out{background:#000;border:1px solid #1a2235;border-radius:8px;padding:18px;font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.9;min-height:260px;max-height:520px;overflow-y:auto;white-space:pre-wrap;color:#94a3b8}
.ok{color:#10b981}.err{color:#ef4444}.hi{color:#a3e635}.info{color:#38bdf8}.warn{color:#f59e0b}
</style></head>
<body>
<h1><i class="fa-solid fa-robot" style="color:#10b981;margin-right:8px"></i>Aider <span>Agent</span></h1>
<p class="sub">Type plain English — Qwen2.5-7B reads &amp; edits your code automatically</p>

<div class="card">
  <label>Instruction</label>
  <textarea id="cmd" placeholder="e.g. Add a real-time P&L chart to dashboard.py using Chart.js. Bind to 0.0.0.0:5001."></textarea>

  <label style="margin-top:20px">Files to edit</label>
  <div class="files" id="fileList"></div>

  <div class="row">
    <button id="runBtn" onclick="run()"><i class="fa-solid fa-play"></i> Run</button>
    <button onclick="clearOut()" style="background:#1a2235;color:#94a3b8"><i class="fa-solid fa-trash"></i> Clear</button>
    <span class="pill" id="pill"><i class="fa-solid fa-circle-dot"></i> idle</span>
  </div>
</div>

<div class="card">
  <label>Output</label>
  <div id="out">Ready. Enter an instruction above and click Run.</div>
</div>

<script>
const FILES = ["dashboard.py","intraday_bot_v2.py","local_scanner.py","watchlist.py"];
const fl = document.getElementById('fileList');
FILES.forEach(f => {
  const lbl = document.createElement('label');
  lbl.className = 'filechk on';
  lbl.innerHTML = `<input type="checkbox" value="${f}" checked/> ${f}`;
  lbl.querySelector('input').addEventListener('change', e => {
    lbl.className = 'filechk' + (e.target.checked?' on':'');
  });
  fl.appendChild(lbl);
});

let es = null;
function run() {
  const cmd = document.getElementById('cmd').value.trim();
  if (!cmd) { alert('Enter an instruction first'); return; }
  const sel = [...document.querySelectorAll('#fileList input:checked')].map(i=>i.value);
  if (!sel.length) { alert('Select at least one file'); return; }

  if (es) es.close();
  const out = document.getElementById('out');
  const btn = document.getElementById('runBtn');
  const pill = document.getElementById('pill');
  out.innerHTML = '';
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Running...';
  pill.className = 'pill running';
  pill.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> running';

  es = new EventSource('/run?cmd='+encodeURIComponent(cmd)+'&files='+encodeURIComponent(sel.join(',')));
  es.onmessage = e => {
    const line = e.data;
    const sp = document.createElement('span');
    if (/Applied|Wrote|wrote|created|Updated/.test(line)) sp.className='ok';
    else if (/[Ee]rror|failed|traceback/i.test(line)) sp.className='err';
    else if (/^>>>|^---|\*\*\*/.test(line)) sp.className='hi';
    else if (/Warning|warning/.test(line)) sp.className='warn';
    sp.textContent = line+'\n';
    out.appendChild(sp);
    out.scrollTop = out.scrollHeight;
  };
  es.addEventListener('done', () => {
    es.close(); es=null;
    btn.disabled=false;
    btn.innerHTML='<i class="fa-solid fa-play"></i> Run';
    pill.className='pill done';
    pill.innerHTML='<i class="fa-solid fa-check"></i> done';
  });
  es.onerror = () => {
    if(es){es.close();es=null;}
    btn.disabled=false;
    btn.innerHTML='<i class="fa-solid fa-play"></i> Run';
    pill.className='pill error';
    pill.innerHTML='<i class="fa-solid fa-xmark"></i> error';
  };
}
function clearOut(){document.getElementById('out').innerHTML='Ready.';}
document.getElementById('cmd').addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='Enter')run();});
</script>
</body></html>"""

@app.route("/")
def index():
    return HTML

@app.route("/run")
def run_aider():
    cmd   = request.args.get("cmd","").strip()
    files = [f.strip() for f in request.args.get("files","").split(",") if f.strip()]
    if not cmd:
        return Response("data: No command given\n\nevent:done\ndata:done\n\n", mimetype="text/event-stream")

    def generate():
        args = [AIDER,"--model",MODEL,"--message",cmd,
                "--yes","--no-git","--no-pretty"] + files
        env  = {**os.environ,"OLLAMA_API_BASE":"http://localhost:11434"}
        try:
            proc = subprocess.Popen(args, cwd=BASE_DIR, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
        yield "event: done\ndata: done\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

if __name__ == "__main__":
    print("[aider-ui] Running on http://0.0.0.0:5002")
    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)

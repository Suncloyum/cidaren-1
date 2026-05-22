"""
词达人 Web 控制台
- 列出所有任务（task_name / progress / score）
- 点击启动按钮在子进程中刷题
- 定时刷新分数和子进程日志
- 支持在前端编辑 token / LLM 配置并同步写入 .env
"""
import os, sys, subprocess, threading, time, signal
from collections import deque
from flask import Flask, jsonify, request, Response

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import a as quiz  # noqa
    from config import (
        build_subprocess_env,
        env_file_path,
        get_missing_auth_fields,
        get_runtime_config,
        save_runtime_config,
    )
else:
    from . import a as quiz  # noqa
    from .config import (
        build_subprocess_env,
        env_file_path,
        get_missing_auth_fields,
        get_runtime_config,
        save_runtime_config,
    )

app = Flask(__name__)

# ==== 子进程任务状态 ====
JOBS = {}  # task_id -> {"proc": Popen, "logs": deque, "started": ts, "done": bool}
JOBS_LOCK = threading.Lock()
LOG_MAX = 500


def _job_key(source, task_id, release_id):
    return ((source or "class"), str(task_id), str(release_id))


def _int_or_default(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_job(source, task_id, release_id):
    job = JOBS.get(_job_key(source, task_id, release_id))
    if job or source != "study":
        return job
    for (job_source, _job_task_id, job_release_id), candidate in JOBS.items():
        if job_source == "study" and job_release_id == str(release_id):
            return candidate
    return None


def _client(config=None):
    cfg = config or get_runtime_config()
    missing = get_missing_auth_fields(cfg)
    if missing:
        raise ValueError(f"请先在配置面板填写: {', '.join(missing)}")
    return quiz.Client(cfg["USERTOKEN"], cfg["ABC"], cfg["AUTH_V"])


def _list_class_tasks(c):
    """拉取班级任务, 多页累加。"""
    out, page = [], 1
    while True:
        resp = c.page_task(page=page, size=50)
        recs = (resp.get("data") or {}).get("records") or []
        if not recs:
            break
        out.extend(recs)
        if len(recs) < 50:
            break
        page += 1
        if page > 20:
            break
    return out


def _list_study_tasks(c, course_id):
    """拉取自学任务。"""
    resp = c.study_task_list(course_id=course_id)
    data = resp.get("data") or {}
    return data.get("task_list") or []


def _list_tasks():
    """拉取班级任务 + 自学任务。"""
    cfg = get_runtime_config()
    c = _client(cfg)
    course_id = (cfg.get("COURSE_ID") or "CET4_v2").strip() or "CET4_v2"
    study_grade = _int_or_default(cfg.get("STUDY_GRADE"), 2)
    out, warnings = [], []

    try:
        for r in _list_class_tasks(c):
            tid = r.get("task_id")
            rid = r.get("release_id")
            job = _find_job("class", tid, rid)
            out.append({
                "source": "class",
                "source_label": "班级",
                "can_start": True,
                "task_id": tid,
                "release_id": rid,
                "task_name": r.get("task_name"),
                "progress": r.get("progress"),
                "score": r.get("score"),
                "running": bool(job and not job["done"]),
                "done": bool(job and job["done"]),
                "exit_code": job["exit_code"] if job else None,
                "loop": bool(job and job.get("loop")),
                "round": job.get("round", 0) if job else 0,
            })
    except Exception as e:
        warnings.append(f"班级任务读取失败: {e}")

    try:
        for r in _list_study_tasks(c, course_id):
            tid = r.get("task_id")
            rid = r.get("list_id")
            job = _find_job("study", tid, rid)
            out.append({
                "source": "study",
                "source_label": "自学",
                "can_start": True,
                "task_id": tid,
                "release_id": rid,
                "course_id": r.get("course_id") or course_id,
                "list_id": rid,
                "task_type": r.get("task_type"),
                "grade": _int_or_default(r.get("grade"), study_grade),
                "task_name": r.get("task_name"),
                "progress": r.get("progress"),
                "score": r.get("score"),
                "running": bool(job and not job["done"]),
                "done": bool(job and job["done"]),
                "exit_code": job["exit_code"] if job else None,
                "loop": bool(job and job.get("loop")),
                "round": job.get("round", 0) if job else 0,
            })
    except Exception as e:
        warnings.append(f"自学任务读取失败: {e}")

    return out, warnings


def _reader_thread(job_id, proc):
    """读取子进程 stdout, 写入 ring buffer"""
    job = JOBS[job_id]
    for line in iter(proc.stdout.readline, b""):
        try:
            text = line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            text = repr(line)
        job["logs"].append(text)
    proc.wait()
    job["done"] = True
    job["exit_code"] = proc.returncode

    # 循环模式: 如果未满分则重新启动
    if job.get("loop") and not job.get("stopped"):
        try:
            score = _query_score(job["source"], job["task_id"], job["release_id"], job.get("course_id"), job.get("list_id"))
        except Exception as e:
            job["logs"].append(f"[loop] 查询分数失败: {e}")
            score = None
        if score is not None and score >= 100:
            job["logs"].append(f"[loop] 已满分 ({score}), 停止循环")
            return
        job["logs"].append(f"[loop] 当前分数={score}, 5s 后重新启动...")
        time.sleep(5)
        if job.get("stopped"):
            return
        with JOBS_LOCK:
            _spawn_job(
                job["source"],
                job["task_id"],
                job["release_id"],
                loop=True,
                course_id=job.get("course_id"),
                list_id=job.get("list_id"),
                task_type=job.get("task_type"),
                grade=job.get("grade"),
            )


def _query_score(source, task_id, release_id, course_id=None, list_id=None):
    """轻量查询单个任务当前分数"""
    c = _client()
    if source == "study":
        resp = c.study_task_list(course_id=course_id or "CET4_v2")
        recs = (resp.get("data") or {}).get("task_list") or []
        for r in recs:
            if str(r.get("list_id")) == str(list_id or release_id) or str(r.get("task_id")) == str(task_id):
                return r.get("score")
        return None

    page = 1
    while page <= 20:
        resp = c.page_task(page=page, size=50)
        recs = (resp.get("data") or {}).get("records") or []
        if not recs:
            return None
        for r in recs:
            if r.get("task_id") == task_id and r.get("release_id") == release_id:
                return r.get("score")
        if len(recs) < 50:
            return None
        page += 1
    return None


def _spawn_job(source, task_id, release_id, loop=False, config=None, course_id=None, list_id=None, task_type=None, grade=None):
    """启动子进程跑一个 task, 配置通过环境变量透传给 runner。"""
    if source == "study":
        args = ["study", str(task_id), str(list_id or release_id), str(course_id or "CET4_v2"), str(task_type or 3), str(grade or 2)]
    else:
        args = ["class", str(task_id), str(release_id)]
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "cidaren._runner", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1,
        env=build_subprocess_env(config),
    )
    # 保留旧的 logs (循环模式下追加)
    key = _job_key(source, task_id, release_id)
    old = JOBS.get(key)
    logs = old["logs"] if old else deque(maxlen=LOG_MAX)
    if old:
        logs.append(f"========== 第 {old.get('round', 1) + 1} 轮启动 ==========")
    JOBS[key] = {
        "proc": proc,
        "logs": logs,
        "started": time.time(),
        "done": False,
        "exit_code": None,
        "source": source,
        "release_id": release_id,
        "task_id": task_id,
        "course_id": course_id,
        "list_id": list_id,
        "task_type": task_type,
        "grade": grade,
        "loop": loop,
        "stopped": False,
        "round": (old.get("round", 1) + 1) if old else 1,
    }
    threading.Thread(target=_reader_thread, args=(key, proc), daemon=True).start()


# ==== Routes ====

@app.route("/")
def index():
    return Response(_INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/api/config")
def api_config():
    config = get_runtime_config()
    return jsonify({
        "ok": True,
        "config": config,
        "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(config),
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(force=True) or {}
    saved = save_runtime_config(body)
    return jsonify({
        "ok": True,
        "config": saved,
        "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(saved),
    })


@app.route("/api/tasks")
def api_tasks():
    try:
        tasks, warnings = _list_tasks()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tasks": tasks, "warnings": warnings})


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(force=True)
    source = body.get("source") or "class"
    task_id = int(body["task_id"])
    release_id = body["release_id"]
    if source == "class":
        release_id = int(release_id)
    course_id = body.get("course_id")
    list_id = body.get("list_id") or release_id
    task_type = int(body.get("task_type") or 3)
    config = get_runtime_config()
    grade = _int_or_default(body.get("grade") or config.get("STUDY_GRADE"), 2)
    loop = bool(body.get("loop", False))
    missing = get_missing_auth_fields(config)
    if missing:
        return jsonify({"ok": False, "error": f"请先填写配置: {', '.join(missing)}"}), 400
    with JOBS_LOCK:
        job = _find_job(source, task_id, release_id)
        if job and not job["done"]:
            return jsonify({"ok": False, "error": "任务正在运行"}), 400
        _spawn_job(source, task_id, release_id, loop=loop, config=config, course_id=course_id, list_id=list_id, task_type=task_type, grade=grade)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    body = request.get_json(force=True)
    source = body.get("source") or "class"
    task_id = int(body["task_id"])
    release_id = body["release_id"]
    if source == "class":
        release_id = int(release_id)
    job = _find_job(source, task_id, release_id)
    if not job:
        return jsonify({"ok": False, "error": "任务未运行"}), 400
    # 标记 stopped, 阻断循环重启
    job["stopped"] = True
    job["loop"] = False
    if not job["done"]:
        try:
            job["proc"].send_signal(signal.SIGTERM)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/logs")
def api_logs_query():
    source = request.args.get("source") or "class"
    task_id = request.args.get("task_id")
    release_id = request.args.get("release_id")
    job = _find_job(source, task_id, release_id)
    if not job:
        return jsonify({"ok": False, "logs": []})
    return jsonify({
        "ok": True,
        "logs": list(job["logs"]),
        "done": job["done"],
        "exit_code": job["exit_code"],
    })


@app.route("/api/logs/<int:task_id>/<int:release_id>")
def api_logs(task_id, release_id):
    job = _find_job("class", task_id, release_id)
    if not job:
        return jsonify({"ok": False, "logs": []})
    return jsonify({
        "ok": True,
        "logs": list(job["logs"]),
        "done": job["done"],
        "exit_code": job["exit_code"],
    })


# ==== HTML ====
_INDEX_HTML = '''<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>词达人 控制台</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
         margin: 0; padding: 24px; background: #f5f7fb; color: #1d1d1f; }
  h1 { font-size: 26px; margin: 0 0 8px; }
  .subtitle { margin: 0 0 20px; color: #667085; font-size: 14px; }
  .layout { display: grid; gap: 20px; }
  .card { background: #fff; border: 1px solid #eaecf0; border-radius: 16px; box-shadow: 0 8px 24px rgba(15,23,42,.04); overflow: hidden; }
  .card-head { padding: 18px 20px; border-bottom: 1px solid #f2f4f7; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .card-head h2 { margin: 0; font-size: 18px; }
  .card-head p { margin: 4px 0 0; color: #667085; font-size: 13px; }
  .card-body { padding: 20px; }
  .bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; font-size: 13px; color: #666; flex-wrap: wrap; }
  button { font: inherit; padding: 6px 14px; border-radius: 6px; border: 1px solid #d2d2d7;
           background: white; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  button.primary { background: #007aff; color: white; border-color: #007aff; }
  button.primary:hover { background: #0062cc; }
  button.danger { background: #ff3b30; color: white; border-color: #ff3b30; }
  button.loop { background: #5856d6; color: white; border-color: #5856d6; }
  button.loop:hover { background: #4845b0; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 16px; }
  .field { display: flex; flex-direction: column; gap: 8px; }
  .field label { font-size: 13px; font-weight: 600; color: #344054; }
  .field input { width: 100%; border: 1px solid #d0d5dd; border-radius: 10px; padding: 10px 12px; font: inherit; }
  .field small { color: #667085; font-size: 12px; }
  .full { grid-column: 1 / -1; }
  .status-box { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; background: #f8fafc; border: 1px solid #e2e8f0; color: #475467; font-size: 12px; }
  .status-box.ok { background: #ecfdf3; color: #027a48; border-color: #d1fadf; }
  .status-box.warn { background: #fff7ed; color: #b54708; border-color: #fed7aa; }
  .inline-code { font-family: ui-monospace, Menlo, monospace; background: #f2f4f7; padding: 2px 6px; border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
  th { background: #fafafa; font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  .progress-bar { width: 100px; height: 6px; background: #e8e8ed; border-radius: 3px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }
  .progress-bar > div { height: 100%; background: #34c759; transition: width .3s; }
  .score { font-weight: 600; }
  .score.full { color: #34c759; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .badge.run { background: #fff3cd; color: #856404; }
  .badge.done { background: #d4edda; color: #155724; }
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 100; }
  .modal.show { display: flex; align-items: center; justify-content: center; }
  .modal-body { background: #1e1e1e; color: #d4d4d4; width: 80vw; height: 75vh; border-radius: 8px;
                padding: 16px; overflow: hidden; display: flex; flex-direction: column; }
  .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; color: white; }
  .modal-logs { flex: 1; overflow-y: auto; font-family: ui-monospace, Menlo, monospace; font-size: 12px;
                white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
  @media (max-width: 900px) {
    body { padding: 16px; }
    .grid { grid-template-columns: 1fr; }
    table { display: block; overflow-x: auto; }
  }
</style>
</head>
<body>
  <h1>📚 cidaren 控制台</h1>
  <p class="subtitle">在浏览器里维护词达人鉴权与 LLM 配置，保存后自动写入 <span class="inline-code">.env</span>，并用于后续任务执行。</p>

  <div class="layout">
    <section class="card">
      <div class="card-head">
        <div>
          <h2>配置中心</h2>
          <p>这里填写 token / LLM 变量，点击保存后会同步落盘到项目根目录的 <span class="inline-code">.env</span>。</p>
        </div>
        <div id="config-pill" class="status-box">读取中...</div>
      </div>
      <div class="card-body">
        <div class="grid">
          <div class="field">
            <label for="USERTOKEN">USERTOKEN</label>
            <input id="USERTOKEN" type="password" autocomplete="off" />
            <small>词达人请求头中的 usertoken。</small>
          </div>
          <div class="field">
            <label for="ABC">ABC</label>
            <input id="ABC" type="password" autocomplete="off" />
            <small>词达人请求头中的 abc。</small>
          </div>
          <div class="field full">
            <label for="AUTH_V">AUTH_V</label>
            <input id="AUTH_V" type="password" autocomplete="off" />
            <small>词达人请求头中的 authorization-v。</small>
          </div>
          <div class="field">
            <label for="COURSE_ID">COURSE_ID</label>
            <input id="COURSE_ID" type="text" placeholder="CET4_v2" />
            <small>自学任务课程 ID。</small>
          </div>
          <div class="field">
            <label for="STUDY_GRADE">STUDY_GRADE</label>
            <input id="STUDY_GRADE" type="number" min="1" max="4" placeholder="2" />
            <small>自学模式: 1 快速 / 2 普通 / 3 完整 / 4 超级困难。</small>
          </div>
          <div class="field">
            <label for="LLM_URL">LLM_URL</label>
            <input id="LLM_URL" type="text" placeholder="https://ai.saurlax.com/" />
            <small>OpenAI 兼容接口地址，留空表示不启用 LLM 兜底。</small>
          </div>
          <div class="field">
            <label for="LLM_MODEL">LLM_MODEL</label>
            <input id="LLM_MODEL" type="text" placeholder="step-3.6" />
            <small>请求使用的模型名。</small>
          </div>
          <div class="field full">
            <label for="LLM_KEY">LLM_KEY</label>
            <input id="LLM_KEY" type="password" autocomplete="off" />
            <small>Bearer token / API Key。</small>
          </div>
        </div>

        <div class="bar" style="margin-top:18px; margin-bottom:0; justify-content:space-between;">
          <div id="env-path" class="status-box">.env 路径加载中...</div>
          <div class="btn-row">
            <button onclick="loadConfig()">重新读取</button>
            <button class="primary" onclick="saveConfig(false)">保存配置</button>
            <button class="primary" onclick="saveConfig(true)">保存并刷新任务</button>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <div>
          <h2>任务面板</h2>
          <p>启动后会在子进程里执行刷题流程，日志可实时查看。</p>
        </div>
        <div class="status-box">分数自动每 5 秒刷新</div>
      </div>
      <div class="card-body">
        <div class="bar">
          <button onclick="loadTasks()">🔄 刷新任务</button>
          <span id="status">加载中...</span>
        </div>
        <table>
          <thead>
            <tr>
              <th style="width:40px">#</th>
              <th>任务名</th>
              <th style="width:160px">进度</th>
              <th style="width:80px">分数</th>
              <th style="width:80px">状态</th>
              <th style="width:200px">操作</th>
            </tr>
          </thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <div class="modal" id="modal" onclick="if(event.target===this)closeLogs()">
    <div class="modal-body">
      <div class="modal-head">
        <span id="modal-title">日志</span>
        <button onclick="closeLogs()">✕ 关闭</button>
      </div>
      <div class="modal-logs" id="modal-logs"></div>
    </div>
  </div>

<script>
let activeLogKey = null;
let logTimer = null;
let currentTasks = [];
const CONFIG_KEYS = ["USERTOKEN", "ABC", "AUTH_V", "COURSE_ID", "STUDY_GRADE", "LLM_URL", "LLM_KEY", "LLM_MODEL"];

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "读取配置失败");
    for (const key of CONFIG_KEYS) {
      const el = document.getElementById(key);
      if (el) el.value = j.config[key] || "";
    }
    document.getElementById("env-path").textContent = `📄 ${j.env_file}`;
    renderConfigStatus(j.missing_auth || []);
  } catch (e) {
    const pill = document.getElementById("config-pill");
    pill.className = "status-box warn";
    pill.textContent = "配置读取失败";
    document.getElementById("env-path").textContent = "❌ " + e.message;
  }
}

function renderConfigStatus(missing) {
  const pill = document.getElementById("config-pill");
  if (!missing || missing.length === 0) {
    pill.className = "status-box ok";
    pill.textContent = "鉴权配置完整，可直接启动任务";
    return;
  }
  pill.className = "status-box warn";
  pill.textContent = `缺少字段: ${missing.join(", ")}`;
}

async function saveConfig(refreshTasks) {
  const payload = {};
  for (const key of CONFIG_KEYS) {
    const el = document.getElementById(key);
    payload[key] = el ? el.value : "";
  }
  const r = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json();
  if (!j.ok) {
    alert("保存失败: " + (j.error || "未知错误"));
    return;
  }
  renderConfigStatus(j.missing_auth || []);
  document.getElementById("env-path").textContent = `✅ 已保存到 ${j.env_file}`;
  if (refreshTasks) await loadTasks();
}

async function loadTasks() {
  document.getElementById("status").textContent = "拉取中...";
  try {
    const r = await fetch("/api/tasks");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
    render(j.tasks);
    const warnings = (j.warnings || []).length ? ` · ${j.warnings.join(" · ")}` : "";
    document.getElementById("status").textContent =
      `共 ${j.tasks.length} 个任务 · 已更新 ${new Date().toLocaleTimeString()}${warnings}`;
  } catch (e) {
    document.getElementById("status").textContent = "❌ " + e.message;
  }
}

function render(tasks) {
  currentTasks = tasks;
  const tb = document.getElementById("tbody");
  if (!tasks.length) {
    tb.innerHTML = `<tr><td colspan="6" style="color:#667085; text-align:center; padding:24px;">暂无任务，或当前账号下还没有可见任务。</td></tr>`;
    return;
  }
  tb.innerHTML = tasks.map((t, i) => {
    const prog = t.progress || 0;
    const score = t.score == null ? "-" : t.score;
    const scoreCls = t.score >= 100 ? "score full" : "score";
    let badge = `<span class="badge">${escapeHtml(t.source_label || "")}</span>`;
    if (t.running) {
      badge += t.loop
        ? ` <span class="badge run">🔁 循环中 (第${t.round}轮)</span>`
        : ` <span class="badge run">运行中</span>`;
    } else if (t.done) {
      badge += ` <span class="badge done">已完成</span>`;
    }
    return `
      <tr>
        <td>${i}</td>
        <td>${escapeHtml(t.task_name)}</td>
        <td>
          <div class="progress-bar"><div style="width:${prog}%"></div></div>
          ${prog}%
        </td>
        <td class="${scoreCls}">${score}</td>
        <td>${badge}</td>
        <td>
          ${!t.can_start
            ? `<button disabled title="${escapeHtml(t.note || "暂不支持启动")}">仅展示</button>`
            : t.running
            ? `<button class="danger" onclick="stopTask(${i})">停止</button>
               <button onclick="showLogs(${i})">日志</button>`
            : `<button class="primary" onclick="startTask(${i}, false)">▶ 启动</button>
               <button class="loop" onclick="startTask(${i}, true)" title="刷到100分为止">🔁 循环</button>
               ${t.done ? `<button onclick="showLogs(${i})">日志</button>` : ""}`
          }
        </td>
      </tr>`;
  }).join("");
}

function taskPayload(task, loop) {
  return {
    source: task.source || "class",
    task_id: task.task_id,
    release_id: task.release_id,
    course_id: task.course_id,
    list_id: task.list_id,
    task_type: task.task_type,
    grade: task.grade,
    loop: !!loop,
  };
}

async function startTask(index, loop) {
  const task = currentTasks[index];
  if (!task) return;
  const r = await fetch("/api/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(taskPayload(task, loop))
  });
  const j = await r.json();
  if (!j.ok) { alert("启动失败: " + j.error); return; }
  showLogs(index, loop);
  loadTasks();
}

async function stopTask(index) {
  const task = currentTasks[index];
  if (!task) return;
  if (!confirm("停止任务?")) return;
  const r = await fetch("/api/stop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(taskPayload(task, false))
  });
  const j = await r.json();
  if (!j.ok) alert("停止失败: " + j.error);
  loadTasks();
}

async function showLogs(index, loop) {
  const task = currentTasks[index];
  if (!task) return;
  activeLogKey = taskPayload(task, false);
  document.getElementById("modal-title").textContent = "📜 " + task.task_name + (loop ? " 🔁" : "");
  document.getElementById("modal").classList.add("show");
  await refreshLogs();
  if (logTimer) clearInterval(logTimer);
  logTimer = setInterval(refreshLogs, 1500);
}

function closeLogs() {
  activeLogKey = null;
  document.getElementById("modal").classList.remove("show");
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

async function refreshLogs() {
  if (!activeLogKey) return;
  try {
    const qs = new URLSearchParams({
      source: activeLogKey.source || "class",
      task_id: activeLogKey.task_id,
      release_id: activeLogKey.release_id,
    });
    const r = await fetch(`/api/logs?${qs.toString()}`);
    const j = await r.json();
    if (j.ok) {
      const box = document.getElementById("modal-logs");
      const wasBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      box.textContent = j.logs.join("\\n");
      if (wasBottom) box.scrollTop = box.scrollHeight;
      if (j.done) {
        clearInterval(logTimer); logTimer = null;
        loadTasks();
      }
    }
  } catch (e) {}
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}

loadConfig().then(loadTasks);
setInterval(loadTasks, 5000);
</script>
</body>
</html>
'''


def main():
    host = "127.0.0.1"
    port = 5000
    print(f"🌐 http://localhost:{port}")
    print(f"📄 配置文件: {env_file_path()}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

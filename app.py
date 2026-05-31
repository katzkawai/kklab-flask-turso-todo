# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flask>=3.0",
#     "libsql>=0.1.0",
#     "python-dotenv>=1.0",
# ]
# ///
"""備忘録 (To Do) アプリ — Flask + Turso (libSQL) / Embedded Replica = Sync 方式

PEP 723 のインラインスクリプトメタデータを利用した単一ファイル構成。
依存関係の解決と仮想環境の用意は uv が自動で行うため、次のコマンドで起動できます。

    uv run app.py

データベースには Turso (libSQL) を **Embedded Replica（同期＝Sync 方式）** で利用します。
ローカルにレプリカ用の SQLite ファイルを置き、リモートの Turso と `sync()` で同期します。

    - 読み取りはローカルレプリカから（高速・オフラインでも可）
    - 書き込みはローカルへ反映したうえで sync() でリモートへ反映
    - ネットワーク断時は同期をスキップし、ローカルレプリカで動作を継続

接続情報は環境変数で渡します。
    TURSO_DATABASE_URL : リモート Turso の URL（sync_url）。設定すると Sync 方式が有効。
    TURSO_AUTH_TOKEN   : 認証トークン。
未設定の場合はローカルレプリカ単体（同期なし）のローカル DB として動作します。
"""

import os
import threading
from contextlib import contextmanager

import libsql
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template_string,
    request,
    url_for,
)

# .env があれば読み込む（無ければ何もしない）
load_dotenv()

# ---------------------------------------------------------------------------
# 設定（Sync 方式）
# ---------------------------------------------------------------------------
# リモート Turso の URL（sync_url）。設定されていれば Embedded Replica = Sync 方式。
SYNC_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
# ローカルに置くレプリカ（SQLite ファイル）のパス。
LOCAL_PATH = os.environ.get("TURSO_SYNC_LOCAL_PATH", "todo.db").strip() or "todo.db"
# 任意: バックグラウンドで定期的に自動同期する間隔（秒）。未設定なら手動同期のみ。
_interval_raw = os.environ.get("TURSO_SYNC_INTERVAL", "").strip()
SYNC_INTERVAL = float(_interval_raw) if _interval_raw else None
# リモートに接続できないとき、ローカル単体へ縮退せず即エラーにするか（1 で必須）。
REQUIRE_SYNC = os.environ.get("TURSO_REQUIRE_SYNC", "").strip() == "1"

# SYNC_URL があれば Sync 方式（リモートと同期）、無ければローカル単体。
SYNC_MODE = bool(SYNC_URL)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")


# ---------------------------------------------------------------------------
# データベース（単一接続をロックで直列化して扱う）
# ---------------------------------------------------------------------------
# Embedded Replica はローカルのレプリカファイルに紐づくため、接続はプロセスで 1 本だけ
# 保持し、Flask のスレッド間共有に備えて threading.Lock で直列化する。
_db_lock = threading.Lock()
_conn = None
_schema_ready = False
# Sync 方式を要求したがリモートに繋げず、ローカル単体へ縮退している状態か。
_degraded = False


def _connect_local():
    """同期なしのローカル単体接続を開く。"""
    return libsql.connect(database=LOCAL_PATH, _check_same_thread=False)


def _connect():
    """libSQL 接続を生成する。

    SYNC_MODE ならリモートの Embedded Replica（接続時に初回 pull）として開く。
    リモートへ繋げない場合は、REQUIRE_SYNC でなければローカル単体へ縮退する。
    """
    global _degraded
    if not SYNC_MODE:
        return _connect_local()

    kwargs = dict(database=LOCAL_PATH, _check_same_thread=False,
                  sync_url=SYNC_URL, auth_token=AUTH_TOKEN)
    if SYNC_INTERVAL:
        # libSQL がバックグラウンドスレッドで定期的に sync() してくれる。
        kwargs["sync_interval"] = SYNC_INTERVAL
    try:
        conn = libsql.connect(**kwargs)
        _degraded = False
        return conn
    except Exception as exc:
        if REQUIRE_SYNC:
            raise
        _degraded = True
        app.logger.warning(
            "Turso への接続に失敗しました: %s — ローカルレプリカ (%s) で縮退起動します。"
            "（同期は行われません。資格情報やネットワークを確認してください）",
            exc,
            LOCAL_PATH,
        )
        return _connect_local()


def _safe_sync(conn, reason=""):
    """リモートと同期する。失敗してもローカルレプリカで継続できるよう握りつぶす。"""
    if not SYNC_MODE or _degraded:
        return False
    try:
        conn.sync()
        return True
    except Exception as exc:  # ネットワーク断・認証エラー等
        app.logger.warning(
            "Turso 同期に失敗しました (%s): %s — ローカルレプリカで継続します",
            reason or "sync",
            exc,
        )
        return False


def _ensure_conn():
    """接続とスキーマを 1 度だけ用意する（呼び出し側で _db_lock を保持済みであること）。"""
    global _conn, _schema_ready
    if _conn is None:
        # connect() 自体が初回 pull を兼ねる（縮退時はローカル接続）。
        _conn = _connect()
    if not _schema_ready:
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT    NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        _conn.commit()
        _safe_sync(_conn, "スキーマ作成後の同期")
        _schema_ready = True
    return _conn


@contextmanager
def get_db(sync_before=False):
    """接続をロック下で貸し出す。sync_before=True なら読み取り前にプルする。"""
    with _db_lock:
        conn = _ensure_conn()
        if sync_before:
            _safe_sync(conn, "読み取り前の同期")
        yield conn


# ---------------------------------------------------------------------------
# 画面テンプレート（単一ファイル構成のためインライン化）
# ---------------------------------------------------------------------------
PAGE = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>備忘録 To Do</title>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --fg:#e2e8f0;
            --muted:#94a3b8; --accent:#38bdf8; --danger:#f87171; --ok:#34d399; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", "Hiragino Sans",
           "Noto Sans JP", sans-serif; background:var(--bg); color:var(--fg);
           line-height:1.6; }
    .wrap { max-width: 720px; margin: 0 auto; padding: 32px 20px 64px; }
    h1 { font-size: 1.6rem; margin: 0 0 4px; }
    .sub { color: var(--muted); margin: 0 0 24px; font-size: .9rem; }
    form.add { display:flex; gap:8px; margin-bottom: 20px; }
    input[type=text] { flex:1; padding:12px 14px; border-radius:10px;
           border:1px solid var(--line); background:var(--card); color:var(--fg);
           font-size:1rem; }
    input[type=text]:focus { outline:2px solid var(--accent); border-color:transparent; }
    button { cursor:pointer; border:none; border-radius:10px; padding:12px 16px;
           font-size:.95rem; font-weight:600; background:var(--accent); color:#04263a; }
    button:hover { filter:brightness(1.08); }
    .btn-ghost { background:transparent; color:var(--muted); padding:8px 10px;
           font-weight:500; }
    .btn-ghost:hover { color:var(--fg); }
    .btn-danger { color:var(--danger); }
    ul.list { list-style:none; margin:0; padding:0; display:flex; flex-direction:column;
           gap:8px; }
    li.item { display:flex; align-items:center; gap:12px; background:var(--card);
           border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
    li.item.done .title { text-decoration:line-through; color:var(--muted); }
    .title { flex:1; word-break:break-word; }
    .meta { color:var(--muted); font-size:.75rem; }
    .check { width:20px; height:20px; accent-color:var(--ok); cursor:pointer; }
    .inline { display:inline; }
    .empty { text-align:center; color:var(--muted); padding:40px 0; }
    .flash { background:#064e3b; border:1px solid #065f46; color:#d1fae5;
           padding:10px 14px; border-radius:10px; margin-bottom:16px; font-size:.9rem; }
    .bar { display:flex; justify-content:space-between; align-items:center;
           margin-bottom:12px; color:var(--muted); font-size:.85rem; gap:12px; }
    .source { font-family: ui-monospace, monospace; text-align:right; }
    .badge { display:inline-block; padding:1px 8px; border-radius:999px; font-size:.7rem;
           font-weight:700; vertical-align:middle; }
    .badge.sync { background:#0c4a6e; color:#bae6fd; }
    .badge.off { background:#7c2d12; color:#fed7aa; }
    .badge.local { background:#3f3f46; color:#e4e4e7; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>📝 備忘録 To Do</h1>
    <p class="sub">Flask + Turso (libSQL) Embedded Replica / Sync 方式 — PEP 723 single-file app</p>

    {% with messages = get_flashed_messages() %}
      {% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}
    {% endwith %}

    <form class="add" action="{{ url_for('add') }}" method="post">
      <input type="text" name="title" placeholder="やることを入力…" autofocus required maxlength="500">
      <button type="submit">追加</button>
    </form>

    <div class="bar">
      <span>未完了 {{ todos | rejectattr('done') | list | length }} 件 / 全 {{ todos | length }} 件</span>
      <span class="source">
        {% if sync_mode and not degraded %}
          <span class="badge sync">SYNC</span> {{ sync_url }}<br>local: {{ local_path }}
        {% elif sync_mode and degraded %}
          <span class="badge off">SYNC・オフライン</span> {{ local_path }}（リモート未接続）
        {% else %}
          <span class="badge local">LOCAL</span> {{ local_path }}（同期なし）
        {% endif %}
      </span>
    </div>

    {% if todos %}
    <ul class="list">
      {% for t in todos %}
      <li class="item {{ 'done' if t.done else '' }}">
        <form class="inline" action="{{ url_for('toggle', todo_id=t.id) }}" method="post">
          <input class="check" type="checkbox" onchange="this.form.submit()"
                 {{ 'checked' if t.done else '' }} title="完了/未完了を切り替え">
        </form>
        <span class="title">
          {{ t.title }}
          <br><span class="meta">追加: {{ t.created_at }}</span>
        </span>
        <form class="inline" action="{{ url_for('delete', todo_id=t.id) }}" method="post"
              onsubmit="return confirm('削除しますか？');">
          <button class="btn-ghost btn-danger" type="submit" title="削除">削除</button>
        </form>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="empty">まだ項目がありません。上のフォームから追加してください。</p>
    {% endif %}
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
def _rows_to_todos(rows):
    """libSQL の行（タプル）を扱いやすい辞書のリストへ変換する。

    SELECT の列順: id, title, done, created_at, updated_at
    """
    todos = []
    for row in rows:
        todos.append(
            {
                "id": row[0],
                "title": row[1],
                "done": bool(row[2]),
                "created_at": row[3],
                "updated_at": row[4],
            }
        )
    return todos


@app.get("/")
def index():
    # 読み取り前にリモートの最新をプル（Sync 方式）。
    with get_db(sync_before=True) as conn:
        rows = conn.execute(
            "SELECT id, title, done, created_at, updated_at "
            "FROM todos ORDER BY done ASC, created_at DESC"
        ).fetchall()
    return render_template_string(
        PAGE,
        todos=_rows_to_todos(rows),
        sync_mode=SYNC_MODE,
        degraded=_degraded,
        sync_url=SYNC_URL,
        local_path=LOCAL_PATH,
    )


@app.post("/add")
def add():
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("内容が空です。")
        return redirect(url_for("index"))
    with get_db() as conn:
        conn.execute("INSERT INTO todos (title) VALUES (?)", (title,))
        conn.commit()
        _safe_sync(conn, "追加後の同期")
    flash("追加しました。")
    return redirect(url_for("index"))


@app.post("/toggle/<int:todo_id>")
def toggle(todo_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT done FROM todos WHERE id = ?", (todo_id,)
        ).fetchone()
        if row is None:
            abort(404)
        new_done = 0 if row[0] else 1
        conn.execute(
            "UPDATE todos SET done = ?, updated_at = datetime('now') WHERE id = ?",
            (new_done, todo_id),
        )
        conn.commit()
        _safe_sync(conn, "更新後の同期")
    return redirect(url_for("index"))


@app.post("/delete/<int:todo_id>")
def delete(todo_id):
    with get_db() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        conn.commit()
        _safe_sync(conn, "削除後の同期")
    flash("削除しました。")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print(f" * 備忘録 To Do を起動します  ->  http://{host}:{port}")
    if SYNC_MODE:
        # 接続を確立してモード（同期 / 縮退）を確定させてから表示する。
        with get_db():
            pass
        if _degraded:
            print(f" * DB: Sync 方式だがリモート未接続 → ローカル縮退 (local={LOCAL_PATH})")
        else:
            print(f" * DB: Sync 方式 (remote={SYNC_URL}, local={LOCAL_PATH})")
    else:
        print(f" * DB: ローカル単体 (local={LOCAL_PATH}, 同期なし)")
    # debug の自動リロードは Embedded Replica の二重オープンを避けるため使わない。
    # threaded=True でも単一接続を _db_lock で直列化するため安全。
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)

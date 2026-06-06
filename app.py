from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from datetime import datetime, date, timedelta
import sqlite3, os, pytz

JST = pytz.timezone('Asia/Tokyo')

def now_jst():
    return datetime.now(JST)

def today_jst():
    return now_jst().date().isoformat()

def time_jst():
    return now_jst().strftime("%H:%M")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "seat-booking-secret-2025")
CORS(app)
DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "seats.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")

# カテゴリの定義
CATEGORIES = {
    "window":   "窓際",
    "no_phone": "電話なし",
    "other":    "おまかせ",
}

# デフォルト席（初回のみ投入）
DEFAULT_SEATS = [
    {"label": "1",  "category": "other"},
    {"label": "2",  "category": "other"},
    {"label": "3",  "category": "other"},
    {"label": "4",  "category": "other"},
    {"label": "5",  "category": "other"},
    {"label": "6",  "category": "window"},
    {"label": "7",  "category": "window"},
    {"label": "8",  "category": "window"},
    {"label": "9",  "category": "window"},
    {"label": "10", "category": "no_phone"},
    {"label": "11", "category": "no_phone"},
    {"label": "12", "category": "no_phone"},
    {"label": "13", "category": "other"},
    {"label": "14", "category": "other"},
    {"label": "15", "category": "other"},
]

# ── DB ──────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = get_db()

    # bookings テーブル
    con.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            seat_id    INTEGER NOT NULL,
            user       TEXT NOT NULL,
            date       TEXT NOT NULL,
            start_time TEXT NOT NULL DEFAULT '00:00',
            end_time   TEXT NOT NULL,
            cancelled  INTEGER NOT NULL DEFAULT 0
        )
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(bookings)").fetchall()]
    if "start_time" not in cols:
        con.execute("ALTER TABLE bookings ADD COLUMN start_time TEXT NOT NULL DEFAULT '00:00'")

    # seats テーブル（category カラム付き）
    con.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            label    TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other'
        )
    """)

    # 旧スキーマ（features カラム）からの移行
    seat_cols = [r[1] for r in con.execute("PRAGMA table_info(seats)").fetchall()]
    if "features" in seat_cols and "category" not in seat_cols:
        con.execute("ALTER TABLE seats ADD COLUMN category TEXT NOT NULL DEFAULT 'other'")
        # features → category 変換
        rows = con.execute("SELECT id, features FROM seats").fetchall()
        for r in rows:
            feats = r["features"].split(",")
            if "window" in feats:
                cat = "window"
            elif "phone" not in feats:
                cat = "no_phone"
            else:
                cat = "other"
            con.execute("UPDATE seats SET category=? WHERE id=?", (cat, r["id"]))

    if con.execute("SELECT COUNT(*) FROM seats").fetchone()[0] == 0:
        for s in DEFAULT_SEATS:
            con.execute(
                "INSERT INTO seats (label, category) VALUES (?,?)",
                (s["label"], s["category"])
            )

    con.commit()
    con.close()

def get_seats():
    con = get_db()
    rows = con.execute("SELECT id, label, category FROM seats ORDER BY id").fetchall()
    con.close()
    return [{"id": r["id"], "label": r["label"], "category": r["category"]} for r in rows]

# ── 予約ヘルパー ───────────────────────────────────

def has_overlap(con, seat_id, date_str, start_time, end_time, exclude_id=None):
    q = """
        SELECT id FROM bookings
        WHERE seat_id=? AND date=? AND cancelled=0
          AND start_time < ? AND end_time > ?
    """
    params = [seat_id, date_str, end_time, start_time]
    if exclude_id:
        q += " AND id != ?"
        params.append(exclude_id)
    return con.execute(q, params).fetchone() is not None

def get_yesterday_seat(con, user, date_str):
    """前日にそのユーザーが使用した席IDを返す（なければNone）"""
    yesterday = (date.fromisoformat(date_str) - timedelta(days=1)).isoformat()
    row = con.execute(
        "SELECT seat_id FROM bookings WHERE user=? AND date=? AND cancelled=0 LIMIT 1",
        (user, yesterday)
    ).fetchone()
    return row["seat_id"] if row else None

# ── ユーザー向けルート ─────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", seats=get_seats(), categories=CATEGORIES)

@app.route("/api/seats")
def api_seats():
    return jsonify(get_seats())

@app.route("/api/bookings")
def get_bookings():
    today = today_jst()
    now   = time_jst()
    target_date = request.args.get("date", "")
    con = get_db()
    if target_date:
        rows = con.execute(
            "SELECT * FROM bookings WHERE date=? AND cancelled=0 ORDER BY start_time",
            (target_date,)
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT * FROM bookings WHERE cancelled=0
               AND (date > ? OR (date = ? AND end_time > ?))
               ORDER BY date, start_time""",
            (today, today, now)
        ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/auto_book", methods=["POST"])
def auto_book():
    """希望カテゴリに基づいて自動で席を割り当てる"""
    data       = request.json
    user       = data.get("user", "").strip()
    preference = data.get("preference", "other")  # "window" / "no_phone" / "other"
    start_time = data.get("start_time", "").strip()
    end_time   = data.get("end_time", "").strip()
    today      = today_jst()
    book_date  = data.get("date", today)

    if not user:
        return jsonify({"error": "名前を入力してください"}), 400
    try:
        datetime.strptime(start_time, "%H:%M")
        datetime.strptime(end_time,   "%H:%M")
    except ValueError:
        return jsonify({"error": "時刻の形式が正しくありません"}), 400
    if start_time >= end_time:
        return jsonify({"error": "終了時刻は開始時刻より後にしてください"}), 400

    con = get_db()

    # すでに当日予約済みか確認
    existing = con.execute(
        "SELECT seat_id FROM bookings WHERE user=? AND date=? AND cancelled=0",
        (user, book_date)
    ).fetchone()
    if existing:
        con.close()
        return jsonify({"error": "すでにこの日の予約があります"}), 409

    # 前日使用した席
    yesterday_seat = get_yesterday_seat(con, user, book_date)

    # 全席：希望カテゴリを先頭に、それ以外を後ろに並べる
    all_seats = get_seats()
    preferred  = [s for s in all_seats if s["category"] == preference]
    fallback   = [s for s in all_seats if s["category"] != preference]
    ordered    = preferred + fallback

    assigned = None
    for seat in ordered:
        if seat["id"] == yesterday_seat:
            continue
        if has_overlap(con, seat["id"], book_date, start_time, end_time):
            continue
        assigned = seat
        break

    if not assigned:
        con.close()
        return jsonify({"error": "空席が見つかりませんでした（前日使用の席を除く）"}), 409

    con.execute(
        "INSERT INTO bookings (seat_id, user, date, start_time, end_time) VALUES (?,?,?,?,?)",
        (assigned["id"], user, book_date, start_time, end_time)
    )
    con.commit()
    con.close()
    return jsonify({"ok": True, "seat": assigned})

@app.route("/api/extend", methods=["POST"])
def extend():
    data     = request.json
    seat_id  = int(data["seat_id"])
    user     = data["user"].strip()
    end_time = data["end_time"]
    today    = today_jst()

    try:
        datetime.strptime(end_time, "%H:%M")
    except ValueError:
        return jsonify({"error": "時刻の形式が正しくありません"}), 400

    con = get_db()
    row = con.execute(
        "SELECT id, start_time FROM bookings WHERE seat_id=? AND date=? AND user=? AND cancelled=0",
        (seat_id, today, user)
    ).fetchone()
    if not row:
        con.close()
        return jsonify({"error": "予約が見つかりません"}), 404

    if has_overlap(con, seat_id, today, row["start_time"], end_time, exclude_id=row["id"]):
        con.close()
        return jsonify({"error": "延長後の時間帯に別の予約があります"}), 409

    con.execute("UPDATE bookings SET end_time=? WHERE id=?", (end_time, row["id"]))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/cancel", methods=["POST"])
def cancel():
    data    = request.json
    seat_id = int(data["seat_id"])
    user    = data["user"].strip()
    today   = today_jst()

    con = get_db()
    result = con.execute(
        "UPDATE bookings SET cancelled=1 WHERE seat_id=? AND date=? AND user=? AND cancelled=0",
        (seat_id, today, user)
    )
    con.commit()
    affected = result.rowcount
    con.close()

    if affected == 0:
        return jsonify({"error": "予約が見つかりません（名前が違う可能性があります）"}), 404
    return jsonify({"ok": True})

# ── 管理者ルート ───────────────────────────────────

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_index"))
        error = "パスワードが違います"
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_index():
    return render_template("admin.html", seats=get_seats(), categories=CATEGORIES)

@app.route("/admin/api/seats", methods=["GET"])
@admin_required
def admin_api_seats():
    return jsonify(get_seats())

@app.route("/admin/api/add_seat", methods=["POST"])
@admin_required
def admin_add_seat():
    data     = request.json or {}
    label    = data.get("label", "").strip()
    category = data.get("category", "other")
    if category not in CATEGORIES:
        return jsonify({"error": "不正なカテゴリです"}), 400
    if not label:
        return jsonify({"error": "席番号を入力してください"}), 400

    con = get_db()
    cur = con.execute(
        "INSERT INTO seats (label, category) VALUES (?,?)",
        (label, category)
    )
    new_id = cur.lastrowid
    con.commit()
    con.close()
    return jsonify({"ok": True, "id": new_id, "label": label, "category": category})

@app.route("/admin/api/update_seat", methods=["POST"])
@admin_required
def admin_update_seat():
    data     = request.json or {}
    seat_id  = data.get("id")
    label    = data.get("label", "").strip()
    category = data.get("category", "other")

    if not seat_id:
        return jsonify({"error": "席IDが必要です"}), 400
    if category not in CATEGORIES:
        return jsonify({"error": "不正なカテゴリです"}), 400
    if not label:
        return jsonify({"error": "席番号を入力してください"}), 400

    con = get_db()
    con.execute(
        "UPDATE seats SET label=?, category=? WHERE id=?",
        (label, category, seat_id)
    )
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/admin/api/delete_seat", methods=["POST"])
@admin_required
def admin_delete_seat():
    data    = request.json or {}
    seat_id = data.get("id")
    if not seat_id:
        return jsonify({"error": "席IDが必要です"}), 400

    con = get_db()
    # 今日以降の予約がある席は削除不可
    today = today_jst()
    active = con.execute(
        "SELECT id FROM bookings WHERE seat_id=? AND date>=? AND cancelled=0",
        (seat_id, today)
    ).fetchone()
    if active:
        con.close()
        return jsonify({"error": "この席には有効な予約があるため削除できません"}), 409
    con.execute("DELETE FROM seats WHERE id=?", (seat_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)

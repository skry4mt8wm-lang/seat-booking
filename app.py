from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from datetime import datetime, date
import sqlite3, os

app = Flask(__name__)
CORS(app)  # Flutter アプリからのクロスオリジンリクエストを許可
DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "seats.db"))

SEATS = [
    {"id": 1,  "label": "1",  "features": ["phone"]},
    {"id": 2,  "label": "2",  "features": ["phone"]},
    {"id": 3,  "label": "3",  "features": ["phone"]},
    {"id": 4,  "label": "4",  "features": []},
    {"id": 5,  "label": "5",  "features": []},
    {"id": 6,  "label": "6",  "features": ["window"]},
    {"id": 7,  "label": "7",  "features": ["window"]},
    {"id": 8,  "label": "8",  "features": ["window"]},
    {"id": 9,  "label": "9",  "features": ["window"]},
    {"id": 10, "label": "10", "features": []},
    {"id": 11, "label": "11", "features": []},
    {"id": 12, "label": "12", "features": []},
    {"id": 13, "label": "13", "features": ["leader"]},
    {"id": 14, "label": "14", "features": ["leader"]},
    {"id": 15, "label": "15", "features": ["leader"]},
]

def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = get_db()
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
    # 既存DBにstart_timeカラムがなければ追加
    cols = [r[1] for r in con.execute("PRAGMA table_info(bookings)").fetchall()]
    if "start_time" not in cols:
        con.execute("ALTER TABLE bookings ADD COLUMN start_time TEXT NOT NULL DEFAULT '00:00'")

    con.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            id       INTEGER PRIMARY KEY,
            label    TEXT NOT NULL,
            features TEXT NOT NULL DEFAULT ''
        )
    """)
    if con.execute("SELECT COUNT(*) FROM seats").fetchone()[0] == 0:
        for s in SEATS:
            con.execute(
                "INSERT INTO seats (id, label, features) VALUES (?,?,?)",
                (s["id"], s["label"], ",".join(s["features"]))
            )
    con.commit()
    con.close()

def get_seats():
    con = get_db()
    rows = con.execute("SELECT * FROM seats ORDER BY id").fetchall()
    con.close()
    return [{"id": r["id"], "label": r["label"],
             "features": [f for f in r["features"].split(",") if f]} for r in rows]

def has_overlap(con, seat_id, date_str, start_time, end_time, exclude_id=None):
    """指定した時間帯と重複する予約があるか確認する"""
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

@app.route("/")
def index():
    return render_template("index.html", seats=get_seats())

@app.route("/api/seats")
def api_seats():
    return jsonify(get_seats())

@app.route("/api/add_seat", methods=["POST"])
def add_seat():
    data     = request.json or {}
    label    = data.get("label", "").strip()
    features = [f.strip() for f in data.get("features", []) if f.strip()]

    con = get_db()
    max_id = con.execute("SELECT MAX(id) FROM seats").fetchone()[0] or 0
    new_id = max_id + 1
    if not label:
        label = str(new_id)
    con.execute(
        "INSERT INTO seats (id, label, features) VALUES (?,?,?)",
        (new_id, label, ",".join(features))
    )
    con.commit()
    con.close()
    return jsonify({"ok": True, "id": new_id, "label": label, "features": features})

@app.route("/api/bookings")
def get_bookings():
    today = date.today().isoformat()
    now   = datetime.now().strftime("%H:%M")
    target_date = request.args.get("date", "")
    con = get_db()
    if target_date:
        # 指定日の全予約（過去分も含む）
        rows = con.execute(
            "SELECT * FROM bookings WHERE date=? AND cancelled=0 ORDER BY start_time",
            (target_date,)
        ).fetchall()
    else:
        # 今日の、まだ終わっていない予約＋今日以降の未来の予約
        rows = con.execute(
            """SELECT * FROM bookings WHERE cancelled=0
               AND (date > ? OR (date = ? AND end_time > ?))
               ORDER BY date, start_time""",
            (today, today, now)
        ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/book", methods=["POST"])
def book():
    data       = request.json
    seat_id    = int(data["seat_id"])
    user       = data["user"].strip()
    start_time = data.get("start_time", "").strip()
    end_time   = data["end_time"]
    today      = date.today().isoformat()
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
    if has_overlap(con, seat_id, book_date, start_time, end_time):
        con.close()
        return jsonify({"error": "その時間帯はすでに予約されています"}), 409

    previously_used = con.execute(
        "SELECT id FROM bookings WHERE seat_id=? AND user=?",
        (seat_id, user)
    ).fetchone()
    if previously_used:
        con.close()
        return jsonify({"error": "以前に使用した席は再予約できません"}), 409

    con.execute(
        "INSERT INTO bookings (seat_id, user, date, start_time, end_time) VALUES (?,?,?,?,?)",
        (seat_id, user, book_date, start_time, end_time)
    )
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/extend", methods=["POST"])
def extend():
    data       = request.json
    seat_id    = int(data["seat_id"])
    user       = data["user"].strip()
    end_time   = data["end_time"]
    today      = date.today().isoformat()

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

    con.execute(
        "UPDATE bookings SET end_time=? WHERE id=?",
        (end_time, row["id"])
    )
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/cancel", methods=["POST"])
def cancel():
    data    = request.json
    seat_id = int(data["seat_id"])
    user    = data["user"].strip()
    today   = date.today().isoformat()

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

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)

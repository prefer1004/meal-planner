import os
import sqlite3
import calendar
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

# (선택) PDF 생성용
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# =========================
# DB (SQLite) 설정
# =========================
DB_PATH = "meals.db"


def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def table_columns(conn, table_name: str):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    cols = [r[1] for r in cur.fetchall()]  # name
    return cols


def init_db():
    """
    1) users 테이블 생성
    2) meals 테이블을 "user_name + meal_date" 복합키로 생성
    3) 기존 1인용(meal_date PK) 구조라면 자동 마이그레이션:
       - 기존 meals 데이터를 user_name='나'로 복사
    """
    conn = get_conn()
    cur = conn.cursor()

    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_name TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    # 기본 사용자
    cur.execute("""
        INSERT OR IGNORE INTO users(user_name, created_at)
        VALUES ('나', ?)
    """, (datetime.now().isoformat(timespec="seconds"),))

    # meals 테이블 존재 여부 확인
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meals'")
    has_meals = cur.fetchone() is not None

    if not has_meals:
        # 새로 생성(멀티유저 구조)
        cur.execute("""
            CREATE TABLE meals (
                user_name TEXT NOT NULL,
                meal_date TEXT NOT NULL,
                breakfast TEXT,
                lunch TEXT,
                dinner TEXT,
                snack TEXT,
                memo TEXT,
                updated_at TEXT,
                PRIMARY KEY (user_name, meal_date)
            )
        """)
        conn.commit()
        conn.close()
        return

    # 기존 meals가 있으면 컬럼 체크
    cols = table_columns(conn, "meals")
    if "user_name" in cols:
        # 이미 멀티유저 구조
        conn.commit()
        conn.close()
        return

    # ===== 마이그레이션: 1인용 구조 -> 멀티유저 구조 =====
    # 기존 구조: meal_date PK + breakfast, lunch, dinner, snack, memo, updated_at
    # 새 구조로 옮기고, 기존 테이블 교체
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meals_new (
            user_name TEXT NOT NULL,
            meal_date TEXT NOT NULL,
            breakfast TEXT,
            lunch TEXT,
            dinner TEXT,
            snack TEXT,
            memo TEXT,
            updated_at TEXT,
            PRIMARY KEY (user_name, meal_date)
        )
    """)

    # 기존 데이터 복사(기본 사용자 '나')
    # 기존 테이블에 snack/memo/updated_at 없을 수도 있으니 안전하게 처리
    base_cols = ["meal_date", "breakfast", "lunch", "dinner", "snack", "memo", "updated_at"]
    existing = set(cols)
    select_cols = [c for c in base_cols if c in existing]

    if "meal_date" in existing:
        select_sql = ", ".join(select_cols)
        cur.execute(f"SELECT {select_sql} FROM meals")
        rows = cur.fetchall()

        # rows를 dict 형태로 맞춰 insert
        for r in rows:
            row_map = dict(zip(select_cols, r))
            cur.execute("""
                INSERT OR REPLACE INTO meals_new
                (user_name, meal_date, breakfast, lunch, dinner, snack, memo, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "나",
                row_map.get("meal_date"),
                row_map.get("breakfast", ""),
                row_map.get("lunch", ""),
                row_map.get("dinner", ""),
                row_map.get("snack", ""),
                row_map.get("memo", ""),
                row_map.get("updated_at", "")
            ))

    # 기존 테이블 교체
    cur.execute("DROP TABLE meals")
    cur.execute("ALTER TABLE meals_new RENAME TO meals")

    conn.commit()
    conn.close()


# =========================
# 사용자 CRUD
# =========================
def list_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_name FROM users ORDER BY user_name")
    users = [r[0] for r in cur.fetchall()]
    conn.close()
    return users


def add_user(user_name: str):
    user_name = (user_name or "").strip()
    if not user_name:
        return False, "이름이 비어있습니다."
    if len(user_name) > 20:
        return False, "이름이 너무 깁니다(20자 이하)."

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO users(user_name, created_at)
        VALUES (?, ?)
    """, (user_name, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    inserted = (cur.rowcount == 1)
    conn.close()

    if inserted:
        return True, "추가 완료"
    return False, "이미 존재하는 사용자입니다."


def user_has_meals(user_name: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM meals WHERE user_name=? LIMIT 1", (user_name,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def delete_user(user_name: str, delete_meals: bool = False):
    user_name = (user_name or "").strip()
    if not user_name:
        return False, "사용자 이름이 비어있습니다."
    if user_name == "나":
        return False, "'나' 사용자는 삭제할 수 없게 막아뒀습니다."

    conn = get_conn()
    cur = conn.cursor()
    if delete_meals:
        cur.execute("DELETE FROM meals WHERE user_name=?", (user_name,))
    cur.execute("DELETE FROM users WHERE user_name=?", (user_name,))
    conn.commit()
    conn.close()
    return True, "삭제 완료"


# =========================
# meals CRUD (사용자별)
# =========================
def upsert_meal(user_name: str, meal_date: str, breakfast: str, lunch: str, dinner: str, snack: str, memo: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO meals(user_name, meal_date, breakfast, lunch, dinner, snack, memo, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_name, meal_date) DO UPDATE SET
            breakfast=excluded.breakfast,
            lunch=excluded.lunch,
            dinner=excluded.dinner,
            snack=excluded.snack,
            memo=excluded.memo,
            updated_at=excluded.updated_at
    """, (
        user_name, meal_date,
        breakfast, lunch, dinner, snack, memo,
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()
    conn.close()


def load_meal(user_name: str, meal_date: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT breakfast, lunch, dinner, snack, memo
        FROM meals
        WHERE user_name=? AND meal_date=?
    """, (user_name, meal_date))
    row = cur.fetchone()
    conn.close()
    return row


def load_month(user_name: str, year: int, month: int) -> pd.DataFrame:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    conn = get_conn()
    df = pd.read_sql_query(
        """
        SELECT meal_date, breakfast, lunch, dinner, snack, memo, updated_at
        FROM meals
        WHERE user_name=? AND meal_date >= ? AND meal_date < ?
        ORDER BY meal_date
        """,
        conn,
        params=(user_name, start.isoformat(), end.isoformat())
    )
    conn.close()
    return df


def week_dates(monday: date):
    return [monday + timedelta(days=i) for i in range(7)]


# =========================
# (선택) 주간 PDF 생성
# =========================
def make_week_pdf(user_name: str, monday: date, df_week: pd.DataFrame, out_path: str):
    os.makedirs("output", exist_ok=True)

    if os.path.exists("font.ttf"):
        pdfmetrics.registerFont(TTFont("Nanum", "font.ttf"))
        font_name = "Nanum"
    else:
        font_name = "Helvetica"

    c = canvas.Canvas(out_path, pagesize=A4)
    c.setFont(font_name, 16)

    title = f"🍽 {user_name} 주간 식단표 ({monday.isoformat()} ~ {(monday + timedelta(days=6)).isoformat()})"
    c.drawString(40, 805, title)

    y = 770
    c.setFont(font_name, 11)
    c.drawString(40, y, "날짜")
    c.drawString(120, y, "아침")
    c.drawString(270, y, "점심")
    c.drawString(420, y, "저녁")
    y -= 18
    c.line(40, y, 550, y)
    y -= 16

    for d in week_dates(monday):
        day_str = d.isoformat()
        row = df_week[df_week["meal_date"] == day_str]
        b = row["breakfast"].iloc[0] if len(row) else ""
        l = row["lunch"].iloc[0] if len(row) else ""
        dn = row["dinner"].iloc[0] if len(row) else ""

        def short(s, n=18):
            s = (s or "").strip()
            return s[:n] + ("…" if len(s) > n else "")

        c.drawString(40, y, day_str)
        c.drawString(120, y, short(b))
        c.drawString(270, y, short(l))
        c.drawString(420, y, short(dn))
        y -= 18
        if y < 70:
            c.showPage()
            y = 805

    c.save()


# =========================
# 월간 달력 UI 함수
# =========================
def summarize_cell(row: pd.Series) -> str:
    if row is None:
        return ""

    b = (row.get("breakfast") or "").strip()
    l = (row.get("lunch") or "").strip()
    d = (row.get("dinner") or "").strip()
    s = (row.get("snack") or "").strip()
    m = (row.get("memo") or "").strip()

    badges = []
    if b: badges.append("🌞")
    if l: badges.append("🍚")
    if d: badges.append("🌙")
    if s: badges.append("🍪")

    def short(text, n=16):
        text = (text or "").strip()
        return text[:n] + ("…" if len(text) > n else "")

    main = short(d or l or b or "", 18)
    memo = short(m, 18) if m else ""

    line1 = (" ".join(badges) + " " + main).strip() if (badges or main) else ""
    line2 = ("📝 " + memo).strip() if memo else ""

    return (line1 + ("\n" + line2 if line2 else "")).strip()


def render_month_calendar(year: int, month: int, df_month_raw: pd.DataFrame):
    data_map = {}
    if not df_month_raw.empty:
        for _, r in df_month_raw.iterrows():
            data_map[r["meal_date"]] = r

    weeks = calendar.monthcalendar(year, month)

    headers = ["월", "화", "수", "목", "금", "토", "일"]
    cols = st.columns(7)
    for i, h in enumerate(headers):
        cols[i].markdown(f"**{h}**")

    if "selected_date" not in st.session_state:
        st.session_state.selected_date = None

    for w in weeks:
        row_cols = st.columns(7)
        for i, day in enumerate(w):
            if day == 0:
                row_cols[i].markdown("<div class='cal-empty'></div>", unsafe_allow_html=True)
                continue

            d = date(year, month, day).isoformat()
            row = data_map.get(d)
            summary = summarize_cell(row)

            is_today = (date.today().isoformat() == d)
            day_badge = "📍" if is_today else ""

            # 날짜 선택(사용자별 충돌 방지: key에 날짜만 써도 탭별로 안전하지만, 더 안전하게 month/year 포함)
            if row_cols[i].button(f"{day} {day_badge}", key=f"pick_{year}_{month}_{d}"):
                st.session_state.selected_date = d

            if summary:
                row_cols[i].markdown(
                    f"<div class='cal-card cal-filled'>{summary.replace(chr(10), '<br>')}</div>",
                    unsafe_allow_html=True
                )
            else:
                row_cols[i].markdown("<div class='cal-card'>—</div>", unsafe_allow_html=True)


# =========================
# 앱 시작
# =========================
init_db()

st.set_page_config(page_title="식단 플래너", page_icon="🍱", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
h1, h2, h3 {letter-spacing:-0.3px;}
.small-note {color:#777; font-size:0.9rem;}
.badge {display:inline-block; padding:4px 10px; border-radius:999px; background:#f3f4f6; margin-right:6px; font-size:12px;}

.cal-card{
  border:1px solid #eee;
  border-radius:14px;
  padding:10px;
  min-height:68px;
  background:#fff;
  font-size:13px;
  line-height:1.25rem;
  white-space:normal;
}
.cal-filled{ background:#fafafa; }
.cal-empty{ min-height:68px; }
</style>
""", unsafe_allow_html=True)

st.title("🍱 식단 플래너")
st.markdown('<span class="badge">주간 입력</span><span class="badge">월간 달력</span><span class="badge">PDF(선택)</span>', unsafe_allow_html=True)

# =========================
# 👤 사용자 영역(선택/추가/삭제)
# =========================
st.markdown("### 👤 사용자")

users = list_users()
if "current_user" not in st.session_state:
    st.session_state.current_user = "나" if "나" in users else (users[0] if users else "나")

u1, u2, u3 = st.columns([1.1, 1.1, 1.6])

with u1:
    # 현재 사용자 선택
    users = list_users()
    if st.session_state.current_user not in users:
        st.session_state.current_user = "나" if "나" in users else users[0]
    current_user = st.selectbox("사용자 선택", users, index=users.index(st.session_state.current_user), key="user_select")
    st.session_state.current_user = current_user

with u2:
    # 사용자 추가
    new_user = st.text_input("새 사용자 이름", placeholder="예: 남편, 아내, 아이", key="new_user_name")
    if st.button("➕ 사용자 생성"):
        ok, msg = add_user(new_user)
        if ok:
            st.success(f"{new_user.strip()} / {msg}")
            st.session_state.current_user = new_user.strip()
            st.rerun()
        else:
            st.error(msg)

with u3:
    # 라벨을 다른 칸과 동일한 위젯 라벨 스타일로 맞추기 위해
    # "삭제 섹션"도 selectbox 라벨을 대표 라벨처럼 사용
    deletable = [u for u in list_users() if u != "나"]

    # 1) 삭제할 사용자 선택(라벨이 곧 "사용자 삭제" 제목 역할)
    if not deletable:
        st.selectbox("🗑 사용자 삭제", ["(삭제 가능한 사용자 없음)"], disabled=True, key="del_user_disabled")
        st.info("삭제 가능한 사용자가 없습니다. (기본 사용자 '나'는 삭제 불가)")
    else:
        del_user = st.selectbox("🗑 사용자 삭제", deletable, key="del_user_select")

        has_data = user_has_meals(del_user)
        del_meals = st.checkbox(
            "이 사용자의 식단 데이터도 함께 삭제",
            value=False,
            key="del_meals"
        )

        if has_data and not del_meals:
            st.warning("사용자만 삭제하면 식단 데이터는 DB에 남습니다. (같은 이름으로 다시 만들면 기록이 보일 수 있어요)")

        # 2) 확인 입력
        confirm = st.text_input(
            "확인 입력(삭제할 이름 그대로)",
            key="del_confirm",
            placeholder=del_user
        )

        # 3) 삭제 버튼
        if st.button("❌ 삭제", disabled=(confirm.strip() != del_user), key="del_go"):
            ok, msg = delete_user(del_user, delete_meals=del_meals)
            if ok:
                st.success(f"{del_user} / {msg}")
                st.session_state.current_user = "나"
                st.rerun()
            else:
                st.error(msg)


st.divider()

tab1, tab2 = st.tabs(["🗓️ 주간 입력", "📅 월간 달력"])


# -------------------------
# TAB 1: 주간 입력
# -------------------------
with tab1:
    left, right = st.columns([1.2, 1])

    with left:
        st.subheader(f"🗓️ {current_user} 이번 주 식단 입력")
        st.markdown('<div class="small-note">사용자별로 저장되며, 같은 날짜는 다시 저장하면 덮어쓰기 됩니다.</div>', unsafe_allow_html=True)

        today = date.today()
        monday = today - timedelta(days=today.weekday())
        monday_sel = st.date_input("주 시작(월요일) 선택", value=monday, key="week_monday")
        monday_sel = monday_sel - timedelta(days=monday_sel.weekday())
        days = week_dates(monday_sel)

        dow_emoji = ["🌙 月", "🔥 火", "💧 水", "🌳 木", "💰 金", "🌿 土", "☀️ 日"]

        for i, d in enumerate(days):
            d_str = d.isoformat()
            existing = load_meal(current_user, d_str)

            default_b = existing[0] if existing else ""
            default_l = existing[1] if existing else ""
            default_dn = existing[2] if existing else ""
            default_s = existing[3] if existing else ""
            default_m = existing[4] if existing else ""

            with st.expander(f"{dow_emoji[i]}  {d_str}", expanded=(d == today)):
                c1, c2 = st.columns(2)
                with c1:
                    b = st.text_input("🌞 아침", value=default_b, key=f"b_{current_user}_{d_str}")
                    l = st.text_input("🍚 점심", value=default_l, key=f"l_{current_user}_{d_str}")
                with c2:
                    dn = st.text_input("🌙 저녁", value=default_dn, key=f"d_{current_user}_{d_str}")
                    s = st.text_input("🍪 간식", value=default_s, key=f"s_{current_user}_{d_str}")

                memo = st.text_input("📝 메모(선택)", value=default_m, key=f"m_{current_user}_{d_str}")

                if st.button("✅ 저장", key=f"save_{current_user}_{d_str}"):
                    upsert_meal(current_user, d_str, b, l, dn, s, memo)
                    st.success(f"{current_user} / {d_str} 저장 완료!")

    with right:
        st.subheader(f"📄 {current_user} 주간 요약")
        st.markdown('<div class="small-note">저장된 내용을 표로 확인하고, 원하면 PDF로 출력할 수 있어요.</div>', unsafe_allow_html=True)

        start = monday_sel
        end = monday_sel + timedelta(days=7)

        conn = get_conn()
        df_week = pd.read_sql_query(
            """
            SELECT meal_date, breakfast, lunch, dinner, snack, memo
            FROM meals
            WHERE user_name=? AND meal_date >= ? AND meal_date < ?
            ORDER BY meal_date
            """,
            conn,
            params=(current_user, start.isoformat(), end.isoformat())
        )
        conn.close()

        preview = pd.DataFrame({"meal_date": [d.isoformat() for d in days]})
        preview = preview.merge(df_week, on="meal_date", how="left").fillna("")

        show_df = preview.rename(columns={
            "meal_date": "날짜",
            "breakfast": "🌞아침",
            "lunch": "🍚점심",
            "dinner": "🌙저녁",
            "snack": "🍪간식",
            "memo": "📝메모"
        })

        st.dataframe(show_df, use_container_width=True, hide_index=True)

        st.markdown("#### 🖨️ 주간 PDF(선택)")
        if st.button("📄 주간 PDF 만들기", key="make_pdf"):
            out_path = f"output/{current_user}_주간식단_{monday_sel.isoformat()}.pdf"
            # make_week_pdf는 df_week에서 meal_date 기준으로 읽음 -> preview 형태로 전달
            make_week_pdf(current_user, monday_sel, preview.rename(columns={"날짜": "meal_date"}), out_path)
            with open(out_path, "rb") as f:
                st.download_button("✅ PDF 다운로드", f, file_name=os.path.basename(out_path), mime="application/pdf")


# -------------------------
# TAB 2: 월간 달력 + 선택 날짜 편집
# -------------------------
with tab2:
    st.subheader(f"📅 {current_user} 월간 누적 (달력)")
    st.markdown('<div class="small-note">달력에서 날짜를 누르면 아래에서 그 날 식단을 바로 수정할 수 있어요.</div>', unsafe_allow_html=True)

    c1, c2 = st.columns([1, 1])
    with c1:
        year = st.number_input("연도", min_value=2020, max_value=2100, value=date.today().year, key="cal_year")
    with c2:
        month = st.selectbox("월", list(range(1, 13)), index=date.today().month - 1, key="cal_month")

    df = load_month(current_user, int(year), int(month))
    render_month_calendar(int(year), int(month), df)

    st.divider()

    sel = st.session_state.get("selected_date")
    if not sel:
        st.info("달력에서 날짜를 눌러 식단을 확인/수정하세요 🙂")
    else:
        st.subheader(f"✍️ 선택 날짜 편집: {sel}  /  사용자: {current_user}")

        existing = load_meal(current_user, sel)
        default_b = existing[0] if existing else ""
        default_l = existing[1] if existing else ""
        default_dn = existing[2] if existing else ""
        default_s = existing[3] if existing else ""
        default_m = existing[4] if existing else ""

        cc1, cc2 = st.columns(2)
        with cc1:
            b = st.text_input("🌞 아침", value=default_b, key=f"edit_b_{current_user}_{sel}")
            l = st.text_input("🍚 점심", value=default_l, key=f"edit_l_{current_user}_{sel}")
        with cc2:
            dn = st.text_input("🌙 저녁", value=default_dn, key=f"edit_d_{current_user}_{sel}")
            s = st.text_input("🍪 간식", value=default_s, key=f"edit_s_{current_user}_{sel}")

        memo = st.text_input("📝 메모(선택)", value=default_m, key=f"edit_m_{current_user}_{sel}")

        if st.button("✅ 저장", key=f"edit_save_{current_user}_{sel}"):
            upsert_meal(current_user, sel, b, l, dn, s, memo)
            st.success("저장 완료! 달력에 바로 반영됩니다.")
            st.rerun()

"""
KTX 빈자리 감시 & 텔레그램 알림 봇
====================================
watches.json 에 등록된 조건(날짜/시간대/좌석등급)에 맞는 열차에
자리가 나면 텔레그램으로 알려준다. auto_hold=true 인 조건은
자리를 발견하는 즉시 임시 예약(홀드)까지 걸어준다 (결제는 사람이 직접
코레일 앱에서 홀드 시간 안에 완료해야 함 — 카드 정보는 다루지 않음).

⚠️ 코레일 비공식 API(pykorail)를 사용합니다.
   - 회사가 앱 API를 바꾸면 예고 없이 멈출 수 있습니다.
   - 조회 간격을 너무 짧게 하면 계정이 제재될 수 있습니다 (기본 30초 권장).
   - 개인 용도로만 사용하세요. 자동화 프로그램으로 표를 확보해 되파는 행위는
     철도사업법상 금지 대상입니다.
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from pykorail import Korail, NoResultsError, PastDepartureError, SoldOutError, KorailError

BASE_DIR = Path(__file__).resolve().parent
WATCHES_FILE = BASE_DIR / "watches.json"
STATE_FILE = BASE_DIR / "last_state.json"
OFFSET_FILE = BASE_DIR / "tg_offset.txt"

# ---------- 환경변수 로드 (.env 파일 수동 파싱, 외부 의존성 최소화) ----------
def load_env():
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("#")[0].strip())

load_env()

KORAIL_ID = os.environ["KORAIL_ID"]
KORAIL_PW = os.environ["KORAIL_PW"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# --------------------------- 텔레그램 ---------------------------
def tg_send(text: str):
    try:
        requests.post(f"{TG_API}/sendMessage", data={
            "chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True
        }, timeout=10)
    except requests.RequestException as e:
        print(f"[텔레그램 전송 실패] {e}")

def tg_get_commands():
    """새 명령어(/on, /off, /list 등)를 가져온다."""
    offset = 0
    if OFFSET_FILE.exists():
        offset = int(OFFSET_FILE.read_text().strip() or 0)
    try:
        resp = requests.get(f"{TG_API}/getUpdates",
                             params={"offset": offset, "timeout": 0}, timeout=10).json()
    except requests.RequestException as e:
        print(f"[텔레그램 명령 조회 실패] {e}")
        return []
    updates = resp.get("result", [])
    commands = []
    for u in updates:
        offset = u["update_id"] + 1
        msg = u.get("message", {})
        text = msg.get("text", "")
        if str(msg.get("chat", {}).get("id")) == str(TG_CHAT_ID) and text.startswith("/"):
            commands.append(text.strip())
    OFFSET_FILE.write_text(str(offset))
    return commands

# --------------------------- 감시 조건 ---------------------------
def load_watches():
    return json.loads(WATCHES_FILE.read_text(encoding="utf-8"))

def save_watches(watches):
    WATCHES_FILE.write_text(json.dumps(watches, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# --------------------------- 명령 처리 ---------------------------
def handle_commands(watches):
    changed = False
    for cmd in tg_get_commands():
        parts = cmd.split()
        head = parts[0]
        if head == "/list":
            lines = ["현재 감시 조건 목록:"]
            for w in watches:
                state = "🟢 ON" if w["active"] else "⚪️ OFF"
                lines.append(f"[{w['id']}] {state} {w['label']} "
                              f"({w['dep']}→{w['arr']} {w['date']} "
                              f"{w['earliest_time']}~{w['latest_time']})")
            tg_send("\n".join(lines))
        elif head in ("/on", "/off") and len(parts) == 2 and parts[1].isdigit():
            wid = int(parts[1])
            for w in watches:
                if w["id"] == wid:
                    w["active"] = (head == "/on")
                    changed = True
                    tg_send(f"[{wid}] {w['label']} → {'ON' if w['active'] else 'OFF'} 처리했어요.")
        elif head == "/help":
            tg_send("/list - 조건 목록\n/on <id> - 감시 켜기\n/off <id> - 감시 끄기")
    if changed:
        save_watches(watches)

# --------------------------- 좌석 상태 파싱 ---------------------------
# pykorail Train 객체의 문자열 표현 예:
# "[KTX 101]  04/01 09:00~12:30  서울~부산  특실 가능, 일반실 가능 (3시간 30분)"
TRAIN_RE = re.compile(
    r"\[(?P<name>[^\]]+)\]\s+(?P<date>\d{2}/\d{2})\s+"
    r"(?P<dep_time>\d{2}:\d{2})~(?P<arr_time>\d{2}:\d{2})\s+"
    r"(?P<route>\S+~\S+)\s+(?P<seat_info>[^\(]+)"
)

def parse_train(train):
    """train 객체를 규칙적으로 해석. 라이브러리 버전에 따라 내부 속성명이
    바뀔 수 있어, 공식 예시에 나온 __str__ 출력 형식을 기준으로 파싱한다.
    (라이브러리 업데이트로 형식이 바뀌면 이 정규식만 손보면 됨)"""
    m = TRAIN_RE.search(str(train))
    if not m:
        return None
    seat_info = m.group("seat_info")
    general_ok = "일반실 가능" in seat_info
    special_ok = "특실 가능" in seat_info
    return {
        "name": m.group("name"),
        "dep_time": m.group("dep_time"),
        "arr_time": m.group("arr_time"),
        "general_ok": general_ok,
        "special_ok": special_ok,
        "key": f"{m.group('name')}_{m.group('date')}_{m.group('dep_time')}",
    }

def matches_watch(parsed, watch):
    if not (watch["earliest_time"] <= parsed["dep_time"] <= watch["latest_time"]):
        return False
    pref = watch.get("seat_pref", "any")
    if pref == "general":
        return parsed["general_ok"]
    if pref == "special":
        return parsed["special_ok"]
    return parsed["general_ok"] or parsed["special_ok"]  # any

# --------------------------- 코레일 조회 ---------------------------
def check_watch(korail, watch, state):
    date_str = watch["date"].replace("-", "")
    depart_after = datetime.strptime(f"{watch['date']} {watch['earliest_time']}", "%Y-%m-%d %H:%M")
    try:
        trains = korail.trains.search(watch["dep"], watch["arr"], depart_after=depart_after)
    except NoResultsError:
        return
    except PastDepartureError:
        tg_send(f"⚠️ [{watch['id']}] {watch['label']} - 이미 지난 시각이라 감시를 자동으로 껐어요.")
        watch["active"] = False
        return
    except KorailError as e:
        print(f"[조회 오류] {watch['label']}: {e}")
        return

    watch_state = state.setdefault(str(watch["id"]), {})

    # 1) 이번 조회에서 새로 매칭된(아직 안 알린) 열차들을 전부 모은다
    new_matches = []  # [(parsed, train), ...] 출발시각 순
    for train in trains:
        parsed = parse_train(train)
        if not parsed:
            continue
        if parsed["dep_time"] > watch["latest_time"]:
            continue
        if not matches_watch(parsed, watch):
            continue
        if watch_state.get(parsed["key"]) == "notified":
            continue  # 이미 알린 열차는 재알림 생략 (다시 받고 싶으면 last_state.json 삭제)
        new_matches.append((parsed, train))

    if not new_matches:
        return
    new_matches.sort(key=lambda pt: pt[0]["dep_time"])

    # 2) 알림 메시지는 전부 모아서 한 번에 (선택지를 보여줌)
    lines = [f"🚄 자리 발견! [{watch['label']}]"]
    for parsed, _ in new_matches:
        seat_desc = []
        if parsed["general_ok"]:
            seat_desc.append("일반실")
        if parsed["special_ok"]:
            seat_desc.append("특실")
        lines.append(f"- {parsed['name']} {parsed['dep_time']}~{parsed['arr_time']} "
                      f"({', '.join(seat_desc)})")
    lines.append("코레일톡 앱에서 서둘러 예매하세요!")

    # 3) 자동 홀드는 "가장 빠른 열차 하나만" 시도. 성공하면 더 찾을 필요 없으니 감시를 끈다.
    #    (여러 자리를 동시에 홀드해서 낭비하지 않기 위함)
    if watch.get("auto_hold"):
        held = False
        for parsed, train in new_matches:
            try:
                reservation = korail.reservations.create(train)
                lines.append(f"\n✅ {parsed['name']} {parsed['dep_time']} 열차에 "
                              f"임시 예약(홀드)을 걸어뒀어요.\n{reservation}")
                lines.append("※ 정해진 시간 안에 앱에서 결제하지 않으면 자동 취소됩니다. 서둘러주세요!")
                held = True
                break  # 한 건 홀드했으면 나머지는 시도하지 않음
            except SoldOutError:
                continue  # 이 열차는 방금 매진 → 다음 순번 열차로 재시도
            except KorailError as e:
                lines.append(f"\n(홀드 시도 중 오류: {e})")
                break
        if held:
            lines.append(f"\n🔕 [{watch['label']}] 감시는 자동으로 껐어요. "
                          f"결제를 취소했거나 다른 조건으로 다시 찾고 싶으면 텔레그램에서 "
                          f"/on {watch['id']} 을 보내주세요.")
            watch["active"] = False
        elif not held and any(True for _ in new_matches):
            lines.append("\n(모든 후보 열차가 방금 매진되어 홀드는 못 걸었어요. 계속 감시할게요.)")

    tg_send("\n".join(lines))
    for parsed, _ in new_matches:
        watch_state[parsed["key"]] = "notified"

# --------------------------- 한 번 조회하고 끝내기 ---------------------------
def run_once():
    """감시 조건을 한 번 훑고 종료한다. GitHub Actions처럼 '5~15분마다
    실행해주는 스케줄러'가 따로 있는 환경에서 사용한다."""
    watches = load_watches()
    handle_commands(watches)
    state = load_state()

    active = [w for w in watches if w["active"]]
    if active:
        korail = Korail.logged_in(KORAIL_ID, KORAIL_PW)
        with korail:
            for watch in active:
                check_watch(korail, watch, state)

    save_state(state)
    save_watches(watches)  # PastDepartureError 등으로 active 값이 바뀌었을 수 있음

# --------------------------- 계속 켜두고 도는 모드 (PC용) ---------------------------
def run_loop():
    tg_send("🟢 KTX 감시 봇을 시작했어요. /list 로 조건을 확인할 수 있어요.")
    while True:
        run_once()
        time.sleep(POLL_INTERVAL)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                         help="한 번만 조회하고 종료 (GitHub Actions 등 스케줄러 환경용)")
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        run_loop()

if __name__ == "__main__":
    main()

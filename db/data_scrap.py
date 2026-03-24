#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
빙상연맹 쇼트트랙 선수 데이터 수집 프로그램
대상 사이트: https://result.sports.or.kr/SK/
SQLite DB로 저장
"""

import requests
from bs4 import BeautifulSoup
import sqlite3
import re
import time
import logging
import argparse

# ─────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────
import sys
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)),
        logging.FileHandler("scraping.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 상수 / 기본 파라미터
# ─────────────────────────────────────────────
BASE_URL = "https://result.sports.or.kr/SK"

CLASS_CD_SHORTTRACK = "2"

KIND_CD_MAP = {
    "남자중학부":      "02",
    "남자고등부":      "03",
    "남자대학부":      "04",
    "남자일반부":      "05",
    "여자중학부":      "07",
    "여자고등부":      "08",
    "여자대학부":      "09",
    "여자일반부":      "10",
    "남자초등1,2학년": "31",
    "남자초등3,4학년": "32",
    "남자초등5,6학년": "33",
    "여자초등1,2학년": "41",
    "여자초등3,4학년": "42",
    "여자초등5,6학년": "43",
}

DETAIL_CLASS_CD_MAP = {
    "500M":  "20201",
    "1000M": "20203",
    "1500M": "20204",
}

# 라운드 우선순위: 앞에 올수록 먼저 시도 (인덱스 낮을수록 우선)
ROUND_PRIORITY = ["예선", "준준결승", "준결승", "결승B", "결승A", "결승"]

REQUEST_DELAY = 0.5


# ─────────────────────────────────────────────
# HTTP 세션
# ─────────────────────────────────────────────
def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": f"{BASE_URL}/INF201.do",
            "Origin":  "https://result.sports.or.kr",
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    return session


# ─────────────────────────────────────────────
# DB 초기화
# ─────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS player (
            player_id        TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            gender           TEXT,
            birth_year       TEXT,
            kind_nm          TEXT,
            team_nm          TEXT,
            team_cd          TEXT,
            sido             TEXT,
            last_reg_year    TEXT,
            reg_type         TEXT,
            created_at       TEXT DEFAULT (datetime('now','localtime')),
            updated_at       TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS player_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id        TEXT NOT NULL,
            competition_nm   TEXT,
            event_date       TEXT,
            kind_nm          TEXT,
            event_nm         TEXT,
            round_nm         TEXT,
            team_nm          TEXT,
            record           TEXT,
            rank             TEXT,
            UNIQUE(player_id, competition_nm, event_date, kind_nm, event_nm, round_nm),
            FOREIGN KEY (player_id) REFERENCES player(player_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS collect_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            to_cd           TEXT,
            competition_nm  TEXT,
            kind_nm         TEXT,
            event_nm        TEXT,
            round_nm        TEXT,
            total_players   INTEGER,
            new_players     INTEGER,
            skipped_players INTEGER,
            collected_at    TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )

    conn.commit()
    logger.info(f"DB 초기화 완료: {db_path}")
    return conn


# ─────────────────────────────────────────────
# API 호출 함수들
# ─────────────────────────────────────────────
def post_with_retry(session: requests.Session, url: str, data: dict, timeout: int = 15, retries: int = 3) -> requests.Response:
    """타임아웃/연결 오류 시 재시도하는 POST 요청"""
    for attempt in range(retries):
        try:
            resp = session.post(url, data=data, timeout=timeout)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"    연결 오류 (재시도 {attempt+1}/{retries}): {e}")
            time.sleep(5)


def get_event_list(
    session: requests.Session,
    class_cd: str,
    to_cd: str,
) -> list[dict]:
    """INF202: 세부종목 목록 조회"""
    url = f"{BASE_URL}/INF202.do"
    data = {"classCd": class_cd, "toCd": to_cd, "platform": "pc"}
    resp = post_with_retry(session, url, data)
    soup = BeautifulSoup(resp.text, "html.parser")

    events = []
    tables = soup.find_all("table")
    if len(tables) < 2:
        logger.warning("세부종목 테이블을 찾을 수 없음")
        return events

    for row in tables[1].find("tbody").find_all("tr"):
        onclick = row.get("onclick", "")
        m = re.search(r'fnEventSchedule\("([^"]+)","([^"]+)"\)', onclick)
        if not m:
            continue
        cells = row.find_all("td")
        events.append(
            {
                "kind_nm":        cells[0].get_text(strip=True) if len(cells) > 0 else "",
                "kind_cd":        m.group(1),
                "detail_class_nm": cells[1].get_text(strip=True) if len(cells) > 1 else "",
                "detail_class_cd": m.group(2),
            }
        )

    logger.info(f"세부종목 {len(events)}개 조회")
    return events


def get_round_list(
    session: requests.Session,
    class_cd: str,
    to_cd: str,
    kind_cd: str,
    detail_class_cd: str,
) -> list[dict]:
    """INF301: 경기일정(라운드) 목록 조회 (우선순위 순 정렬)"""
    url = f"{BASE_URL}/INF301.do"
    data = {
        "classCd":       class_cd,
        "toCd":          to_cd,
        "kindCd":        kind_cd,
        "detailClassCd": detail_class_cd,
        "platform":      "pc",
    }
    resp = post_with_retry(session, url, data)
    soup = BeautifulSoup(resp.text, "html.parser")

    rounds = []
    tables = soup.find_all("table")
    if len(tables) < 2:
        return rounds

    for row in tables[1].find("tbody").find_all("tr"):
        btn = row.find("button")
        if not btn:
            continue
        onclick = btn.get("onclick", "")
        m = re.search(
            r'fnEventResult\("([^"]*)",\s*"([^"]*)",\s*"([^"]*)",\s*"([^"]*)",\s*"([^"]*)",\s*"([^"]*)"\)',
            onclick,
        )
        if not m:
            continue
        rounds.append(
            {
                "pcnt_gbn":      m.group(2),
                "base_class_cd": m.group(3),
                "rh_cd":         m.group(4),
                "rh_nm":         m.group(5),
                "base_class_nm": m.group(6),
            }
        )

    # ── 우선순위 순 정렬 ──
    def round_priority(r: dict) -> int:
        for i, keyword in enumerate(ROUND_PRIORITY):
            if keyword in r["rh_nm"]:
                return i
        return 999

    rounds.sort(key=round_priority)
    return rounds


def get_players_from_result(
    session: requests.Session,
    class_cd: str,
    to_cd: str,
    kind_cd: str,
    detail_class_cd: str,
    rh_cd: str,
    base_class_cd: str,
    rh_nm: str,
    base_class_nm: str,
    pcnt_gbn: str = "I",
) -> list[dict]:
    """INF310: 경기결과에서 선수 목록 조회"""
    url = f"{BASE_URL}/INF310.do"
    data = {
        "classCd":            class_cd,
        "toCd":               to_cd,
        "kindCd":             kind_cd,
        "idNo":               "",
        "rhCd":               rh_cd,
        "detailClassCd":      detail_class_cd,
        "baseClassCd":        base_class_cd,
        "teamCd":             "",
        "vsTeamCd":           "",
        "pcntGbn":            pcnt_gbn,
        "baseClassNm":        base_class_nm,
        "rhNm":               rh_nm,
        "searchKindCd":       kind_cd,
        "searchDetailClassCd": detail_class_cd,
        "platform":           "pc",
    }
    resp = post_with_retry(session, url, data)
    soup = BeautifulSoup(resp.text, "html.parser")

    players = []
    for a_tag in soup.find_all("a", href=re.compile(r"fnViewHistory")):
        m = re.search(r"fnViewHistory\('([^']+)'\)", a_tag.get("href", ""))
        if not m:
            continue
        player_id = m.group(1)
        name = a_tag.get_text(strip=True)
        row = a_tag.find_parent("tr")
        team_nm = ""
        if row:
            cells = row.find_all("td")
            if len(cells) >= 4:
                team_nm = cells[3].get_text(strip=True)
        players.append({"player_id": player_id, "name": name, "team_nm": team_nm})

    return players


# ★ 신규 추가 함수 ───────────────────────────────────────────────────────
def find_first_valid_round(
    session: requests.Session,
    class_cd: str,
    to_cd: str,
    kind_cd: str,
    detail_class_cd: str,
    rounds: list[dict],
    delay: float,
) -> tuple[dict | None, list[dict]]:
    """
    우선순위 순으로 정렬된 라운드 목록을 순차 탐색하여,
    실제 선수 데이터가 1명 이상 존재하는 첫 번째 라운드를 반환.

    Returns:
        (선택된_라운드_dict, 해당_라운드의_선수_list)
        선수가 있는 라운드가 없으면 (None, [])
    """
    for rnd in rounds:
        rh_cd         = rnd["rh_cd"]
        rh_nm         = rnd["rh_nm"]
        base_class_cd = rnd["base_class_cd"]
        base_class_nm = rnd["base_class_nm"]
        pcnt_gbn      = rnd["pcnt_gbn"]

        logger.info(f"    라운드 시도: {rh_nm} (rhCd={rh_cd})")

        players = get_players_from_result(
            session, class_cd, to_cd, kind_cd, detail_class_cd,
            rh_cd, base_class_cd, rh_nm, base_class_nm, pcnt_gbn,
        )
        time.sleep(delay)

        if players:
            logger.info(f"    ✔ 유효 라운드 확정: {rh_nm} → 선수 {len(players)}명")
            return rnd, players
        else:
            logger.info(f"    ✘ 선수 0명 → 다음 라운드로")

    logger.warning("    모든 라운드에서 선수 데이터 없음")
    return None, []
# ─────────────────────────────────────────────────────────────────────────


def get_player_detail(
    session: requests.Session,
    class_cd: str,
    to_cd: str,
    kind_cd: str,
    detail_class_cd: str,
    rh_cd: str,
    player_id: str,
) -> dict | None:
    """INF503: 선수 상세정보(최종등록정보 + 대회참가이력) 조회"""
    url = f"{BASE_URL}/INF503.do"
    data = {
        "classCd":       class_cd,
        "toCd":          to_cd,
        "kindCd":        kind_cd,
        "idNo":          player_id,
        "rhCd":          rh_cd,
        "detailClassCd": detail_class_cd,
        "platform":      "pc",
    }
    resp = post_with_retry(session, url, data)
    soup = BeautifulSoup(resp.text, "html.parser")

    # 최종등록정보 파싱
    player_info = {"player_id": player_id}
    reg_table = soup.find("table")
    if reg_table:
        for row in reg_table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            for i in range(0, len(cells) - 1, 2):
                header = cells[i].get_text(strip=True)
                value  = cells[i + 1].get_text(strip=True)
                if "이름" in header:
                    player_info["name"] = value
                elif "성별" in header:
                    player_info["gender"] = value
                elif "출생년도" in header:
                    player_info["birth_year"] = re.sub(r"[^0-9]", "", value)
                elif "종별" in header:
                    player_info["kind_nm"] = value
                elif "소속팀" in header:
                    tm = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", value)
                    if tm:
                        player_info["team_nm"] = tm.group(1).strip()
                        player_info["team_cd"] = tm.group(2).strip()
                    else:
                        player_info["team_nm"] = value
                        player_info["team_cd"] = ""
                elif "시도" in header:
                    player_info["sido"] = value
                elif "최종등록년도" in header:
                    player_info["last_reg_year"] = re.sub(r"[^0-9]", "", value)
                elif "등록구분" in header:
                    player_info["reg_type"] = value

    # 대회참가이력 파싱
    history_list = []
    tables = soup.find_all("table")
    if len(tables) < 2:
        return {"player": player_info, "history": history_list}

    current_competition = ""
    for row in tables[1].find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        # colspan 있는 행 = 대회명
        if len(cells) == 1 or any(c.get("colspan") for c in cells):
            current_competition = cells[0].get_text(strip=True)
            continue
        if len(cells) >= 6:
            history_list.append(
                {
                    "player_id":      player_id,
                    "competition_nm": current_competition,
                    "event_date":     cells[0].get_text(strip=True),
                    "kind_nm":        cells[1].get_text(strip=True),
                    "event_nm":       cells[2].get_text(strip=True),
                    "round_nm":       cells[3].get_text(strip=True),
                    "team_nm":        cells[4].get_text(strip=True),
                    "record":         cells[5].get_text(strip=True),
                    "rank":           cells[6].get_text(strip=True) if len(cells) > 6 else "",
                }
            )

    return {"player": player_info, "history": history_list}


# ─────────────────────────────────────────────
# DB 저장 함수
# ─────────────────────────────────────────────
def save_player(conn: sqlite3.Connection, player: dict) -> str:
    """
    선수 마스터 정보를 저장한다.

    Returns:
        "new"     : 신규 선수로 INSERT됨
        "updated" : 기존 선수이나 last_reg_year가 더 최신이어서 UPDATE됨
        "skipped" : 기존 선수이고 last_reg_year가 같거나 이전이어서 skip됨
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT player_id, last_reg_year FROM player WHERE player_id = ?",
        (player["player_id"],)
    )
    existing = cur.fetchone()

    if existing:
        # 기존 선수 존재 - last_reg_year 비교
        db_last_reg_year = existing[1] or ""
        new_last_reg_year = player.get("last_reg_year", "")

        # 스크랩된 정보의 last_reg_year가 더 최신인 경우 UPDATE
        if new_last_reg_year and new_last_reg_year > db_last_reg_year:
            cur.execute(
                """
                UPDATE player
                SET name = ?,
                    gender = ?,
                    birth_year = ?,
                    kind_nm = ?,
                    team_nm = ?,
                    team_cd = ?,
                    sido = ?,
                    last_reg_year = ?,
                    reg_type = ?,
                    updated_at = datetime('now','localtime')
                WHERE player_id = ?
                """,
                (
                    player.get("name", ""),
                    player.get("gender", ""),
                    player.get("birth_year", ""),
                    player.get("kind_nm", ""),
                    player.get("team_nm", ""),
                    player.get("team_cd", ""),
                    player.get("sido", ""),
                    new_last_reg_year,
                    player.get("reg_type", ""),
                    player["player_id"],
                ),
            )
            conn.commit()
            return "updated"
        return "skipped"

    # 신규 선수 INSERT
    cur.execute(
        """
        INSERT INTO player
            (player_id, name, gender, birth_year, kind_nm,
             team_nm, team_cd, sido, last_reg_year, reg_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            player.get("player_id", ""),
            player.get("name", ""),
            player.get("gender", ""),
            player.get("birth_year", ""),
            player.get("kind_nm", ""),
            player.get("team_nm", ""),
            player.get("team_cd", ""),
            player.get("sido", ""),
            player.get("last_reg_year", ""),
            player.get("reg_type", ""),
        ),
    )
    conn.commit()
    return "new"


def save_history(conn: sqlite3.Connection, history_rows: list[dict]) -> int:
    cur = conn.cursor()
    saved = 0
    for h in history_rows:
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO player_history
                    (player_id, competition_nm, event_date, kind_nm,
                     event_nm, round_nm, team_nm, record, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    h.get("player_id", ""),
                    h.get("competition_nm", ""),
                    h.get("event_date", ""),
                    h.get("kind_nm", ""),
                    h.get("event_nm", ""),
                    h.get("round_nm", ""),
                    h.get("team_nm", ""),
                    h.get("record", ""),
                    h.get("rank", ""),
                ),
            )
            saved += cur.rowcount
        except sqlite3.Error as e:
            logger.warning(f"이력 저장 오류: {e}")
    conn.commit()
    return saved


def save_collect_log(
    conn: sqlite3.Connection,
    to_cd: str,
    competition_nm: str,
    kind_nm: str,
    event_nm: str,
    round_nm: str,
    total: int,
    new: int,
    skipped: int,
):
    conn.execute(
        """
        INSERT INTO collect_log
            (to_cd, competition_nm, kind_nm, event_nm, round_nm,
             total_players, new_players, skipped_players)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (to_cd, competition_nm, kind_nm, event_nm, round_nm, total, new, skipped),
    )
    conn.commit()


# ─────────────────────────────────────────────
# 대회명 조회
# ─────────────────────────────────────────────
def get_competition_name(
    session: requests.Session, class_cd: str, to_cd: str
) -> str:
    url = f"{BASE_URL}/INF202.do"
    resp = session.post(url, data={"classCd": class_cd, "toCd": to_cd, "platform": "pc"}, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    if tables:
        for row in tables[0].find_all("tr"):
            cells = row.find_all("td")
            for i, cell in enumerate(cells):
                if "대회명" in cell.get_text():
                    if i + 1 < len(cells):
                        return cells[i + 1].get_text(strip=True)
    return to_cd


# ─────────────────────────────────────────────
# 메인 수집 함수
# ─────────────────────────────────────────────
def collect(
    db_path: str = "shorttrack_players.db",
    class_cd: str = CLASS_CD_SHORTTRACK,
    to_cd: str = "202514413",
    target_kinds: list[str] | None = None,
    target_events: list[str] | None = None,
    delay: float = REQUEST_DELAY,
):
    """
    메인 수집 루프.

    각 종별+종목 조합에 대해:
      1) 라운드 목록을 우선순위(예선→준준결승→준결승→결승B→결승A→결승) 순으로 정렬
      2) 라운드를 순차 시도하며 선수 수 > 0인 첫 라운드를 채택
      3) 채택된 라운드의 선수 각각에 대해 상세정보 조회 후 DB 저장
      4) 신규 선수는 INSERT, 기존 선수는 last_reg_year 비교 후 UPDATE 또는 skip
      5) 같은 대회 내 중복 처리된 선수는 skip (seen_player_ids)
    """
    conn = init_db(db_path)
    session = make_session()

    competition_nm = get_competition_name(session, class_cd, to_cd)
    logger.info(f"대회: {competition_nm} (toCd={to_cd})")
    time.sleep(delay)

    event_list = get_event_list(session, class_cd, to_cd)
    time.sleep(delay)

    # 현재 세션에서 처리한 선수 ID 추적 (같은 대회 내 중복 처리 방지)
    seen_player_ids: set[str] = set()

    total_new     = 0
    total_updated = 0
    total_skip    = 0

    for event in event_list:
        kind_nm  = event["kind_nm"]
        kind_cd  = event["kind_cd"]
        event_nm = event["detail_class_nm"]
        event_cd = event["detail_class_cd"]

        # 종별/종목 필터
        if target_kinds and kind_nm not in target_kinds:
            continue
        if target_events and event_nm not in target_events:
            continue

        logger.info(f"▶ 종별={kind_nm}, 종목={event_nm}")

        # 라운드 목록 조회 (이미 우선순위 순 정렬됨)
        rounds = get_round_list(session, class_cd, to_cd, kind_cd, event_cd)
        time.sleep(delay)

        if not rounds:
            logger.warning(f"  라운드 없음: {kind_nm} {event_nm}")
            continue

        # ★ 핵심 변경: 선수가 실제 존재하는 첫 번째 유효 라운드 탐색 ─────────
        chosen_round, players = find_first_valid_round(
            session, class_cd, to_cd, kind_cd, event_cd, rounds, delay
        )
        # ──────────────────────────────────────────────────────────────────────

        if chosen_round is None:
            logger.warning(f"  유효 라운드 없음 (전체 skip): {kind_nm} {event_nm}")
            continue

        rh_nm = chosen_round["rh_nm"]
        rh_cd = chosen_round["rh_cd"]

        logger.info(f"  채택 라운드: {rh_nm} | 선수 {len(players)}명")

        new_count     = 0
        updated_count = 0
        skipped_count = 0

        for p in players:
            pid = p["player_id"]

            if pid in seen_player_ids:
                skipped_count += 1
                logger.debug(f"  SKIP: {p['name']} ({pid})")
                continue

            seen_player_ids.add(pid)

            # 선수 상세정보 조회
            detail = get_player_detail(
                session, class_cd, to_cd, kind_cd, event_cd, rh_cd, pid
            )
            time.sleep(delay)

            if not detail:
                logger.warning(f"  상세정보 조회 실패: {pid}")
                continue

            save_result = save_player(conn, detail["player"])

            if detail["history"]:
                save_history(conn, detail["history"])

            if save_result == "new":
                new_count += 1
                total_new += 1
                logger.info(
                    f"  NEW: {detail['player'].get('name','?')} ({pid}) "
                    f"이력 {len(detail['history'])}건"
                )
            elif save_result == "updated":
                updated_count += 1
                total_updated += 1
                logger.info(
                    f"  UPDATED: {detail['player'].get('name','?')} ({pid}) "
                    f"last_reg_year={detail['player'].get('last_reg_year','?')}"
                )
            else:
                skipped_count += 1
                total_skip += 1

        save_collect_log(
            conn, to_cd, competition_nm,
            kind_nm, event_nm, rh_nm,
            len(players), new_count, skipped_count,
        )
        logger.info(f"  완료: 신규={new_count}, 갱신={updated_count}, 중복skip={skipped_count}")

    logger.info(
        f"\n===== 수집 완료 ===== 총 신규={total_new}, 총 갱신={total_updated}, 총 skip={total_skip}"
    )
    conn.close()


# ─────────────────────────────────────────────
# CLI 인터페이스
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="빙상연맹 쇼트트랙 선수 데이터 수집기"
    )
    parser.add_argument(
        "--to_cd",
        default="202514413",
        help="대회 ID (toCd). 기본값: 제40회 전국남녀 종별종합 쇼트트랙 선수권대회",
    )
    parser.add_argument(
        "--class_cd",
        default=CLASS_CD_SHORTTRACK,
        help=f"종목 분류 코드 (기본값: {CLASS_CD_SHORTTRACK} = 쇼트트랙)",
    )
    parser.add_argument(
        "--kinds",
        nargs="*",
        default=None,
        help="수집할 종별 목록. 예: --kinds 남자중학부 여자중학부 | 미지정시 전체",
    )
    parser.add_argument(
        "--events",
        nargs="*",
        default=None,
        help="수집할 종목 목록. 예: --events 500M 1000M | 미지정시 전체",
    )
    parser.add_argument(
        "--db",
        default="shorttrack_players.db",
        help="SQLite DB 파일 경로 (기본값: shorttrack_players.db)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"요청 간 딜레이(초) (기본값: {REQUEST_DELAY})",
    )

    args = parser.parse_args()

    collect(
        db_path=args.db,
        class_cd=args.class_cd,
        to_cd=args.to_cd,
        target_kinds=args.kinds,
        target_events=args.events,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
"""
Writes parsed UDP packets to SQLite.
Schema is append-only during a race; one recording = one session_uid.
"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

TRACK_NAMES = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir (Bahrain)",
    4: "Catalunya", 5: "Monaco", 6: "Montreal", 7: "Silverstone",
    8: "Hockenheim", 9: "Hungaroring", 10: "Spa", 11: "Monza",
    12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Texas",
    16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico",
    20: "Baku (Azerbaijan)", 21: "Sakhir Short", 22: "Silverstone Short",
    23: "Texas Short", 24: "Suzuka Short", 25: "Hanoi", 26: "Zandvoort",
    27: "Imola", 28: "Portimão", 29: "Jeddah", 30: "Miami",
    31: "Las Vegas", 32: "Losail",
}

SESSION_TYPES = {
    0: "Unknown", 1: "P1", 2: "P2", 3: "P3", 4: "Short P",
    5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Q", 9: "OSQ",
    10: "R", 11: "R2", 12: "R3", 13: "Time Trial",
}

RESULT_STATUS_NAMES = {
    0: "Invalid", 1: "Inactive", 2: "Active", 3: "Finished",
    4: "DNF", 5: "DSQ", 6: "Not Classified", 7: "Retired",
}

EVENT_NAMES = {
    "SSTA": "Session Started", "SEND": "Session Ended",
    "FTLP": "Fastest Lap", "RTMT": "Retirement",
    "DRSE": "DRS Enabled", "DRSD": "DRS Disabled",
    "TMPT": "Team Mate In Pits", "CHQF": "Chequered Flag",
    "RCWN": "Race Winner", "PENA": "Penalty Issued",
    "SPTP": "Speed Trap", "STLG": "Start Lights",
    "LGOT": "Lights Out", "DTSV": "Drive Through Served",
    "SGSV": "Stop Go Served", "FLBK": "Flashback",
    "BUTN": "Button Status", "RDFL": "Red Flag",
    "OVTK": "Overtake", "SCAR": "Safety Car",
    "COLL": "Collision",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL,
    packet_id   INTEGER,
    session_uid INTEGER,
    data        BLOB
);

CREATE TABLE IF NOT EXISTS sessions (
    session_uid   INTEGER PRIMARY KEY,
    track_id      INTEGER,
    track_name    TEXT,
    session_type  TEXT,
    total_laps    INTEGER,
    recorded_at   INTEGER
);

CREATE TABLE IF NOT EXISTS participants (
    session_uid   INTEGER,
    car_index     INTEGER,
    name          TEXT,
    team_id       INTEGER,
    driver_id     INTEGER,
    race_number   INTEGER,
    ai_controlled INTEGER,
    PRIMARY KEY (session_uid, car_index)
);

CREATE TABLE IF NOT EXISTS lap_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uid     INTEGER,
    session_time    REAL,
    car_index       INTEGER,
    car_position    INTEGER,
    current_lap     INTEGER,
    lap_distance    REAL,
    total_distance  REAL,
    current_lap_time_ms INTEGER,
    last_lap_time_ms    INTEGER,
    pit_status      INTEGER,
    num_pit_stops   INTEGER,
    result_status   INTEGER,
    delta_to_leader_ms  INTEGER,
    warnings            INTEGER,
    penalties           INTEGER,
    tyre_compound   INTEGER
);

CREATE TABLE IF NOT EXISTS safety_car_timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uid INTEGER,
    session_time REAL,
    status      INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uid   INTEGER,
    session_time  REAL,
    event_code    TEXT,
    event_name    TEXT,
    vehicle_idx   INTEGER,
    extra_json    TEXT
);

CREATE TABLE IF NOT EXISTS final_classification (
    session_uid     INTEGER,
    car_index       INTEGER,
    position        INTEGER,
    num_laps        INTEGER,
    grid_position   INTEGER,
    points          INTEGER,
    num_pit_stops   INTEGER,
    result_status   TEXT,
    best_lap_ms     INTEGER,
    total_race_time REAL,
    penalties_time  INTEGER,
    PRIMARY KEY (session_uid, car_index)
);
"""


def _to_signed_uid(uid) -> int:
    uid = int(uid)
    return uid if uid < 2**63 else uid - 2**64


class Recorder:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._session_uid: Optional[int] = None
        self._last_snapshot_time: float = -1.0
        self._snapshot_interval: float = 0.5
        self._last_sc_status: Optional[int] = None
        self._current_tyre: dict = {}   # car_index -> visual_tyre_compound

    def store_raw(self, packet_id: int, session_uid: int, data: bytes) -> None:
        self.conn.execute(
            "INSERT INTO raw_packets (received_at, packet_id, session_uid, data) VALUES (?,?,?,?)",
            (time.time(), packet_id, session_uid, data),
        )

    def handle_session(self, pkt) -> None:
        uid = _to_signed_uid(pkt.header.session_uid)
        if uid != self._session_uid:
            self._session_uid = uid
            self._last_sc_status = None
            self._last_snapshot_time = -1.0
            self._current_tyre = {}
            track_name = TRACK_NAMES.get(int(pkt.track_id), f"Track {pkt.track_id}")
            session_name = SESSION_TYPES.get(int(pkt.session_type), f"Type {pkt.session_type}")
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?)",
                (uid, int(pkt.track_id), track_name, session_name,
                 int(pkt.total_laps), int(time.time())),
            )

        sc_status = int(pkt.safety_car_status)
        if sc_status != self._last_sc_status:
            self._last_sc_status = sc_status
            t = float(pkt.header.session_time)
            self.conn.execute(
                "INSERT INTO safety_car_timeline (session_uid, session_time, status) VALUES (?,?,?)",
                (uid, t, sc_status),
            )

        self.conn.commit()

    def handle_participants(self, pkt) -> None:
        uid = self._session_uid
        if uid is None:
            return
        rows = []
        for i, p in enumerate(list(pkt.participants)[:int(pkt.num_active_cars)]):
            name = bytes(p.name).split(b"\x00")[0].decode("utf-8", errors="replace")
            rows.append((uid, i, name, int(p.team_id), int(p.driver_id),
                         int(p.race_number), int(p.ai_controlled)))
        self.conn.executemany(
            "INSERT OR REPLACE INTO participants VALUES (?,?,?,?,?,?,?)", rows
        )
        self.conn.commit()

    def handle_car_status(self, pkt) -> None:
        """Cache visual tyre compound per car (packet_id=7)."""
        for i, cs in enumerate(list(pkt.car_status_data)):
            compound = int(cs.visual_tyre_compound)
            if compound > 0:
                self._current_tyre[i] = compound

    def handle_lap_data(self, pkt) -> None:
        t = float(pkt.header.session_time)
        if t - self._last_snapshot_time < self._snapshot_interval:
            return
        self._last_snapshot_time = t
        uid = _to_signed_uid(pkt.header.session_uid)
        rows = [
            (uid, t, i,
             int(ld.car_position), int(ld.current_lap_num),
             float(ld.lap_distance), float(ld.total_distance),
             int(ld.current_lap_time_in_ms), int(ld.last_lap_time_in_ms),
             int(ld.pit_status), int(ld.num_pit_stops), int(ld.result_status),
             int(ld.delta_to_race_leader_minutes_part) * 60000
             + int(ld.delta_to_race_leader_ms_part),
             int(ld.total_warnings),
             int(ld.penalties),
             self._current_tyre.get(i))
            for i, ld in enumerate(list(pkt.lap_data))
            if int(ld.result_status) in (0, 1, 2, 3)  # include 0/1 to capture pre-Active race laps
        ]
        self.conn.executemany(
            """INSERT INTO lap_snapshots
               (session_uid, session_time, car_index, car_position, current_lap,
                lap_distance, total_distance, current_lap_time_ms, last_lap_time_ms,
                pit_status, num_pit_stops, result_status,
                delta_to_leader_ms, warnings, penalties, tyre_compound)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()

    def handle_event(self, pkt) -> None:
        uid = self._session_uid
        if uid is None:
            return
        code = bytes(pkt.event_string_code).decode("ascii", errors="replace").rstrip("\x00")
        t = float(pkt.header.session_time)
        extra = {}
        vehicle_idx = None

        if code == "FTLP":
            fl = pkt.event_details.fastest_lap
            extra = {"lap_time": float(fl.lap_time)}
            vehicle_idx = int(fl.vehicle_idx)
        elif code == "PENA":
            p = pkt.event_details.penalty
            extra = {"type": int(p.penalty_type), "infringement": int(p.infringement_type),
                     "time": int(p.time), "lap": int(p.lap_num)}
            vehicle_idx = int(p.vehicle_idx)
        elif code == "OVTK":
            ov = pkt.event_details.overtake
            extra = {"overtaking": int(ov.overtaking_vehicle_idx),
                     "being_overtaken": int(ov.being_overtaken_vehicle_idx)}
        elif code == "RTMT":
            vehicle_idx = int(pkt.event_details.retirement.vehicle_idx)
        elif code == "RCWN":
            vehicle_idx = int(pkt.event_details.race_winner.vehicle_idx)
        elif code == "FLBK":
            fb = pkt.event_details.flashback
            extra = {"rewind_to": float(fb.flashback_session_time),
                     "frame": int(fb.flashback_frame_identifier)}
            self.conn.execute(
                "DELETE FROM lap_snapshots WHERE session_uid=? AND session_time > ?",
                (uid, float(fb.flashback_session_time)),
            )
            self._last_snapshot_time = float(fb.flashback_session_time) - self._snapshot_interval
            deleted_count = self.conn.execute("SELECT changes()").fetchone()[0]
            print(f"[recorder] Flashback detected — rewound to {fb.flashback_session_time:.1f}s, "
                  f"deleted {deleted_count} snapshot rows")

        self.conn.execute(
            "INSERT INTO events (session_uid, session_time, event_code, event_name, vehicle_idx, extra_json)"
            " VALUES (?,?,?,?,?,?)",
            (uid, t, code, EVENT_NAMES.get(code, code), vehicle_idx,
             json.dumps(extra) if extra else None),
        )
        self.conn.commit()

    def handle_final_classification(self, pkt) -> None:
        uid = self._session_uid
        if uid is None:
            return
        rows = [
            (uid, i, int(c.position), int(c.num_laps), int(c.grid_position), int(c.points),
             int(c.num_pit_stops), RESULT_STATUS_NAMES.get(int(c.result_status), "Unknown"),
             int(c.best_lap_time_in_ms), float(c.total_race_time), int(c.penalties_time))
            for i, c in enumerate(list(pkt.classification_data)[:int(pkt.num_cars)])
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO final_classification VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

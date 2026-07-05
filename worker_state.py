"""
worker_state.py — global mutable state + log() ของ RAG worker (แยกออกมาจาก rag_worker.py
ตาม Architecture report High #1) — จุดเดียวที่ถือ state ที่แชร์ข้าม module/thread ทั้งหมด

กติกาการใช้ (สำคัญ):
- ทุก module ต้องเข้าถึงผ่าน attribute ของโมดูลนี้เสมอ เช่น `state._index` ห้าม
  `from worker_state import _index` เด็ดขาด เพราะ _index/_reranker/_sys_prompt ถูก rebind
  ตอน _load_everything() เสร็จ — from-import จะได้ค่า None ค้างตลอดไป
- อ่าน/เขียน _status ต้องอยู่ใน `with _state_lock` เสมอ
- session state ทั้งหมดห่ออยู่ใน SessionStore แล้ว (Architecture report Low #2) — lock ให้เอง
  ในทุก method ผู้เรียกห้ามถือ dict/lock ของ session ตรงๆ อีก โค้ดใหม่จึงไม่มีทางลืม lock ได้
"""
import datetime
import threading
import time

from worker_config import LOG_FILE

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class SessionStore:
    """เก็บ chat memory ต่อ session พร้อม timestamp การใช้งานล่าสุด (ดู ADR-005) — lock ให้เอง
    ภายในทุก method (ดู Architecture report Low #2: เดิม _sessions/_session_last_used/_sessions_lock
    เป็น global 3 ตัวที่ทุกฟังก์ชันต้องจำเองว่าห้ามแตะ dict โดยไม่ถือ lock — ตอนนี้บังคับที่ interface
    แทนการพึ่งวินัยคนเขียนโค้ดถัดไป) พฤติกรรมเดิมทุกประการ: get-or-create + touch ในจังหวะเดียว,
    cleanup ลบตาม idle timeout"""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, object] = {}  # session_id -> ChatMemoryBuffer
        self._last_used: dict[str, float] = {}  # session_id -> unix timestamp ที่ใช้งานล่าสุด

    def get_or_create(self, session_id: str, factory):
        """คืน memory ของ session นี้ (สร้างใหม่ด้วย factory() ถ้ายังไม่มี) พร้อมอัปเดตเวลาใช้งาน
        ล่าสุดในจังหวะเดียวกันใต้ lock เดียว — factory เป็น callable เพื่อให้ llama_index ยังถูก
        import แบบ lazy ที่ฝั่งผู้เรียก (worker_handlers) เหมือนเดิม โมดูลนี้ไม่รู้จัก llama_index"""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = factory()
            self._last_used[session_id] = time.time()
            return self._sessions[session_id]

    def cleanup_idle(self, idle_timeout_seconds: float) -> list[str]:
        """ลบ session ที่ไม่ได้ใช้งานเกิน idle_timeout_seconds — คืนรายการ session_id ที่ถูกลบ
        (ผู้เรียกเอาไป log ต่อ) ดู ADR-005"""
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, last_used in self._last_used.items()
                if now - last_used > idle_timeout_seconds
            ]
            for sid in expired:
                self._sessions.pop(sid, None)
                self._last_used.pop(sid, None)
        return expired

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# ── Global state ─────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_status = {"status": "loading", "detail": "กำลังเริ่มต้น..."}
_index = None
_reranker = None
_sys_prompt = None
sessions = SessionStore()

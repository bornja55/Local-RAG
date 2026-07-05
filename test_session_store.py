"""
test_session_store.py — pure unit test ของ SessionStore ใน worker_state.py
(ดู Architecture report Low #2) ไม่ต้องมี GOOGLE_API_KEY ไม่ต้องโหลดโมเดล —
memory object เป็นอะไรก็ได้ (ใช้ object() ธรรมดา) เพราะ SessionStore ไม่รู้จัก llama_index เลย

วิธีรัน:
    venv\\Scripts\\python.exe test_session_store.py
"""
import threading
import time
import unittest

from worker_state import SessionStore


class TestSessionStore(unittest.TestCase):
    def test_get_or_create_creates_once_then_reuses(self):
        store = SessionStore()
        created = []

        def factory():
            obj = object()
            created.append(obj)
            return obj

        first = store.get_or_create("s1", factory)
        second = store.get_or_create("s1", factory)
        self.assertIs(first, second)          # session เดิมต้องได้ memory ตัวเดิม
        self.assertEqual(len(created), 1)     # factory ถูกเรียกครั้งเดียว
        self.assertEqual(len(store), 1)

    def test_separate_sessions_get_separate_memory(self):
        store = SessionStore()
        a = store.get_or_create("a", object)
        b = store.get_or_create("b", object)
        self.assertIsNot(a, b)
        self.assertEqual(len(store), 2)

    def test_cleanup_idle_removes_only_expired(self):
        store = SessionStore()
        store.get_or_create("old", object)
        # ย้อน timestamp ของ 'old' ให้เกิน timeout (แตะ internal ตรงๆ เฉพาะในเทสนี้)
        with store._lock:
            store._last_used["old"] = time.time() - 100
        store.get_or_create("fresh", object)

        expired = store.cleanup_idle(idle_timeout_seconds=50)
        self.assertEqual(expired, ["old"])
        self.assertEqual(len(store), 1)
        # 'fresh' ต้องยังอยู่ และ get_or_create ต้องไม่สร้างใหม่
        created = []
        store.get_or_create("fresh", lambda: created.append(1))
        self.assertEqual(created, [])

    def test_get_or_create_touches_last_used(self):
        """การใช้งาน session ต้องต่ออายุ — session ที่เพิ่งถูกใช้ห้ามโดน cleanup"""
        store = SessionStore()
        store.get_or_create("s", object)
        with store._lock:
            store._last_used["s"] = time.time() - 100  # แก่เกิน timeout
        store.get_or_create("s", object)               # ใช้งานอีกครั้ง = touch
        expired = store.cleanup_idle(idle_timeout_seconds=50)
        self.assertEqual(expired, [])
        self.assertEqual(len(store), 1)

    def test_thread_safety_single_creation_under_race(self):
        """ยิง get_or_create พร้อมกันหลาย thread — factory ต้องถูกเรียกครั้งเดียว
        และทุก thread ได้ memory ตัวเดียวกัน (นี่คือ guarantee ที่เดิมพึ่งวินัยการถือ _sessions_lock)"""
        store = SessionStore()
        created = []
        results = []
        barrier = threading.Barrier(8)

        def factory():
            obj = object()
            created.append(obj)
            return obj

        def worker():
            barrier.wait()
            results.append(store.get_or_create("shared", factory))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(created), 1)
        self.assertEqual(len(set(id(r) for r in results)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

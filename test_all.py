"""
test_all.py — unit test suite รวมทั้งหมดของโปรเจกต์นี้ (pure unit test, ไม่ต้องมี GOOGLE_API_KEY,
ไม่ต้องโหลดโมเดล, ไม่ต้องต่อเน็ต, ไม่ต้องมี worker process จริงรันอยู่)

รวมมาจาก 3 ไฟล์เดิม (2026-07-05 consolidation — ดู HANDOFF.md/ADR.md):
    - test_llm_fallback.py        → TestIsQuotaError, TestIsFallbackWorthyError,
                                     TestCompleteWithFallback, TestRunWithFallbackGenericCallShape
    - test_handle_chat_fallback.py → TestHandleChatFallback
    - test_session_store.py       → TestSessionStore

**ทำไมเหลือแค่ไฟล์เดียว**: ทั้ง 3 ไฟล์เดิมเป็น pure unittest ทั้งหมด (ไม่มี dependency นอก stdlib
ต่างจาก test_rag_pipeline.py ที่เป็น E2E ต้องมี worker process จริง + API key จริง และต่างจาก
test_fallback_model.py ที่เป็นสคริปต์ตรวจสอบชื่อโมเดลสำรองด้วยมือ ก่อนตั้งใน .env — ทั้งสองไฟล์นี้
"ไม่ใช่" unit test ในความหมายเดียวกัน จึงยังคงแยกไว้ต่างหาก ไม่รวมเข้ามาในไฟล์นี้)

ครอบคลุมเคสสำคัญที่ /scrutinize เคยเจอ (ดู ADR-003 หมายเหตุเพิ่มเติม 2026-07-03 และ 2026-07-05):
    - quota error → retry 3 ครั้ง + backoff → fallback
    - timeout error → ไม่ retry (backoff ไม่ช่วย) → fallback ทันที (regression test ของบั๊ก
      "timeout ไม่ trigger fallback")
    - error อื่นๆ ที่ไม่เข้าเงื่อนไข → ไม่ fallback
    - run_with_fallback() ใช้ได้กับ call shape ทั่วไป (llm.complete() และ chat_engine.chat())
    - _handle_chat() ทั้งฟังก์ชัน (สร้าง chat_engine + retry/fallback + แปลผลลัพธ์เป็น response dict)
    - SessionStore: thread-safety, idle cleanup, get-or-create semantics

วิธีรัน (ไม่ต้องใช้ venv ก็ได้ เพราะไม่มี dependency นอก stdlib):
    venv\\Scripts\\python.exe test_all.py
หรือ
    venv\\Scripts\\python.exe -m unittest test_all -v
"""
import sys
import threading
import time
import types
import unittest

from llm_fallback import (
    complete_with_fallback,
    is_fallback_worthy_error,
    is_quota_error,
    run_with_fallback,
)
from worker_state import SessionStore


# ═══════════════════════════════════════════════════════════════════════════════════
# Fakes ที่ใช้ร่วมกันหลาย test class (เดิมซ้ำกันเป๊ะๆ ในทั้ง test_llm_fallback.py และ
# test_handle_chat_fallback.py — รวมเป็นชุดเดียวตอน consolidate)
# ═══════════════════════════════════════════════════════════════════════════════════


class _FakeResponse:
    """เลียนแบบ response ของ llm.complete() (มีแค่ .text)"""

    def __init__(self, text):
        self.text = text


class _FakeLLM:
    """llm ปลอมที่ .complete() ทำตามสคริปต์: ถ้าเป็น Exception ให้ raise, ถ้าเป็น str ให้คืนเป็นคำตอบ"""

    def __init__(self, script):
        self._script = script

    def complete(self, prompt):
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)


class _ScriptedFactory:
    """llm_factory ปลอม — กำหนดสคริปต์แยกต่อโมเดล และบันทึกลำดับโมเดลที่ถูกเรียกไว้ตรวจสอบ"""

    def __init__(self, scripts: dict):
        self._scripts = {model: list(script) for model, script in scripts.items()}
        self.calls: list[str] = []

    def __call__(self, model):
        self.calls.append(model)
        return _FakeLLM(self._scripts[model])


class ReadTimeout(Exception):
    """เลียนแบบ exception ที่ 'ชื่อ type' เข้าเงื่อนไข timeout (เช่น ReadTimeout ของ httpx)"""


class DeadlineExceeded(Exception):
    """เลียนแบบ exception ที่ 'ชื่อ type' เข้าเงื่อนไข deadline (เช่น grpc DeadlineExceeded)"""


def _quota_exc():
    return Exception("429 RESOURCE_EXHAUSTED: quota exceeded for model")


class _FakeChatResponse:
    """เลียนแบบ response object ของ llama_index chat engine (มี .response/.source_nodes)
    ต่างจาก _FakeResponse ด้านบนที่เลียนแบบ llm.complete() (มีแค่ .text)"""

    def __init__(self, response_text, source_nodes=()):
        self.response = response_text
        self.source_nodes = list(source_nodes)


class _FakeChatEngine:
    """เลียนแบบ chat_engine ที่มี .chat(prompt) แทน .complete(prompt) — ใช้ทั้งใน
    TestRunWithFallbackGenericCallShape (ทดสอบ run_with_fallback เดี่ยวๆ) และ
    TestHandleChatFallback (ทดสอบ _handle_chat ทั้งฟังก์ชัน)"""

    def __init__(self, script):
        self._script = script

    def chat(self, prompt):
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # ต้องเป็น _FakeChatResponse อยู่แล้ว (สคริปต์ใส่มาสำเร็จรูป)


# ═══════════════════════════════════════════════════════════════════════════════════
# is_quota_error / is_fallback_worthy_error
# ═══════════════════════════════════════════════════════════════════════════════════


class TestIsQuotaError(unittest.TestCase):
    def test_code_429_attribute(self):
        e = Exception("boom")
        e.code = 429
        self.assertTrue(is_quota_error(e))

    def test_status_code_429_attribute(self):
        e = Exception("boom")
        e.status_code = 429
        self.assertTrue(is_quota_error(e))

    def test_429_word_boundary_in_message(self):
        self.assertTrue(is_quota_error(Exception("HTTP error 429 returned")))

    def test_429_inside_longer_number_not_matched(self):
        # กัน false positive: "42900123" ไม่ใช่ HTTP 429
        self.assertFalse(is_quota_error(Exception("record id 42900123 not found")))

    def test_resource_exhausted(self):
        self.assertTrue(is_quota_error(Exception("RESOURCE_EXHAUSTED")))

    def test_quota_word(self):
        self.assertTrue(is_quota_error(Exception("Quota exceeded")))

    def test_quotation_not_matched(self):
        # กัน false positive: "quotation" ต้องไม่แมตช์คำว่า quota
        self.assertFalse(is_quota_error(Exception("invalid quotation mark in input")))

    def test_unrelated_error(self):
        self.assertFalse(is_quota_error(ValueError("bad input")))


class TestIsFallbackWorthyError(unittest.TestCase):
    def test_quota_is_fallback_worthy(self):
        self.assertTrue(is_fallback_worthy_error(_quota_exc()))

    def test_timeout_type_name(self):
        self.assertTrue(is_fallback_worthy_error(ReadTimeout("boom")))

    def test_deadline_type_name(self):
        self.assertTrue(is_fallback_worthy_error(DeadlineExceeded("boom")))

    def test_timed_out_in_message(self):
        self.assertTrue(is_fallback_worthy_error(Exception("request timed out after 300s")))

    def test_deadline_exceeded_in_message(self):
        self.assertTrue(is_fallback_worthy_error(Exception("504 Deadline Exceeded")))

    def test_503_in_message(self):
        self.assertTrue(is_fallback_worthy_error(Exception("server returned 503")))

    def test_unavailable_in_message(self):
        self.assertTrue(is_fallback_worthy_error(Exception("UNAVAILABLE: service down")))

    def test_plain_value_error_not_fallback_worthy(self):
        self.assertFalse(is_fallback_worthy_error(ValueError("bad input")))

    def test_auth_error_not_fallback_worthy(self):
        # error แบบ "เรียกยังไงก็พัง" (เช่น API key ผิด) ต้องไม่เปลืองเวลาลองโมเดลสำรอง
        self.assertFalse(is_fallback_worthy_error(Exception("401 API key not valid")))


# ═══════════════════════════════════════════════════════════════════════════════════
# complete_with_fallback() — path ของ llm.complete() (ใช้ใน /draft, /review/*)
# ═══════════════════════════════════════════════════════════════════════════════════


class TestCompleteWithFallback(unittest.TestCase):
    def _run(self, factory, primary="primary", fallbacks=None, sleeps=None):
        """helper: เรียก complete_with_fallback แบบเก็บ sleep ที่ถูกเรียก และปิด log"""
        sleeps = sleeps if sleeps is not None else []
        return complete_with_fallback(
            primary, fallbacks or [], "prompt", "[TEST]",
            log=lambda msg: None, sleep=sleeps.append, llm_factory=factory,
        )

    def test_primary_success_no_fallback_called(self):
        factory = _ScriptedFactory({"primary": ["คำตอบ"]})
        text, error = self._run(factory, fallbacks=["fb1"])
        self.assertEqual(text, "คำตอบ")
        self.assertIsNone(error)
        self.assertEqual(factory.calls, ["primary"])

    def test_quota_error_retries_3_times_with_backoff_then_fallback(self):
        factory = _ScriptedFactory({
            "primary": [_quota_exc(), _quota_exc(), _quota_exc()],
            "fb1": ["คำตอบสำรอง"],
        })
        sleeps = []
        text, error = self._run(factory, fallbacks=["fb1"], sleeps=sleeps)
        self.assertEqual(text, "คำตอบสำรอง")
        self.assertIsNone(error)
        # โมเดลหลักถูกลอง 3 ครั้ง (retry + backoff 10s, 20s) ก่อนสลับไป fb1
        self.assertEqual(factory.calls, ["primary", "primary", "primary", "fb1"])
        self.assertEqual(sleeps, [10, 20])

    def test_timeout_triggers_fallback_without_retrying_primary(self):
        """Regression test ของบั๊ก "timeout ไม่ trigger fallback" (ADR-003 หมายเหตุ 2026-07-03):
        timeout ต้อง (1) ไม่ retry โมเดลหลักซ้ำ — backoff ไม่ช่วยอะไรกับ timeout และ
        (2) ต้องสลับไปโมเดลสำรองทันที ไม่ใช่โยน error ตรงๆ ทั้งที่มีโมเดลสำรองตั้งไว้"""
        factory = _ScriptedFactory({
            "primary": [ReadTimeout("Request timed out")],
            "fb1": ["คำตอบสำรอง"],
        })
        sleeps = []
        text, error = self._run(factory, fallbacks=["fb1"], sleeps=sleeps)
        self.assertEqual(text, "คำตอบสำรอง")
        self.assertIsNone(error)
        self.assertEqual(factory.calls, ["primary", "fb1"])  # ไม่ retry primary ซ้ำ
        self.assertEqual(sleeps, [])  # ไม่มี backoff กับ timeout

    def test_non_fallback_worthy_error_does_not_fallback(self):
        boom = ValueError("bad input")
        factory = _ScriptedFactory({"primary": [boom], "fb1": ["ไม่ควรถูกเรียก"]})
        text, error = self._run(factory, fallbacks=["fb1"])
        self.assertIsNone(text)
        self.assertIs(error, boom)
        self.assertEqual(factory.calls, ["primary"])  # ไม่แตะ fb1 เลย

    def test_no_fallback_models_returns_error(self):
        boom = _quota_exc()
        factory = _ScriptedFactory({"primary": [boom, boom, boom]})
        text, error = self._run(factory, fallbacks=[])
        self.assertIsNone(text)
        self.assertIs(error, boom)

    def test_fallback_chain_tries_next_model_on_any_error(self):
        """โมเดลสำรองตัวแรก error (ประเภทไหนก็ตาม) ต้องลองตัวถัดไปต่อทันที ไม่หยุดกลางคัน"""
        factory = _ScriptedFactory({
            "primary": [ReadTimeout("timed out")],
            "fb1": [ValueError("fb1 พังด้วยเหตุอื่น")],
            "fb2": ["คำตอบจาก fb2"],
        })
        text, error = self._run(factory, fallbacks=["fb1", "fb2"])
        self.assertEqual(text, "คำตอบจาก fb2")
        self.assertIsNone(error)
        self.assertEqual(factory.calls, ["primary", "fb1", "fb2"])

    def test_all_fallbacks_fail_returns_last_error(self):
        last = Exception("fb2 พังเป็นตัวสุดท้าย")
        factory = _ScriptedFactory({
            "primary": [_quota_exc(), _quota_exc(), _quota_exc()],
            "fb1": [Exception("fb1 พัง")],
            "fb2": [last],
        })
        text, error = self._run(factory, fallbacks=["fb1", "fb2"])
        self.assertIsNone(text)
        self.assertIs(error, last)
        self.assertEqual(factory.calls, ["primary", "primary", "primary", "fb1", "fb2"])

    def test_quota_recovers_on_second_attempt_without_fallback(self):
        """quota ครั้งแรกแล้วรอดครั้งที่สอง — ต้องได้คำตอบจากโมเดลหลัก ไม่แตะโมเดลสำรอง"""
        factory = _ScriptedFactory({
            "primary": [_quota_exc(), "คำตอบหลังรอ backoff"],
            "fb1": ["ไม่ควรถูกเรียก"],
        })
        sleeps = []
        text, error = self._run(factory, fallbacks=["fb1"], sleeps=sleeps)
        self.assertEqual(text, "คำตอบหลังรอ backoff")
        self.assertIsNone(error)
        self.assertEqual(factory.calls, ["primary", "primary"])
        self.assertEqual(sleeps, [10])


# ═══════════════════════════════════════════════════════════════════════════════════
# run_with_fallback: ตัว engine ทั่วไป (ดู ADR-003 หมายเหตุ 2026-07-05) — ทดสอบด้วย call shape
# ที่ "ไม่ใช่" llm.complete() (จำลอง chat_engine.chat() ที่ _handle_chat ใช้จริง) เพื่อยืนยันว่า
# ฟังก์ชันนี้ generic จริง ไม่ผูกกับรูปแบบของ complete_with_fallback เพียงอย่างเดียว
# ═══════════════════════════════════════════════════════════════════════════════════


class _ScriptedChatEngineFactory:
    def __init__(self, scripts: dict):
        self._scripts = {model: list(script) for model, script in scripts.items()}
        self.calls: list[str] = []

    def __call__(self, model):
        self.calls.append(model)
        return _FakeChatEngine(self._scripts[model])


class TestRunWithFallbackGenericCallShape(unittest.TestCase):
    """ยืนยันว่า run_with_fallback() ใช้ได้กับ object/call shape ใดก็ได้ ไม่ใช่แค่ llm.complete() —
    นี่คือ path เดียวกับที่ _handle_chat ใน worker_handlers.py เรียกใช้จริง (ดู
    TestHandleChatFallback ด้านล่างสำหรับเทสต์ระดับ _handle_chat ทั้งฟังก์ชัน รวม llama_index stub)"""

    def _run(self, factory, primary="primary", fallbacks=None, sleeps=None):
        sleeps = sleeps if sleeps is not None else []
        prompt = "สวัสดี"
        return run_with_fallback(
            primary, fallbacks or [], factory, lambda engine: engine.chat(prompt),
            "[TEST-CHAT]", log=lambda msg: None, sleep=sleeps.append,
        )

    def test_primary_success_returns_response_object_not_text(self):
        """ต่างจาก complete_with_fallback (คืน .text เป็น str) — run_with_fallback คืนอะไรก็ตามที่
        call() คืนมาตรงๆ ในที่นี้คือ response object ทั้งก้อน (มี .response/.source_nodes)"""
        factory = _ScriptedChatEngineFactory({"primary": [_FakeChatResponse("ตอบแล้ว", source_nodes=[1, 2])]})
        result, error = self._run(factory)
        self.assertIsNone(error)
        self.assertEqual(result.response, "ตอบแล้ว")
        self.assertEqual(result.source_nodes, [1, 2])
        self.assertEqual(factory.calls, ["primary"])

    def test_timeout_triggers_fallback_without_retrying_primary_chat_shape(self):
        """regression test เดียวกับ complete_with_fallback แต่ผ่าน call shape ของ chat —
        พิสูจน์ว่าบั๊ก ADR-003 เดิม (retry primary ตอน timeout) แก้ที่จุดเดียว ใช้ร่วมกันทั้ง 2 path"""
        factory = _ScriptedChatEngineFactory({
            "primary": [ReadTimeout("Request timed out")],
            "fb1": [_FakeChatResponse("ตอบจากโมเดลสำรอง")],
        })
        sleeps = []
        result, error = self._run(factory, fallbacks=["fb1"], sleeps=sleeps)
        self.assertIsNone(error)
        self.assertEqual(result.response, "ตอบจากโมเดลสำรอง")
        self.assertEqual(factory.calls, ["primary", "fb1"])  # ไม่ retry primary ซ้ำ
        self.assertEqual(sleeps, [])

    def test_quota_retries_3_times_then_fallback_chat_shape(self):
        factory = _ScriptedChatEngineFactory({
            "primary": [_quota_exc(), _quota_exc(), _quota_exc()],
            "fb1": [_FakeChatResponse("ตอบจากโมเดลสำรอง")],
        })
        sleeps = []
        result, error = self._run(factory, fallbacks=["fb1"], sleeps=sleeps)
        self.assertIsNone(error)
        self.assertEqual(result.response, "ตอบจากโมเดลสำรอง")
        self.assertEqual(factory.calls, ["primary", "primary", "primary", "fb1"])
        self.assertEqual(sleeps, [10, 20])

    def test_all_fallbacks_fail_returns_none_and_last_error_chat_shape(self):
        last = Exception("fb1 พังเป็นตัวสุดท้าย")
        factory = _ScriptedChatEngineFactory({
            "primary": [ReadTimeout("timed out")],
            "fb1": [last],
        })
        result, error = self._run(factory, fallbacks=["fb1"])
        self.assertIsNone(result)
        self.assertIs(error, last)


# ═══════════════════════════════════════════════════════════════════════════════════
# _handle_chat() ทั้งฟังก์ชัน — worker_handlers.py (ดู ADR-003 หมายเหตุเพิ่มเติม 2026-07-05 /
# HANDOFF.md "0b" ข้อ 4 เดิมที่เคยระบุว่า path นี้ไม่มี unit test คุ้มเลย, ปิด gap แล้วดู "0d")
#
# ก่อนหน้านี้ _handle_chat เขียน retry+fallback loop เองแยกต่างหากทั้งหมด (ไม่ผ่าน
# llm_fallback.complete_with_fallback เพราะใช้ chat_engine.chat() แทน llm.complete()) — ไม่มีเทสคุ้ม
# เส้นทางนี้เลย ถ้าแก้โค้ดในอนาคตบั๊ก ADR-003 เดิม (retry primary ตอน timeout) อาจกลับมาได้โดยไม่มีเทสจับ
#
# แก้โดยแยกตรรกะ retry/fallback ออกมาเป็น llm_fallback.run_with_fallback() ที่ generic พอจะใช้ร่วมกับ
# llm.complete() (complete_with_fallback) และ chat_engine.chat() (_handle_chat) ได้ — ดู
# TestRunWithFallbackGenericCallShape ด้านบนสำหรับเทสของตรรกะ retry/fallback เอง — ไฟล์นี้ทดสอบคนละชั้น:
# ว่า _handle_chat ประกอบร่าง (สร้าง chat_engine ผูก memory/retriever, เรียก run_with_fallback,
# แปลผลลัพธ์เป็น response dict ที่ /chat endpoint คืนให้ client) ถูกต้องจริง — ไม่ต้องมี GOOGLE_API_KEY,
# ไม่ต้องโหลดโมเดล, ไม่ต้องมี llama_index ติดตั้งจริงด้วยซ้ำ (stub เฉพาะส่วนที่ _handle_chat ใช้ผ่าน
# sys.modules เพราะ import เป็นแบบ lazy อยู่แล้วในฟังก์ชัน)
# ═══════════════════════════════════════════════════════════════════════════════════


def _install_fake_llama_index():
    """stub เฉพาะส่วนของ llama_index ที่ _handle_chat ใช้ (ChatMemoryBuffer, Settings) — ไม่ต้อง
    ติดตั้ง llama_index จริงเลย เพราะ _handle_chat import แบบ lazy ข้างในฟังก์ชันอยู่แล้ว (ตั้งใจ
    ออกแบบไว้ให้ทดสอบแบบนี้ได้ — ดู worker_state.py). คืน dict ของ module เดิม (ถ้ามี) ไว้คืนค่ากลับ"""
    saved = {
        name: sys.modules.get(name)
        for name in ("llama_index", "llama_index.core", "llama_index.core.memory")
    }

    fake_llama_index = types.ModuleType("llama_index")
    fake_core = types.ModuleType("llama_index.core")
    fake_memory = types.ModuleType("llama_index.core.memory")

    class FakeChatMemoryBuffer:
        @classmethod
        def from_defaults(cls, token_limit=8000):
            return cls()

        def put(self, *a, **kw):
            pass

    class FakeSettings:
        llm = None

    fake_memory.ChatMemoryBuffer = FakeChatMemoryBuffer
    fake_core.Settings = FakeSettings
    fake_core.memory = fake_memory
    fake_llama_index.core = fake_core

    sys.modules["llama_index"] = fake_llama_index
    sys.modules["llama_index.core"] = fake_core
    sys.modules["llama_index.core.memory"] = fake_memory
    return saved


def _restore_llama_index(saved: dict) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class _FakeNode:
    def __init__(self, file_name: str, content: str):
        self._file_name = file_name
        self._content = content
        self.metadata = {"file_name": file_name}

    def get_content(self):
        return self._content


class _FakeSourceNode:
    def __init__(self, file_name: str, content: str):
        self.node = _FakeNode(file_name, content)


class TestHandleChatFallback(unittest.TestCase):
    """ทดสอบ _handle_chat() ทั้งฟังก์ชัน (สร้าง chat_engine + retry/fallback + แปลผลลัพธ์) —
    stub state._index ให้คืน chat_engine ปลอมตามสคริปต์ที่กำหนด แยกสคริปต์ต่อโมเดลเหมือน
    _ScriptedFactory ด้านบน"""

    def setUp(self):
        self._saved_modules = _install_fake_llama_index()

        # import หลังติดตั้ง fake llama_index แล้วเท่านั้น (เผื่อกรณี rerun ในโปรเซสเดียวกัน
        # ทำให้ module cache ของ worker_handlers ไม่เคยเห็น llama_index จริงเลย)
        import worker_config as config
        import worker_state as state
        import worker_handlers as handlers

        self.config = config
        self.state = state
        self.handlers = handlers

        # backup ค่า config/state เดิม กันเทสนี้กระทบเทสอื่น (module-level state ใช้ร่วมกันข้ามเทส)
        self._saved_chat_model = config.GEMINI_MODEL_CHAT
        self._saved_chat_fallback = config.GEMINI_MODEL_CHAT_FALLBACK
        self._saved_index = state._index
        self._saved_reranker = state._reranker
        self._saved_sys_prompt = state._sys_prompt
        self._saved_build_llm = handlers._build_llm
        self._saved_sessions = state.sessions

        config.GEMINI_MODEL_CHAT = "primary"
        state._reranker = object()
        state._sys_prompt = "fake sys prompt"
        state.sessions = type(state.sessions)()  # SessionStore สดใหม่ ไม่ปนกับเทสอื่น/รันจริง

        self._model_scripts: dict[str, list] = {}
        self._current_model = [None]
        handlers._build_llm = lambda model: self._current_model.__setitem__(0, model)

        class _FakeIndex:
            def as_chat_engine(_self, **kwargs):
                model = self._current_model[0]
                return _FakeChatEngine(self._model_scripts[model])

        state._index = _FakeIndex()

    def tearDown(self):
        self.config.GEMINI_MODEL_CHAT = self._saved_chat_model
        self.config.GEMINI_MODEL_CHAT_FALLBACK = self._saved_chat_fallback
        self.state._index = self._saved_index
        self.state._reranker = self._saved_reranker
        self.state._sys_prompt = self._saved_sys_prompt
        self.handlers._build_llm = self._saved_build_llm
        self.state.sessions = self._saved_sessions
        _restore_llama_index(self._saved_modules)

    def test_primary_success_returns_response_with_sources_and_tokens(self):
        self.config.GEMINI_MODEL_CHAT_FALLBACK = []
        self._model_scripts["primary"] = [
            _FakeChatResponse("คำตอบจากโมเดลหลัก", source_nodes=[
                _FakeSourceNode("policy_a.md", "เนื้อหานโยบาย A " * 30),
            ])
        ]

        result = self.handlers._handle_chat("session-1", "ถามอะไรสักอย่าง")

        self.assertNotIn("error", result)
        self.assertEqual(result["response"], "คำตอบจากโมเดลหลัก")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["file_name"], "policy_a.md")
        self.assertLessEqual(len(result["sources"][0]["content"]), 200)
        self.assertIsInstance(result["tokens"], int)

    def test_quota_error_retries_3_times_with_backoff_then_falls_back(self):
        """เหมือน TestCompleteWithFallback แต่ผ่าน _handle_chat ทั้งฟังก์ชัน —
        เดิม path นี้ไม่มีเทสคุ้มเลย (ดู module docstring)"""
        self.config.GEMINI_MODEL_CHAT_FALLBACK = ["fb1"]
        self._model_scripts["primary"] = [
            Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
            Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
            Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
        ]
        self._model_scripts["fb1"] = [_FakeChatResponse("ตอบจากโมเดลสำรอง")]

        # sleep จริงจะทำให้เทสช้า (10s+20s) — ปิดโดย monkeypatch time.sleep เฉพาะช่วงเทสนี้
        original_sleep = time.sleep
        slept = []
        time.sleep = lambda s: slept.append(s)
        try:
            result = self.handlers._handle_chat("session-2", "คำถาม")
        finally:
            time.sleep = original_sleep

        self.assertNotIn("error", result)
        self.assertEqual(result["response"], "ตอบจากโมเดลสำรอง")
        self.assertEqual(slept, [10, 20])

    def test_timeout_triggers_fallback_without_retrying_primary(self):
        """Regression test ของบั๊ก ADR-003 เดิม (retry primary ตอน timeout แทนที่จะ fallback ทันที)
        ผ่าน _handle_chat ทั้งฟังก์ชัน — เดิมไม่มีเทสระดับนี้จับบั๊กนี้ได้เลยถ้ามันย้อนกลับมา"""
        self.config.GEMINI_MODEL_CHAT_FALLBACK = ["fb1"]
        self._model_scripts["primary"] = [TimeoutError("Request timed out after 300s")]
        self._model_scripts["fb1"] = [_FakeChatResponse("ตอบจากโมเดลสำรอง")]

        result = self.handlers._handle_chat("session-3", "คำถาม")

        self.assertNotIn("error", result)
        self.assertEqual(result["response"], "ตอบจากโมเดลสำรอง")
        # primary ต้องถูกใช้แค่ script เดียว (ไม่ retry ซ้ำ) — ถ้า script ถูกดึงเกิน 1 ครั้งจะ IndexError
        self.assertEqual(self._model_scripts["primary"], [])

    def test_all_models_fail_returns_error_dict(self):
        self.config.GEMINI_MODEL_CHAT_FALLBACK = ["fb1"]
        self._model_scripts["primary"] = [TimeoutError("timed out")]
        last_error = Exception("fb1 ก็พังด้วย")
        self._model_scripts["fb1"] = [last_error]

        result = self.handlers._handle_chat("session-4", "คำถาม")

        self.assertIn("error", result)
        self.assertIn("fb1 ก็พังด้วย", result["error"])

    def test_no_fallback_configured_returns_error_immediately_on_primary_failure(self):
        """ค่า default ของ GEMINI_MODEL_CHAT_FALLBACK คือ list ว่าง (ปิดฟีเจอร์นี้) — ต้อง error
        ตรงๆ ไม่มีการพยายามหาโมเดลสำรองใดๆ (backward-compatible กับพฤติกรรมก่อนมี fallback — ADR-003)"""
        self.config.GEMINI_MODEL_CHAT_FALLBACK = []
        self._model_scripts["primary"] = [Exception("บางอย่างพัง")]

        result = self.handlers._handle_chat("session-5", "คำถาม")

        self.assertIn("error", result)
        self.assertIn("บางอย่างพัง", result["error"])


# ═══════════════════════════════════════════════════════════════════════════════════
# SessionStore — worker_state.py (ดู Architecture report Low #2)
# ═══════════════════════════════════════════════════════════════════════════════════


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

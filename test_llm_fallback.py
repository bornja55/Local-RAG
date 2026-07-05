"""
test_llm_fallback.py — pure unit test ของ llm_fallback.py (ดู Architecture report High #2)
ไม่ต้องมี GOOGLE_API_KEY ไม่ต้องโหลดโมเดล ไม่ต้องต่อเน็ต — mock exception object ล้วนๆ

ครอบคลุมเคสที่ /scrutinize เคยเจอ (ดู ADR-003 หมายเหตุเพิ่มเติม 2026-07-03):
    - quota error → retry 3 ครั้ง + backoff → fallback
    - timeout error → ไม่ retry (backoff ไม่ช่วย) → fallback ทันที  ← regression test ของบั๊ก
      "timeout ไม่ trigger fallback" ที่แก้ไปรอบก่อน
    - error อื่นๆ ที่ไม่เข้าเงื่อนไข → ไม่ fallback

วิธีรัน (ไม่ต้องใช้ venv ก็ได้ เพราะไม่มี dependency นอก stdlib):
    venv\\Scripts\\python.exe test_llm_fallback.py
หรือ
    venv\\Scripts\\python.exe -m unittest test_llm_fallback -v
"""
import unittest

from llm_fallback import (
    complete_with_fallback,
    is_fallback_worthy_error,
    is_quota_error,
    run_with_fallback,
)


class _FakeResponse:
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


class _TimeoutLikeError(Exception):
    """เลียนแบบ exception ที่ 'ชื่อ type' เข้าเงื่อนไข timeout (เช่น ReadTimeout ของ httpx)"""


class ReadTimeout(Exception):
    pass


class DeadlineExceeded(Exception):
    pass


def _quota_exc():
    return Exception("429 RESOURCE_EXHAUSTED: quota exceeded for model")


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


# ── run_with_fallback: ตัว engine ทั่วไป (ดู ADR-003 หมายเหตุ 2026-07-05) ──────────────────────
# ทดสอบด้วย call shape ที่ "ไม่ใช่" llm.complete() (จำลอง chat_engine.chat() ที่ _handle_chat ใช้จริง)
# เพื่อยืนยันว่าฟังก์ชันนี้ generic จริง ไม่ผูกกับรูปแบบของ complete_with_fallback เพียงอย่างเดียว —
# complete_with_fallback ด้านบนเป็นแค่ 1 ใน 2 ผู้ใช้งานของฟังก์ชันนี้เท่านั้น (อีกที่คือ _handle_chat)


class _FakeChatResponse:
    """เลียนแบบ response object ของ llama_index chat engine (มี .response/.source_nodes)
    ต่างจาก _FakeResponse ด้านบนที่เลียนแบบ llm.complete() (มีแค่ .text)"""

    def __init__(self, response_text, source_nodes=()):
        self.response = response_text
        self.source_nodes = list(source_nodes)


class _FakeChatEngine:
    """เลียนแบบ chat_engine ที่มี .chat(prompt) แทน .complete(prompt) — ใช้ทดสอบว่า
    run_with_fallback() ใช้ได้กับ call shape ที่ต่างจาก complete_with_fallback จริงๆ"""

    def __init__(self, script):
        self._script = script

    def chat(self, prompt):
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # ต้องเป็น _FakeChatResponse อยู่แล้ว (สคริปต์ใส่มาสำเร็จรูป — เหมือน
        # _FakeChatEngine ใน test_handle_chat_fallback.py แก้ 2026-07-05: เดิม wrap ซ้ำเป็น
        # _FakeChatResponse(result) ทั้งที่ result เป็น _FakeChatResponse อยู่แล้ว ทำให้
        # .response กลายเป็น object แทนที่จะเป็น str ที่คาดไว้ (เทสยังพังไม่รู้ตัวเพราะไม่มีใคร
        # เรียก TestRunWithFallbackGenericCallShape มาก่อนจนกว่าจะรันจริงรอบนี้)


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
    test_handle_chat_fallback.py สำหรับเทสต์ระดับ _handle_chat ทั้งฟังก์ชัน รวม llama_index stub)"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
test_handle_chat_fallback.py — unit test ของ _handle_chat() ใน worker_handlers.py (ดู ADR-003
หมายเหตุเพิ่มเติม 2026-07-05 / HANDOFF.md "0b" ข้อ 4 ที่เคยระบุว่า path นี้ไม่มี unit test คุ้มเลย)

ก่อนหน้านี้ _handle_chat เขียน retry+fallback loop เองแยกต่างหากทั้งหมด (ไม่ผ่าน
llm_fallback.complete_with_fallback เพราะใช้ chat_engine.chat() แทน llm.complete()) — ไม่มีเทสคุ้ม
เส้นทางนี้เลย ถ้าแก้โค้ดในอนาคตบั๊ก ADR-003 เดิม (retry primary ตอน timeout) อาจกลับมาได้โดยไม่มีเทสจับ

แก้โดยแยกตรรกะ retry/fallback ออกมาเป็น llm_fallback.run_with_fallback() ที่ generic พอจะใช้ร่วมกับ
llm.complete() (complete_with_fallback) และ chat_engine.chat() (_handle_chat) ได้ — ดู
test_llm_fallback.py::TestRunWithFallbackGenericCallShape สำหรับเทสของตรรกะ retry/fallback เอง

ไฟล์นี้ทดสอบคนละชั้น: ว่า _handle_chat ประกอบร่าง (สร้าง chat_engine ผูก memory/retriever, เรียก
run_with_fallback, แปลผลลัพธ์เป็น response dict ที่ /chat endpoint คืนให้ client) ถูกต้องจริง —
ไม่ต้องมี GOOGLE_API_KEY, ไม่ต้องโหลดโมเดล, ไม่ต้องมี llama_index ติดตั้งจริงด้วยซ้ำ (stub เฉพาะส่วนที่
_handle_chat ใช้ผ่าน sys.modules เพราะ import เป็นแบบ lazy อยู่แล้วในฟังก์ชัน)

วิธีรัน:
    venv\\Scripts\\python.exe test_handle_chat_fallback.py
หรือ
    venv\\Scripts\\python.exe -m unittest test_handle_chat_fallback -v
"""
import sys
import types
import unittest


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


class _FakeChatResponse:
    def __init__(self, response_text: str, source_nodes=()):
        self.response = response_text
        self.source_nodes = list(source_nodes)


class _FakeChatEngine:
    """เลียนแบบ chat_engine ที่ .chat(prompt) ทำตามสคริปต์ — เหมือน _FakeLLM ใน
    test_llm_fallback.py แต่คืน response object (มี .response/.source_nodes) แทน .text"""

    def __init__(self, script):
        self._script = script

    def chat(self, prompt):
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # ต้องเป็น _FakeChatResponse อยู่แล้ว (สคริปต์ใส่มาสำเร็จรูป)


class TestHandleChatFallback(unittest.TestCase):
    """ทดสอบ _handle_chat() ทั้งฟังก์ชัน (สร้าง chat_engine + retry/fallback + แปลผลลัพธ์) —
    stub state._index ให้คืน chat_engine ปลอมตามสคริปต์ที่กำหนด แยกสคริปต์ต่อโมเดลเหมือน
    _ScriptedFactory ใน test_llm_fallback.py"""

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
        """เหมือน test_llm_fallback.TestCompleteWithFallback แต่ผ่าน _handle_chat ทั้งฟังก์ชัน —
        เดิม path นี้ไม่มีเทสคุ้มเลย (ดู module docstring)"""
        self.config.GEMINI_MODEL_CHAT_FALLBACK = ["fb1"]
        self._model_scripts["primary"] = [
            Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
            Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
            Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
        ]
        self._model_scripts["fb1"] = [_FakeChatResponse("ตอบจากโมเดลสำรอง")]

        # sleep จริงจะทำให้เทสช้า (10s+20s) — ปิดโดย monkeypatch time.sleep เฉพาะช่วงเทสนี้
        import time
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

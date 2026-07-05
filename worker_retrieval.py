"""
worker_retrieval.py — retrieval layer ของ RAG worker (แยกออกมาจาก rag_worker.py ตาม
Architecture report High #1) — ดึง context จาก FAISS index + reranker ที่ worker โหลดไว้ใน
worker_state (rebind ตอน _load_everything() เสร็จ — ต้องอ้างผ่าน state.X ที่ call time เสมอ
ห้าม from-import ค่าออกมา เพราะจะได้ None ค้าง)
"""
import worker_state as state


def _nodes_to_context_and_sources(nodes) -> tuple[str, list[dict]]:
    """แปลง list ของ retrieved nodes (หลัง rerank แล้ว) เป็น (context_text, sources) รูปแบบเดียวกับ
    ที่ /chat คืนให้ — ใช้ร่วมกันโดย _retrieve_context (whole-corpus) และ _retrieve_context_scoped
    (scoped ต่อรายชื่อไฟล์ — ดู ADR-006 Cross-reference retrieval) กันโค้ดซ้ำ"""
    context_text = "\n\n".join(
        f"[{n.node.metadata.get('file_name', 'Unknown')}]\n{n.node.get_content()}"
        for n in nodes
    )
    sources = [
        {
            "file_name": n.node.metadata.get("file_name", "Unknown"),
            "content": n.node.get_content()[:200],
        }
        for n in nodes
    ]
    return context_text, sources


def _retrieve_context(query_str: str, top_n: int = 15) -> tuple[str, list[dict]]:
    """ดึง context ที่เกี่ยวข้องจาก index+reranker เดียวกับ Q&A ใช้ทั้งขั้นตอนร่างและ scrutinize
    (ดู ADR-001 ข้อ 6 — ไม่สร้าง index แยกสำหรับโหมดร่างเอกสาร) ดึงข้าม corpus ทั้งหมดแบบ semantic
    search ล้วนๆ ไม่ scope ต่อเอกสารใดเอกสารหนึ่งเลย — ถ้าต้องการ scope ต่อรายชื่อไฟล์ที่กำหนด ใช้
    _retrieve_context_scoped() แทน (ดู ADR-006 Cross-reference retrieval)
    คืนค่า (context_text รวมเป็น string เดียว, รายการ sources แบบเดียวกับที่ /chat คืนให้)"""
    from llama_index.core.schema import QueryBundle

    retriever = state._index.as_retriever(similarity_top_k=60)
    nodes = retriever.retrieve(query_str)
    nodes = state._reranker._postprocess_nodes(nodes, QueryBundle(query_str=query_str))[:top_n]
    return _nodes_to_context_and_sources(nodes)


def _retrieve_context_scoped(
    query_str: str, allowed_file_names: list[str], top_n: int = 15, over_fetch_k: int = 200
) -> tuple[str, list[dict]]:
    """เหมือน _retrieve_context() แต่ scope ผลลัพธ์ให้เหลือเฉพาะไฟล์ใน allowed_file_names เท่านั้น —
    ใช้กับ **Cross-reference retrieval** ของโหมดรีวิวเอกสาร (ดู ADR-006 ข้อ 3/CONTEXT.md) เพื่อไม่ให้
    เนื้อหาจากเอกสารอื่นที่บังเอิญ match คำค้นหาหลุดเข้ามาปนกับเอกสารที่เกี่ยวข้องที่ผู้ใช้ยืนยันไว้แล้ว

    หมายเหตุ implementation (สำคัญ): FaissVectorStore ที่ระบบใช้อยู่ (ดู
    venv/Lib/site-packages/llama_index/vector_stores/faiss/base.py: query()) ไม่รองรับ metadata
    filters ที่ชั้น query() เลย — ส่ง `filters=` เข้าไปจะโดน raise ValueError("Metadata filters not
    implemented for Faiss yet.") ทันที เพราะ FAISS index เองไม่มี concept ของ metadata ในตัว ระบบจึง
    ทำ filtering แบบ application-level แทน: over-fetch (similarity_top_k สูงกว่าปกติมาก ดีฟอลต์ 200)
    จากนั้นกรอง node.metadata['file_name'] ด้วย Python เอาเฉพาะที่อยู่ใน allowed list ก่อนส่งต่อให้
    reranker เดิม — ไม่ใช่การกรองที่ชั้น vector store แบบที่ ADR-006 ผลที่ตามมาพูดถึงเรื่อง metadata
    filter เผื่อไว้ แต่ได้ผลลัพธ์เดียวกัน (scope ตาม allowed_file_names) โดยไม่ต้องแก้ FaissVectorStore
    เอง ถ้า allowed_file_names ว่างเปล่า คืน context ว่างทันทีโดยไม่ retrieve เลย (กันกรณีผู้ใช้ยังไม่
    ยืนยันเอกสารที่เกี่ยวข้องเลยสักฉบับ)"""
    if not allowed_file_names:
        return "", []

    from llama_index.core.schema import QueryBundle

    allowed_set = set(allowed_file_names)
    retriever = state._index.as_retriever(similarity_top_k=over_fetch_k)
    nodes = retriever.retrieve(query_str)
    nodes = [n for n in nodes if n.node.metadata.get("file_name") in allowed_set]
    if not nodes:
        return "", []
    nodes = state._reranker._postprocess_nodes(nodes, QueryBundle(query_str=query_str))[:top_n]
    return _nodes_to_context_and_sources(nodes)

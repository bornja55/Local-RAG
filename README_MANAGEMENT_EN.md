*Read this in other languages: [🇹🇭 ภาษาไทย](README_MANAGEMENT.md), [🇬🇧 English](README_MANAGEMENT_EN.md)*

# 🤖 Policy RAG Assistant — Leadership Summary (v2.2)

![Status](https://img.shields.io/badge/Status-Live%20%26%20Tested-2ECC71?style=for-the-badge)
![Data](https://img.shields.io/badge/Data-On--Prem%20Only-3498DB?style=for-the-badge)
![Review](https://img.shields.io/badge/AI%20Drafts-Require%20Human%20Approval-E67E22?style=for-the-badge)

An AI assistant that answers employee questions about **Origin Global Empire PLC's** policies and procedures, so staff no longer have to open documents one by one. It also drafts new policies as a starting point for the team to review — every answer is grounded strictly in real company documents, with no fabricated responses.

---

## 🆕 Latest Update — v2.2

**Status: Shipped** — verified with 11/11 end-to-end test cases (up from 6/6, reflecting the new features added).

- **New mode: helps review existing policies (Document Review Mode):** Beyond drafting new policies, the system can now help *review* policies that already exist — it automatically maps out a document's heading structure, surfaces related documents worth checking together (e.g. cross-referenced policies), and pulls supporting detail from real source documents so the team can work through the document one section at a time. This cuts down the time spent re-reading an entire old policy from scratch whenever it needs updating.
- **More thorough clarifying questions before drafting:** Previously a single batch of questions, now grouped into categories with broader coverage — further reducing the chance the system has to "guess" company-specific facts not covered in existing documents.
- **Closed out a risk of the system hanging with no response:** Testing surfaced that some AI requests could hang indefinitely with no warning (there was previously no time limit in place at all). Fixed by capping each request at 5 minutes — past that, the system now returns a clear error instead of hanging silently.
- **Backup-model resilience increased from 1 model to 5:** Previously there was a single backup model; now each function has a ranked list of 5, ordered using real quota data, deliberately including a model that draws from a *separate* quota pool from the primary model. This reduces the chance of the whole system going down at once during a usage spike — all 5 backup models have now been tested and confirmed working (100%).

## 🕒 Previous Updates — v2.1

**Status: Shipped**

- **Downtime risk fully closed out (at the time):** The selected backup model (Gemma 4) was tested and confirmed working end-to-end.
- **Prevents unbounded memory growth (Session Cleanup):** Chat/draft sessions that sit idle for more than 8 hours are now automatically cleared from the system's memory — preventing slowdowns or crashes if the tool stays running continuously for weeks without a restart.
- **Easier to spot missing information in drafts:** Word documents downloaded from Draft Mode now bold and color-flag (in red) any point the AI marked as needing more input, reducing the chance a reviewer overlooks one.

## 🕒 Previous Updates — v2.0

**Status: Shipped**

- **Prevents service interruption under heavy load (Auto Model Fallback):** When the primary AI model exhausts its quota during peak usage, the system automatically switches to a backup model instead of going down — reducing downtime risk with no manual intervention required.
- **Chat can now reference drafts mid-conversation:** After an employee creates a policy draft, they can return to normal chat and keep asking about it (e.g. "explain section 3 of the draft") — cutting down repeated back-and-forth. The system always labels this content clearly as unapproved, and it's only visible within the same session, so it never gets confused with live policy.

## 🕒 Earlier Updates

### v1.1 — Added a clarifying-questions step before drafting
**Status: Shipped**
Fixed a risk found during testing: the system previously drafted documents immediately without asking for missing information, risking that it would "guess" company-specific facts not covered in existing documents (e.g. a PDPA policy topic requiring details of the company's own personal-data handling). The system now always asks about the specific gaps before drafting. Unanswered questions are fine — the system flags the missing points directly in the draft instead of guessing.

### v1.0 — New-policy drafting mode with automatic self-review
**Status: Shipped**
Added the ability for the system to draft entirely new policies (not just retrieve existing ones), with the AI immediately critiquing its own draft against real existing policy. Findings are ranked by severity (critical / needs review / minor / overall verdict). **Every draft must be reviewed and approved by a human before real use — no exceptions.**

---

## 💼 Business Value

- **Less time spent searching for policy** — employees ask in plain language instead of opening files one by one, with every answer citing the source document and section for verification.
- **A head start on drafting new policies** — reduces the time a team spends starting a new policy from scratch; the system drafts an outline consistent with the organization's existing format, so the team can focus on review and refinement.
- **Helps review existing policies (v2.2)** — when an old policy needs updating, the system points to what to check and which related documents matter, cutting the time spent re-reading the whole thing manually.
- **Predictable cost** — no external SaaS subscription fees; the main cost is Gemini API usage, scaling directly with actual usage.

## 🔒 Security and Risks Already Managed

| Risk | Mitigation in place |
|---|---|

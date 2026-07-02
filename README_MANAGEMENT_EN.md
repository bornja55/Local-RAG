*Read this in other languages: [🇹🇭 ภาษาไทย](README_MANAGEMENT.md), [🇬🇧 English](README_MANAGEMENT_EN.md)*

# 🤖 Policy RAG Assistant — Leadership Summary (v2.0)

![Status](https://img.shields.io/badge/Status-Live%20%26%20Tested-2ECC71?style=for-the-badge)
![Data](https://img.shields.io/badge/Data-On--Prem%20Only-3498DB?style=for-the-badge)
![Review](https://img.shields.io/badge/AI%20Drafts-Require%20Human%20Approval-E67E22?style=for-the-badge)

An AI assistant that answers employee questions about **Origin Global Empire PLC's** policies and procedures, so staff no longer have to open documents one by one. It also drafts new policies as a starting point for the team to review — every answer is grounded strictly in real company documents, with no fabricated responses.

---

## 🆕 Latest Update — v2.0

**Status: Shipped** — verified with 6/6 end-to-end test cases, ready for use.

- **Prevents service interruption under heavy load (Auto Model Fallback):** When the primary AI model exhausts its quota during peak usage, the system automatically switches to a backup model instead of going down — reducing downtime risk with no manual intervention required. (Off by default; the team still needs to choose and validate a backup model before enabling it — see "Decisions Needed" below.)
- **Chat can now reference drafts mid-conversation:** After an employee creates a policy draft, they can return to normal chat and keep asking about it (e.g. "explain section 3 of the draft") — cutting down repeated back-and-forth. The system always labels this content clearly as unapproved, and it's only visible within the same session, so it never gets confused with live policy.

## 🕒 Previous Updates

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
- **Predictable cost** — no external SaaS subscription fees; the main cost is Gemini API usage, scaling directly with actual usage.

## 🔒 Security and Risks Already Managed

| Risk | Mitigation in place |
|---|---|
| AI gives incorrect/fabricated answers in Q&A mode | Hard rule forces answers to come only from real documents ("iron rule") — never relaxed at any point during development |
| An AI-drafted document gets used without review | Every screen shows a clear warning + the AI critiques its own draft before handing it to a human + points of uncertainty are explicitly flagged |
| Draft content gets mixed up with real policy in chat | Limited to the same session only, never saved to permanent storage, clearly labeled at every point |
| Company data leaving the organization | All documents and processing stay on internal machines; only the summarization call to the Gemini API leaves the machine |
| Service stops responding under heavy load | Automatic backup-model fallback is already built and ready (v2.0); pending the team's decision to enable it |

## 🗳️ Decisions Needed from Leadership / IT

1. **Choose a backup model** — the auto-fallback feature is ready but disabled by default; a backup model (e.g. Gemma 4) needs to be selected and quality-tested before enabling it in production.
2. **Gemini API budget** — cost scales with usage; if this is rolled out more broadly, budget should be estimated in advance.
3. **Storage planning for additional machines** — the system needs ~15GB of disk space per installed machine (AI models + libraries); plan ahead if deploying to more machines.

---

*This document is a summary for leadership and line managers. Full technical documentation is in [README.md](README.md); architectural decisions are recorded in [ADR.md](ADR.md).*

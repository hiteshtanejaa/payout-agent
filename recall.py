"""C7 memory check: recall finance notes about a merchant, then decide in plain Python.

Cognee does the retrieval (what does memory say about this merchant?).
The verdict is a deterministic keyword match on the retrieved lines, so the
same notes always give the same decision and no LLM decides whether to pay.

MEMORY_CHECK=cognee (default)  recall from Cognee; any error -> status "error" -> HOLD (fail closed)
MEMORY_CHECK=file              read notes/finance_notes.md directly (use if Cognee is down on demo day)
"""
import asyncio
import os

NOTES_PATH = os.path.join("notes", "finance_notes.md")


def _lines(texts):
    out = []
    for t in texts:
        for line in str(t).splitlines():
            line = line.strip(" -*\t")
            if line:
                out.append(line)
    return out


def _from_file():
    with open(NOTES_PATH, encoding="utf-8") as f:
        return [f.read()]


def check(merchant, policy):
    mode = os.getenv("MEMORY_CHECK", "cognee").lower()
    keywords = [k.lower() for k in policy["clauses"]["C7"]["hold_keywords"]]
    try:
        if mode == "file":
            texts, source = _from_file(), "file:" + NOTES_PATH
        else:
            import memory

            if not memory.HAVE_COGNEE:
                raise RuntimeError("cognee is not installed")
            texts, source = asyncio.run(memory.recall_notes(merchant)), "cognee:" + memory.NOTES_DATASET
    except Exception as e:  # fail closed: no memory, no money
        return {"status": "error", "source": mode, "detail": f"{type(e).__name__}: {e}"[:300], "notes": []}

    m = merchant.strip().lower()
    hits = [l for l in _lines(texts) if m and m in l.lower() and any(k in l.lower() for k in keywords)]
    return {"status": "flagged" if hits else "clear", "source": source, "notes": hits[:3]}

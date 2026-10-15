"""LLM step: turn a free-text purchase request email into structured fields.

The model only extracts. It never decides and never touches Stripe; the
output goes straight into policy.evaluate(). Uses litellm with the same
LLM_MODEL / LLM_API_KEY that Cognee uses, so there is one key for everything.
"""
import json
import os

KNOWN_OTHER_CATEGORIES = [
    "eating_places_restaurants",
    "electronics_stores",
    "taxicabs_limousines",
    "airlines_air_carriers",
    "hotels_motels_and_resorts",
]


def extract(text, policy):
    import litellm

    allowed = policy["clauses"]["C2"]["allowed_categories"]
    system = (
        "You extract purchase requests for a finance agent. Reply with JSON only: "
        '{"merchant": str, "category": str, "amount_gbp": number, "reason": str}. '
        "merchant is the company being paid (e.g. 'Google Ads', not 'Google'). "
        f"category must be a Stripe merchant category, preferably one of {allowed + KNOWN_OTHER_CATEGORIES}; "
        "pick the most accurate one even if it is not allowed, the policy will decide. "
        "amount_gbp is the total in pounds as a number. Do not invent values."
    )
    resp = litellm.completion(
        model=os.getenv("LLM_MODEL", "openai/gpt-5.6-luna"),
        api_key=os.getenv("LLM_API_KEY") or None,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    merchant = str(data.get("merchant") or "").strip()
    category = str(data.get("category") or "").strip()
    amount = float(data.get("amount_gbp") or 0)
    if not merchant or not category or amount <= 0:
        raise ValueError(f"incomplete extraction: {data}")
    return {"merchant": merchant, "category": category, "amount": amount, "reason": str(data.get("reason") or "")}

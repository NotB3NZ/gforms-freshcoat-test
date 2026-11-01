"""Exact, reviewed tier aliases. Never rewrite form values or infer from prices."""

TIERS = ("Lite", "Regular", "Pro")
APPROVED_TIER_ALIASES = {
    "regular": {"Lite (₱199)": "Lite", "Pro (₱349)": "Pro", "Regular (₱289)": "Regular"},
    "discount_15": {"Lite (₱170)": "Lite", "Pro (₱297)": "Pro", "Regular (₱246)": "Regular"},
    "discount_10": {"Lite (₱180)": "Lite", "Regular (₱261)": "Regular"},
}


def resolve_tier(source_id, raw_label):
    """Return a canonical tier or None; source, case, spacing and price are exact.

    Preserve the previously supported canonical values. Only the explicitly approved
    source-specific labels extend that set. Price category is deliberately not an
    input: a source category must never determine or override the ticket tier.
    """
    if not isinstance(source_id, str) or source_id not in APPROVED_TIER_ALIASES or not isinstance(raw_label, str):
        return None
    if raw_label in TIERS:
        return raw_label
    return APPROVED_TIER_ALIASES[source_id].get(raw_label)

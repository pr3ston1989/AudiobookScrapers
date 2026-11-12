"""Field-level parser coverage and service-aware completion requirements."""

FIELDS = ("title", "authors", "narrators", "publisher", "description", "cover_url", "duration_minutes",
          "length_str", "language", "category", "categories", "collections", "series", "volume_number",
          "avg_rating", "rating_count", "release_date", "premiere_date", "release_year", "isbn", "pages",
          "original_title", "translator", "format", "tags", "why_worth_it", "reviews")


def assess(site, data):
    """Separate required metadata from legitimate optional or inapplicable fields."""
    available = [field for field in FIELDS if data.get(field) is not None and data.get(field) != "" and data.get(field) != []]
    if "duration_minutes" in available:
        try:
            positive = float(data["duration_minutes"]) > 0
        except (TypeError, ValueError):
            positive = False
        if not positive:
            available.remove("duration_minutes")
    required = ["title", "authors", "description", "cover_url"]
    if site == 'wolnelektury':
        required.remove('description')
    if site != "lubimyczytac" and data.get("type") != "ebook":
        required += ["duration_minutes"]
        if site != "wolnelektury":
            required += ["publisher"]
    missing = [field for field in required if field not in available]
    return {"complete": not missing, "missing_required": missing, "available_fields": available,
            "optional_absent": [field for field in FIELDS if field not in available and field not in missing]}

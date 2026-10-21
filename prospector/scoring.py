"""Deterministic prospect score and priority tier (config/scoring.yaml)."""
from .config import scoring
from .website import is_mobile


def score(p: dict, category: str | None) -> tuple[int | None, int, list[str]]:
    """Returns (score or None if excluded, tier, reasons)."""
    cfg = scoring()
    reasons = []
    if p.get("blocked"):
        return None, 6, ["isključeno: blokiran / stečaj / likvidacija"]

    points = 0
    revenue = p.get("revenue")
    if revenue is None:
        points += cfg["unknown_revenue_points"]
        reasons.append("promet nepoznat")
    else:
        for band in cfg["revenue_bands"]:
            if revenue >= band["min"] and (band["max"] is None or revenue < band["max"]):
                points += band["points"]
                reasons.append(f"promet {band['label']}")
                break
    if p.get("website_ok"):
        points += cfg["has_website"]
        reasons.append("web")
    if p.get("general_keywords"):
        points += cfg["general_keywords"]
        reasons.append("opće ključne riječi")
    if category and p.get("category_keywords"):
        points += cfg["category_keywords"]
        reasons.append("ključne riječi kategorije")
    if any(is_mobile(x) for x in p.get("phones", [])):
        points += cfg["mobile_phone"]
        reasons.append("mobitel")
    if p.get("emails"):
        points += cfg["email"]
        reasons.append("e-mail")
    segment = p.get("segment")
    if segment in cfg["core_segments"]:
        points += cfg["specialised_bonus"]
    if segment == "elektro":
        points += cfg["electrician_penalty"]
        reasons.append("električar (niži prioritet)")
    maximum = _maximum_points(cfg)
    if p.get("is_relevant") is False:
        points = min(points, round(maximum * 0.2))
        reasons.append("vjerojatno nije relevantan")

    status = p.get("status", "New")
    specialised = segment in cfg["core_segments"]
    if status == "Active":
        tier = 5
    elif status.startswith("Inactive") and (revenue or 0) > 50000:
        tier = 3
    elif p.get("is_relevant") is False:
        tier = 6
    elif revenue is None or revenue == 0:
        tier = 6
    elif revenue > 100000:
        tier = 1 if specialised else 2
    else:
        tier = 4
    score = round(max(0, min(maximum, points)) * 100 / maximum) if maximum else 0
    return score, tier, reasons


def _maximum_points(cfg: dict) -> int:
    """Maximum raw points from the configured positive scoring factors."""
    revenue = max((band["points"] for band in cfg["revenue_bands"]), default=0)
    return max(0, revenue + cfg["has_website"] + cfg["general_keywords"]
               + cfg["category_keywords"] + cfg["mobile_phone"] + cfg["email"]
               + cfg["specialised_bonus"])

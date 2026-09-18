#!/usr/bin/env python3
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "scopus.csv"
OUTPUT = ROOT / "scopus_enriched.csv"
CACHE_DIR = ROOT / "data" / "api_cache"
STATUS_FILE = ROOT / "data" / "update_status.json"

API_KEY = os.getenv("ELSEVIER_API_KEY", "").strip()
CACHE_DAYS = int(os.getenv("SCOPUS_CACHE_DAYS", "7"))
FORCE_REFRESH = os.getenv("FORCE_REFRESH", "").lower() in {"1", "true", "yes"}

ADDED_FIELDS = [
    "CiteScore",
    "BestPercentile",
    "Quartile",
    "MetricStatus",
    "SJR",
    "SNIP",
    "QuartileMethod",
    "API_UpdatedAt",
]

def now_utc():
    return datetime.now(timezone.utc)

def iso_now():
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")

def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def normalize_issn(value):
    text = str(value or "")
    # Scopus exports sometimes contain formatted or unformatted ISSNs.
    matches = re.findall(r"(?i)(\d{4})-?(\d{3}[\dX])", text)
    if not matches:
        return ""
    a, b = matches[0]
    return (a + b).upper()

def formatted_issn(issn):
    return f"{issn[:4]}-{issn[4:]}" if len(issn) == 8 else issn

def quartile_from_percentile(p):
    if p >= 75:
        return "Q1"
    if p >= 50:
        return "Q2"
    if p >= 25:
        return "Q3"
    return "Q4"

def cache_path(issn):
    return CACHE_DIR / f"{issn}.json"

def read_cache(issn):
    p = cache_path(issn)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(obj["fetched_at"].replace("Z", "+00:00"))
        if FORCE_REFRESH or now_utc() - fetched > timedelta(days=CACHE_DAYS):
            return None
        return obj.get("response")
    except Exception:
        return None

def write_cache(issn, response):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": iso_now(), "response": response}
    cache_path(issn).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

def api_get(issn):
    cached = read_cache(issn)
    if cached is not None:
        return cached, "cache"

    if not API_KEY:
        raise RuntimeError("ELSEVIER_API_KEY GitHub Secret topilmadi.")

    urls = [
        f"https://api.elsevier.com/content/serial/title/issn/{issn}?view=CITESCORE",
        f"https://api.elsevier.com/content/serial/title/issn/{formatted_issn(issn)}?view=CITESCORE",
    ]

    last_error = None
    for url in urls:
        for attempt in range(1, 4):
            req = Request(
                url,
                headers={
                    "X-ELS-APIKey": API_KEY,
                    "Accept": "application/json",
                    "User-Agent": "BuxDU-Scopus-Dashboard/1.0",
                },
            )
            try:
                with urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    write_cache(issn, data)
                    return data, "api"
            except HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code in (401, 403):
                    raise RuntimeError(
                        f"Elsevier API {e.code}: API key yoki entitlement/ruxsat muammosi."
                    )
                if e.code == 429:
                    time.sleep(5 * attempt)
                    continue
                if e.code == 404:
                    break
                time.sleep(2)
            except (URLError, TimeoutError, json.JSONDecodeError) as e:
                last_error = str(e)
                time.sleep(2 * attempt)
    return None, last_error or "API error"

def first_entry(api_obj):
    try:
        entries = as_list(api_obj["serial-metadata-response"]["entry"])
        return entries[0] if entries else None
    except Exception:
        return None

def exact_year_metric(api_obj, year):
    entry = first_entry(api_obj)
    if not entry:
        return None

    year_infos = as_list((entry.get("citeScoreYearInfoList") or {}).get("citeScoreYearInfo"))
    yi = next((x for x in year_infos if str(x.get("@year", "")).strip() == str(year)), None)
    if not yi:
        return None

    infos = []
    for block in as_list(yi.get("citeScoreInformationList")):
        infos.extend(as_list((block or {}).get("citeScoreInfo")))
    info = next((x for x in infos if str((x or {}).get("docType", "")).lower() == "all"), None)
    if not info and infos:
        info = infos[0]
    if not info:
        return None

    percentiles = []
    for rank in as_list(info.get("citeScoreSubjectRank")):
        try:
            percentiles.append(int(rank.get("percentile")))
        except Exception:
            pass
    if not percentiles:
        return None

    best = max(percentiles)

    def year_value(container_name, item_name):
        container = entry.get(container_name) or {}
        for item in as_list(container.get(item_name)):
            if str((item or {}).get("@year", "")).strip() == str(year):
                return str((item or {}).get("$", "")).strip()
        return ""

    return {
        "CiteScore": str(info.get("citeScore", "") or ""),
        "BestPercentile": str(best),
        "Quartile": quartile_from_percentile(best),
        "MetricStatus": str(yi.get("@status", "") or ""),
        "SJR": year_value("SJRList", "SJR"),
        "SNIP": year_value("SNIPList", "SNIP"),
        "QuartileMethod": "Best CiteScore subject percentile",
    }

def main():
    if not INPUT.exists():
        raise FileNotFoundError(f"{INPUT.name} topilmadi.")

    with INPUT.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fields = reader.fieldnames or []

    if not rows:
        raise RuntimeError("scopus.csv bo'sh.")

    issns = sorted({normalize_issn(r.get("ISSN")) for r in rows if normalize_issn(r.get("ISSN"))})
    print(f"Rows: {len(rows)} | Unique ISSN: {len(issns)}")

    api_cache = {}
    api_stats = Counter()

    for i, issn in enumerate(issns, 1):
        print(f"[{i}/{len(issns)}] ISSN {issn}")
        obj, source = api_get(issn)
        api_cache[issn] = obj
        api_stats[source] += 1
        # Gentle pacing only for live API calls.
        if source == "api":
            time.sleep(0.25)

    q_counts = Counter()
    matched = 0
    updated_at = iso_now()

    for row in rows:
        issn = normalize_issn(row.get("ISSN"))
        try:
            year = int(float(str(row.get("Year", "")).strip()))
        except Exception:
            year = None

        metric = exact_year_metric(api_cache.get(issn), year) if issn and year else None

        for field in ADDED_FIELDS:
            row[field] = ""

        if metric:
            matched += 1
            row.update(metric)
            row["API_UpdatedAt"] = updated_at
            q_counts[row["Quartile"]] += 1
        else:
            row["Quartile"] = "Aniqlanmagan"
            row["QuartileMethod"] = "Exact publication-year CiteScore metric not available"
            row["API_UpdatedAt"] = updated_at
            q_counts["Aniqlanmagan"] += 1

    fields = original_fields + [x for x in ADDED_FIELDS if x not in original_fields]
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    status = {
        "updated_at_utc": updated_at,
        "rows": len(rows),
        "unique_issn": len(issns),
        "matched_publication_year_metrics": matched,
        "quartiles": {q: q_counts.get(q, 0) for q in ["Q1", "Q2", "Q3", "Q4", "Aniqlanmagan"]},
        "api_sources": dict(api_stats),
        "cache_days": CACHE_DAYS,
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(status, ensure_ascii=False, indent=2))
    print(f"Created: {OUTPUT.name}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

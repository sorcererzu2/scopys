#!/usr/bin/env python3
import csv
import json
import os
import re
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

# Journal/source metric cache.
CACHE_DAYS = int(os.getenv("SCOPUS_CACHE_DAYS", "7"))

# Author Retrieval METRICS cache.
# 200/day means the most frequent authors are populated first. On later daily runs
# another group is populated, while already cached authors are reused.
AUTHOR_CACHE_DAYS = int(os.getenv("SCOPUS_AUTHOR_CACHE_DAYS", "30"))
AUTHOR_API_LIMIT = int(os.getenv("SCOPUS_AUTHOR_API_LIMIT", "200"))

FORCE_REFRESH = os.getenv("FORCE_REFRESH", "").lower() in {"1", "true", "yes"}

AUTHOR_METRICS_FIELD = "ScopusAuthorMetricsJSON"

ADDED_FIELDS = [
    "CiteScore",
    "BestPercentile",
    "Quartile",
    "MetricStatus",
    "SJR",
    "SNIP",
    "QuartileMethod",
    "API_UpdatedAt",
    AUTHOR_METRICS_FIELD,
]

def now_utc():
    return datetime.now(timezone.utc)

def iso_now():
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")

def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def split_author_ids(value):
    out = []
    seen = set()
    for token in str(value or "").split(";"):
        author_id = token.strip()
        if re.fullmatch(r"\d{6,}", author_id) and author_id not in seen:
            out.append(author_id)
            seen.add(author_id)
    return out

def normalize_issn(value):
    text = str(value or "")
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

# --------------------------
# Serial Title API cache/API
# --------------------------

def serial_cache_path(issn):
    return CACHE_DIR / f"{issn}.json"

def read_serial_cache(issn):
    p = serial_cache_path(issn)
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

def write_serial_cache(issn, response):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": iso_now(), "response": response}
    serial_cache_path(issn).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

def serial_api_get(issn):
    cached = read_serial_cache(issn)
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
                    "User-Agent": "BuxDU-Scopus-Dashboard/2.0",
                },
            )
            try:
                with urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    write_serial_cache(issn, data)
                    return data, "api"
            except HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code in (401, 403):
                    raise RuntimeError(
                        f"Elsevier Serial Title API {e.code}: API key yoki entitlement/ruxsat muammosi."
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

    info = next(
        (x for x in infos if str((x or {}).get("docType", "")).lower() == "all"),
        None,
    )
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

# --------------------------
# Scopus Author Retrieval API
# --------------------------
# We use view=METRICS. Elsevier documents this view as containing:
# document-count, cited-by-count, citations-count, h-index, coauthor-count.

def author_cache_path(author_id):
    # Stored inside the already-committed data/api_cache folder, so the
    # existing GitHub workflow does not need to change.
    return CACHE_DIR / f"author_{author_id}.json"

def read_author_cache_info(author_id):
    p = author_cache_path(author_id)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        fetched_s = str(obj.get("fetched_at", "") or "")
        fetched = datetime.fromisoformat(fetched_s.replace("Z", "+00:00"))
        fresh = (not FORCE_REFRESH) and (
            now_utc() - fetched <= timedelta(days=AUTHOR_CACHE_DAYS)
        )
        return {
            "response": obj.get("response"),
            "fetched_at": fetched_s,
            "fresh": fresh,
        }
    except Exception:
        return None

def write_author_cache(author_id, response, fetched_at=None):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = fetched_at or iso_now()
    payload = {"fetched_at": fetched_at, "response": response}
    author_cache_path(author_id).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

def author_api_get_live(author_id):
    if not API_KEY:
        return None, "NO_API_KEY"

    url = (
        f"https://api.elsevier.com/content/author/author_id/{author_id}"
        f"?view=METRICS"
    )

    last_error = "API error"
    for attempt in range(1, 4):
        req = Request(
            url,
            headers={
                "X-ELS-APIKey": API_KEY,
                "Accept": "application/json",
                "User-Agent": "BuxDU-Scopus-Dashboard/2.0",
            },
        )
        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                fetched_at = iso_now()
                write_author_cache(author_id, data, fetched_at)
                return {
                    "response": data,
                    "fetched_at": fetched_at,
                    "fresh": True,
                }, "api"
        except HTTPError as e:
            last_error = f"HTTP {e.code}"
            if e.code in (401, 403):
                # Author metrics are an enhancement. Do not destroy the
                # working quartile update if the account lacks this entitlement.
                return None, last_error
            if e.code == 404:
                fetched_at = iso_now()
                data = {"_status": "not_found"}
                write_author_cache(author_id, data, fetched_at)
                return {
                    "response": data,
                    "fetched_at": fetched_at,
                    "fresh": True,
                }, "not_found"
            if e.code == 429:
                time.sleep(6 * attempt)
                continue
            time.sleep(2 * attempt)
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            last_error = str(e)
            time.sleep(2 * attempt)

    return None, last_error

def to_int(value):
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None

def extract_author_metrics(api_obj, author_id, fetched_at=""):
    if not isinstance(api_obj, dict) or api_obj.get("_status") == "not_found":
        return None

    responses = as_list(api_obj.get("author-retrieval-response"))
    if not responses or not isinstance(responses[0], dict):
        return None

    resp = responses[0]
    core = resp.get("coredata") or {}

    h_index = to_int(resp.get("h-index"))
    if h_index is None:
        h_index = to_int(core.get("h-index"))

    document_count = to_int(core.get("document-count"))
    if document_count is None:
        document_count = to_int(resp.get("document-count"))

    cited_by_count = to_int(core.get("cited-by-count"))
    if cited_by_count is None:
        cited_by_count = to_int(resp.get("cited-by-count"))

    citations_count = to_int(core.get("citations-count"))
    if citations_count is None:
        citations_count = to_int(core.get("citation-count"))
    if citations_count is None:
        citations_count = to_int(resp.get("citations-count"))
    if citations_count is None:
        citations_count = to_int(resp.get("citation-count"))

    coauthor_count = to_int(resp.get("coauthor-count"))
    if coauthor_count is None:
        coauthor_count = to_int(core.get("coauthor-count"))

    # At least the key metrics must exist before we call this an official metric.
    if h_index is None and document_count is None:
        return None

    return {
        "author_id": author_id,
        "h_index": h_index,
        "document_count": document_count,
        "citations_count": citations_count,
        "cited_by_count": cited_by_count,
        "coauthor_count": coauthor_count,
        "source": "Scopus Author Retrieval API / METRICS",
        "updated_at": fetched_at,
    }

def load_and_refresh_author_metrics(rows):
    counts = Counter()
    for row in rows:
        # Count each author once per document.
        for author_id in set(split_author_ids(row.get("Author(s) ID"))):
            counts[author_id] += 1

    author_ids = sorted(counts, key=lambda x: (-counts[x], x))
    stores = {}
    pending = []
    stats = Counter()

    # Reuse every existing cache, including stale cache as fallback.
    # Missing/stale entries are refreshed in priority order.
    for author_id in author_ids:
        info = read_author_cache_info(author_id)
        if info:
            stores[author_id] = info
            if info["fresh"]:
                stats["cache_fresh"] += 1
            else:
                stats["cache_stale"] += 1
                pending.append(author_id)
        else:
            stats["cache_missing"] += 1
            pending.append(author_id)

    to_refresh = pending[:max(0, AUTHOR_API_LIMIT)]
    print(
        f"Unique authors: {len(author_ids)} | "
        f"Author API refresh this run: {len(to_refresh)} / {len(pending)} pending"
    )

    author_api_blocked = False
    for i, author_id in enumerate(to_refresh, 1):
        print(
            f"[AUTHOR {i}/{len(to_refresh)}] {author_id} "
            f"(dataset docs={counts[author_id]})"
        )
        info, source = author_api_get_live(author_id)
        stats[source] += 1

        if info:
            stores[author_id] = info

        if source in {"HTTP 401", "HTTP 403", "NO_API_KEY"}:
            author_api_blocked = True
            print(
                "Author Retrieval API ishlamadi/ruxsat yo'q. "
                "Kvartil yangilanishi davom etadi; CSV h-index fallback bo'lib qoladi."
            )
            break

        if source == "api":
            # Author Retrieval documented rate limit is lower than Serial Title.
            time.sleep(0.38)

    metrics = {}
    for author_id, info in stores.items():
        metric = extract_author_metrics(
            info.get("response"), author_id, info.get("fetched_at", "")
        )
        if metric:
            metrics[author_id] = metric

    return metrics, counts, {
        "unique_authors": len(author_ids),
        "official_metrics_available": len(metrics),
        "api_refresh_limit_per_run": AUTHOR_API_LIMIT,
        "pending_before_run": len(pending),
        "api_blocked_or_not_entitled": author_api_blocked,
        "sources": dict(stats),
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

    # 1) Journal CiteScore/quartile metrics
    issns = sorted(
        {normalize_issn(r.get("ISSN")) for r in rows if normalize_issn(r.get("ISSN"))}
    )
    print(f"Rows: {len(rows)} | Unique ISSN: {len(issns)}")

    serial_cache = {}
    serial_stats = Counter()

    for i, issn in enumerate(issns, 1):
        print(f"[SERIAL {i}/{len(issns)}] ISSN {issn}")
        obj, source = serial_api_get(issn)
        serial_cache[issn] = obj
        serial_stats[source] += 1
        if source == "api":
            time.sleep(0.25)

    # 2) Official author metrics. Top/high-frequency authors are refreshed first.
    author_metrics, author_doc_counts, author_status = load_and_refresh_author_metrics(rows)

    q_counts = Counter()
    matched = 0
    updated_at = iso_now()

    for row in rows:
        issn = normalize_issn(row.get("ISSN"))
        try:
            year = int(float(str(row.get("Year", "")).strip()))
        except Exception:
            year = None

        metric = exact_year_metric(serial_cache.get(issn), year) if issn and year else None

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

        # Only include metrics for authors actually present in this document.
        # This keeps the dashboard self-contained in scopus_enriched.csv and
        # requires NO change to the existing GitHub Actions git-add command.
        row_author_metrics = {}
        for author_id in split_author_ids(row.get("Author(s) ID")):
            if author_id in author_metrics:
                row_author_metrics[author_id] = author_metrics[author_id]

        row[AUTHOR_METRICS_FIELD] = json.dumps(
            row_author_metrics,
            ensure_ascii=False,
            separators=(",", ":"),
        )

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
        "quartiles": {
            q: q_counts.get(q, 0)
            for q in ["Q1", "Q2", "Q3", "Q4", "Aniqlanmagan"]
        },
        "serial_api_sources": dict(serial_stats),
        "serial_cache_days": CACHE_DAYS,
        "author_metrics": author_status,
        "author_cache_days": AUTHOR_CACHE_DAYS,
    }

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(status, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

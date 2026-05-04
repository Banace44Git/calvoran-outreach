"""Scrape die drei Plattformen über Apify und persistiere Roh-Daten in calvoran.raw_jobs.

Strategie:
- Indeed:        ein Run pro Suchbegriff, country=DE, ohne location.
- StepStone:     ein Run pro Suchbegriff, ohne location.
- Arbeitsagentur: ein Run für ALLE Suchbegriffe (searchQueries-Array unterstützt).

Default: 20 Items je Run. Mit --max-items überschreibbar.
Stadt-Filterung passiert NICHT hier — erst in 02_filter_dedup.py.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apify_client import ApifyClient
from tqdm import tqdm

from config.keywords import INCLUDE_QUERIES
from scripts._db import get_apify_token, get_client

INDEED_ACTOR = "automation-lab/indeed-scraper"
STEPSTONE_ACTOR = "unfenced-group/stepstone-de-scraper"
ARBEITSAGENTUR_ACTOR = "santamaria-automations/arbeitsagentur-de-scraper"


def run_indeed(client: ApifyClient, query: str, max_items: int) -> tuple[str, list[dict]]:
    run_input = {
        "query": query,
        "country": "DE",
        "maxItems": max_items,
        "includeDescription": True,
        "datePosted": "14",
    }
    run = client.actor(INDEED_ACTOR).call(run_input=run_input, wait_secs=600, memory_mbytes=512)
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    return run["id"], items


def run_stepstone(client: ApifyClient, query: str, max_items: int) -> tuple[str, list[dict]]:
    run_input = {
        "searchQuery": query,
        "maxItems": max_items,
        "daysOld": 14,
        "skipReposts": True,
    }
    run = client.actor(STEPSTONE_ACTOR).call(run_input=run_input, wait_secs=900, memory_mbytes=1024)
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    return run["id"], items


def run_arbeitsagentur(client: ApifyClient, queries: list[str], max_per_query: int) -> tuple[str, list[dict]]:
    run_input = {
        "searchQueries": queries,
        "maxResultsPerQuery": max_per_query,
        "maxResults": max_per_query * len(queries),
        "includeJobDetails": True,
    }
    run = client.actor(ARBEITSAGENTUR_ACTOR).call(run_input=run_input, wait_secs=1800, memory_mbytes=1024)
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    return run["id"], items


def extract_external_id(source: str, item: dict) -> str | None:
    """Heuristik je Quelle. Wird in raw_jobs.external_id gespeichert."""
    if source == "indeed":
        return item.get("jobId") or item.get("id") or item.get("jobUrl") or item.get("url")
    if source == "stepstone":
        return item.get("jobId") or item.get("id") or item.get("url") or item.get("jobUrl")
    if source == "arbeitsagentur":
        return item.get("id") or item.get("refnr") or item.get("source_url")
    return None


def persist(db, source: str, query: str, run_id: str, items: list[dict]) -> int:
    """Schreibt Items in raw_jobs. Duplikate (source+external_id) werden via upsert ignoriert."""
    rows = []
    for it in items:
        ext_id = extract_external_id(source, it)
        if not ext_id:
            continue
        rows.append({
            "source": source,
            "external_id": str(ext_id),
            "search_query": query,
            "apify_run_id": run_id,
            "payload": it,
        })
    if not rows:
        return 0
    # upsert auf (source, external_id)
    res = db.table("raw_jobs").upsert(rows, on_conflict="source,external_id").execute()
    return len(res.data or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Indeed/StepStone/Arbeitsagentur via Apify")
    parser.add_argument("--max-items", type=int, default=20, help="Max Items je Run (Default: 20)")
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=["indeed", "stepstone", "arbeitsagentur"],
        default=["indeed", "stepstone", "arbeitsagentur"],
        help="Welche Plattformen scrapen",
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        help="Override Suchbegriffe (Default: alle aus config/keywords.py)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Nur erste Query je Quelle")
    args = parser.parse_args()

    queries = args.queries or INCLUDE_QUERIES
    if args.dry_run:
        queries = queries[:1]

    apify = ApifyClient(get_apify_token())
    db = get_client()

    total_persisted = 0
    started = time.time()

    # Arbeitsagentur in einem Rutsch
    if "arbeitsagentur" in args.sources:
        print(f"\n→ Arbeitsagentur: {len(queries)} queries × {args.max_items} = max {len(queries)*args.max_items}")
        try:
            run_id, items = run_arbeitsagentur(apify, queries, args.max_items)
            n = persist(db, "arbeitsagentur", "+".join(queries), run_id, items)
            print(f"  ✓ {len(items)} items received, {n} persisted (run {run_id})")
            total_persisted += n
        except Exception as exc:
            print(f"  ✗ Arbeitsagentur failed: {exc}")

    # Indeed + StepStone parallelisieren über alle Queries
    jobs = []
    if "indeed" in args.sources:
        for q in queries:
            jobs.append(("indeed", q, run_indeed))
    if "stepstone" in args.sources:
        for q in queries:
            jobs.append(("stepstone", q, run_stepstone))

    if jobs:
        print(f"\n→ Indeed/StepStone: {len(jobs)} parallele Runs (max_items={args.max_items})")
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(fn, apify, q, args.max_items): (src, q)
                for src, q, fn in jobs
            }
            for fut in tqdm(as_completed(futures), total=len(futures)):
                src, q = futures[fut]
                try:
                    run_id, items = fut.result()
                    n = persist(db, src, q, run_id, items)
                    total_persisted += n
                    tqdm.write(f"  ✓ {src}/{q!r}: {len(items)} items, {n} persisted")
                except Exception as exc:
                    tqdm.write(f"  ✗ {src}/{q!r}: {exc}")

    print(f"\nDone in {time.time()-started:.1f}s. Persisted: {total_persisted} rows.")


if __name__ == "__main__":
    main()

"""Fetcht Actor-Metadata + Input-Schema für die drei Job-Scraper.
Output landet als JSON in config/, damit die Schemas später als Referenz dienen."""

import json
import os
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()

ACTORS = [
    "automation-lab/indeed-scraper",
    "unfenced-group/stepstone-de-scraper",
    "santamaria-automations/arbeitsagentur-de-scraper",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "config"
OUT_DIR.mkdir(exist_ok=True)


def main() -> None:
    token = os.environ.get("APIFY_TOKEN") or os.environ["APIFY_API_KEY"]
    client = ApifyClient(token)

    for actor_id in ACTORS:
        print(f"\n=== {actor_id} ===")
        try:
            actor = client.actor(actor_id).get()
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue

        if not actor:
            print("  not found")
            continue

        title = actor.get("title", "—")
        username = actor.get("username", "—")
        name = actor.get("name", "—")
        stats = actor.get("stats", {})
        runs = stats.get("totalRuns", "—")

        print(f"  Title: {title}")
        print(f"  Slug:  {username}/{name}")
        print(f"  Runs:  {runs}")

        version = actor.get("defaultRunOptions", {})
        print(f"  Default mem MB: {version.get('memoryMbytes')}")

        latest = actor.get("versions", [])
        input_schema = None
        if latest:
            latest_ver = latest[-1]
            input_schema = latest_ver.get("inputSchema")

        if not input_schema:
            try:
                build = client.actor(actor_id).default_build().get()
                if build:
                    input_schema = build.get("inputSchema")
            except Exception as exc:
                print(f"  build fetch error: {exc}")

        slug = actor_id.replace("/", "__")
        out_path = OUT_DIR / f"actor_{slug}.json"
        out_path.write_text(
            json.dumps(
                {
                    "id": actor_id,
                    "title": title,
                    "input_schema": input_schema,
                    "raw": actor,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        print(f"  → {out_path.name}")

        if input_schema:
            schema = (
                json.loads(input_schema)
                if isinstance(input_schema, str)
                else input_schema
            )
            props = schema.get("properties", {}) if isinstance(schema, dict) else {}
            required = schema.get("required", []) if isinstance(schema, dict) else []
            print(f"  Properties ({len(props)}):")
            for key, val in props.items():
                req = " *" if key in required else ""
                desc = (val.get("title") or val.get("description") or "")[:60]
                print(f"    - {key}{req}: {desc}")


if __name__ == "__main__":
    main()

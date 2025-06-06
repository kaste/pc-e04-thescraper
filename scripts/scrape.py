import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from typing import Required, TypedDict

import aiohttp
from bs4 import BeautifulSoup
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, TaskProgressColumn
from rich.console import Console
from datetime import timedelta


BASE_URL = "https://packagecontrol.io/packages/{}"
DEFAULT_REGISTRY = "./registry.json"
DEFAULT_OUTPUT = "./packages_info.json"

type Name = str
type IsoTimestamp = str

class PackageInfo(TypedDict, total=False):
    name: Required[Name]
    first_seen: str | None
    total_installs: int
    win_installs: int
    mac_installs: int
    linux_installs: int
    last_scraped: Required[IsoTimestamp]
    fail_reason: str | None


type OutputFormat = dict[Name, PackageInfo]


async def main(registry: str, output: str, limit: int | None = 20, if_older_than: float | None = None) -> None:
    console = Console()
    # Detect CI or non-interactive environment
    disable_progress = not console.is_terminal or os.environ.get("CI") == "true"
    try:
        with open(registry, "r", encoding="utf-8") as f:
            registry_ = json.load(f)
    except Exception as e:
        print(f"fatal: Error loading registry: {e}")
        return

    input_names = [pkg["name"] for pkg in registry_.get("packages", [])]
    existing_data = load_existing_data(output)

    now = datetime.now(timezone.utc)
    now_string = now.strftime("%Y-%m-%d %H:%M:%S")
    if_older_than_ = (now - timedelta(hours=if_older_than or 0)).strftime("%Y-%m-%d %H:%M:%S")
    to_scrape = packages_sorted_by_age(input_names, existing_data, if_older_than_)
    to_scrape = to_scrape[:limit]
    if not to_scrape:
        if if_older_than is not None:
            local_dt = (now - timedelta(hours=if_older_than)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            print(f"No packages to scrape based on if_older_than={local_dt}.")
            return
        print("Nothing to scrape.")

    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_package(session, name, now_string) for name in to_scrape]
        results = []
        with Progress(
            TextColumn("[bold blue]Scraping Packages:"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=False,  # Show the bar and stats after completion
            disable=disable_progress
        ) as progress:
            task_id = progress.add_task("scrape", total=len(tasks))
            for coro in asyncio.as_completed(tasks):
                res = await coro
                if res is not None:
                    results.append(res)
                progress.update(task_id, advance=1)

    # Merge with existing data
    for res in results:
        if res is None:
            continue
        existing_data[res["name"]] = res

    # Save back to output json as list
    with open(output, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, indent=2)

    elapsed = progress.tasks[task_id].finished_time or 0
    s_per_package = elapsed / len(results) if len(results) > 0 else 0
    print(f"Scraped {len(results)} packages, {s_per_package * 1000:.1f}ms per package.")
    print(f"Saved to {output}")


def load_existing_data(output_path) -> OutputFormat:
    if not os.path.exists(output_path):
        return {}
    with open(output_path, "r", encoding="utf-8") as f:
        return json.load(f)


def packages_sorted_by_age(input_packages: list[Name], existing_data: OutputFormat, if_older_than: str) -> list[Name]:
    return sorted(
        (
            name
            for name in input_packages
            if existing_data.get(name, {}).get("last_scraped", "1970-01-01 00:00:00") <= if_older_than
        ),
        key=lambda name: existing_data.get(name, {}).get(
            "last_scraped", "1970-01-01 00:00:00"
        ),
    )


async def fetch_package(session, name: Name, now: IsoTimestamp) -> PackageInfo | None:
    url_name = quote(name, safe="")
    url = BASE_URL.format(url_name)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"Failed to fetch {name}: HTTP {resp.status}")
                return {
                    "name": name,
                    "last_scraped": now,
                    "fail_reason": f"HTTP {resp.status}"
                }
            text = await resp.text()
    except Exception as e:
        print(f"Exception fetching {name}: {e}")
        return None

    soup = BeautifulSoup(text, "html.parser")

    # Parse first_seen
    first_seen_span = soup.select_one("#details > ul > li.first_seen > span")
    first_seen = parse_first_seen(first_seen_span) if first_seen_span else None

    # Parse installs
    installs_ul = soup.select_one("#installs > ul.totals")
    installs = parse_installs(installs_ul) if installs_ul else {}

    return {
        "name": name,
        "first_seen": first_seen,
        **installs,
        "last_scraped": now,
    }


def parse_first_seen(span):
    # attribute 'title' example: "2025-05-07T18:00:05Z"
    # replace T with space, strip trailing Z
    t = span.attrs.get("title")
    if not t:
        return None
    return t.replace("T", " ").rstrip("Z")


def parse_installs(ul) -> dict:
    # ul class="totals", children li:
    installs = {}

    for li in ul.find_all("li", recursive=False):
        # span with class total or platform
        label_span = li.find("span", class_=["total", "platform"])
        if not label_span:
            continue
        label = label_span.text.strip()
        # The neighbour span that has title attribute with number
        # It could be a sibling span with class installs, e.g. <span class="installs" title="102">
        installs_span = label_span.find_next_sibling("span")
        if not installs_span:
            continue
        try:
            count = int(installs_span.attrs.get("title", "0").replace(",", ""))
        except Exception:
            count = 0

        match label:
            case "Total":
                installs["total_installs"] = count
            case "Win":
                installs["win_installs"] = count
            case "Mac":
                installs["mac_installs"] = count
            case "Linux":
                installs["linux_installs"] = count
    return installs


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape packagecontrol.io package info"
    )
    parser.add_argument(
        "-r",
        "--registry",
        default=DEFAULT_REGISTRY,
        help=f"Input registry file with packages (Default: {DEFAULT_REGISTRY})"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON file to store results (Default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max number of packages to scrape"
    )
    parser.add_argument(
        "--no-limit",
        action="store_true",
        help="If set, scrape all packages (ignore --limit)"
    )
    parser.add_argument(
        "--if-older-than",
        type=float,
        default=None,
        help="Only scrape packages if last_scraped is older than N hours (or missing)"
    )
    args = parser.parse_args()
    if args.no_limit and any(a.startswith('--limit') for a in sys.argv):
        parser.error("Cannot use --limit and --no-limit together.")
    if args.no_limit:
        args.limit = None
    return args


if __name__ == "__main__":
    args = parse_args()
    registry = os.path.abspath(args.registry)
    output = os.path.abspath(args.output)
    asyncio.run(main(registry, output, args.limit, args.if_older_than))

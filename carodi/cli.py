from __future__ import annotations

import argparse
import logging
import os
import sys

from carodi.config import Config
from carodi.models import Opportunity
from carodi.pipeline import Funnel
from carodi.sinks import ConsoleSink, TelegramSink
from carodi.sources import build, known_types
from carodi.store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _sink(args: argparse.Namespace, config: Config):
    if args.dry_run or args.console:
        return ConsoleSink()
    return TelegramSink(
        token=os.environ.get("CARODI_TELEGRAM_TOKEN", ""),
        chat_id=os.environ.get("CARODI_TELEGRAM_CHAT_ID", ""),
        disable_preview=config.settings.get("telegram_disable_preview", True),
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    with Store(config.db_path) as store:
        funnel = Funnel(config, store)
        run_id = None if args.dry_run else store.start_run()

        result = funnel.run(dry_run=args.dry_run)

        logging.info(
            "fetched=%d deduped=%d passed=%d new=%d",
            result.fetched, result.after_dedupe, result.passed, len(result.new),
        )
        if args.explain:
            print("\n--- rejection reasons ---", file=sys.stderr)
            for reason, count in result.rejections.most_common(20):
                print(f"  {count:5d}  {reason}", file=sys.stderr)

        items = result.new[: args.limit or config.digest_limit]
        sink = _sink(args, config)
        sink.deliver(items, store.accountability(), result.source_errors)

        if not args.dry_run:
            store.mark_notified(o.fingerprint for o in items)
            if run_id is not None:
                store.finish_run(run_id, result.as_stats())
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    print("registered types:", ", ".join(known_types()))
    print("\nconfigured sources:")
    for entry in config.sources:
        params = entry.get("params", {})
        label = params.get("name", entry["type"])
        detail = ""
        if boards := params.get("boards"):
            detail = f" ({len(boards)} boards)"
        elif url := params.get("url"):
            detail = f" ({url})"
        print(f"  {entry['type']:16s} {label}{detail}")

    print("\nsponsor registers:")
    if not config.registers:
        print("  (none configured)")
    for reg in config.registers:
        state = "enabled" if reg.get("enabled", True) else "disabled"
        print(f"  {reg['country']:4s} {reg['path']}  [{state}]")
    return 0


def cmd_registers(args: argparse.Namespace) -> int:
    """Report sponsor-register health.

    Worth its own command: when a register is missing, every onsite role in that
    country is rejected as "not on the sponsor register", which looks exactly
    like a working filter that found nothing.
    """
    from carodi.enrichment.sponsors import SponsorRegister

    config = Config.load(args.config)
    ok = True
    loaded: list[SponsorRegister] = []
    for entry in config.registers:
        country, path = entry["country"], entry["path"]
        if not entry.get("enabled", True):
            print(f"  {country}  disabled")
            continue
        try:
            routes = entry.get("routes")
            reg = SponsorRegister(
                country=country,
                path=path,
                name_column=entry.get("name_column"),
                route_column=entry.get("route_column"),
                routes=tuple(routes) if routes else None,
            )
        except (FileNotFoundError, ValueError) as exc:
            ok = False
            print(f"  {country}  MISSING — {exc}")
            continue

        loaded.append(reg)
        excluded = f"  ({reg._skipped_routes:,} rows excluded by route)" if reg.routes else ""
        print(f"  {country}  {len(reg):>7,} employers  {path}{excluded}")

    # Probe against whatever loaded, even if another register is missing --
    # being told "NL is absent" is no reason to refuse to test a name against GB.
    if args.probe:
        print()
        for org in args.probe:
            for reg in loaded:
                found, matched, score = reg.lookup(org)
                verdict = f"{matched} ({score}%)" if found else "not found"
                print(f"  {org} -> {reg.country}: {verdict}")

    if not ok:
        print(
            "\nWhile a register is missing, every onsite role in that country is\n"
            "rejected for lack of a sponsor match. Either download the CSV or set\n"
            "hard_filters.require_sponsor_if_onsite: false in profile.yaml."
        )
        return 1
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Fetch a single source and print what it produces, without the pipeline."""
    config = Config.load(args.config)
    matches = [
        e for e in config.sources
        if e.get("params", {}).get("name") == args.name or e["type"] == args.name
    ]
    if not matches:
        print(f"no configured source matching {args.name!r}", file=sys.stderr)
        return 1

    for entry in matches:
        source = build(entry["type"], entry.get("params", {}))
        items: list[Opportunity] = list(source.fetch())
        print(f"\n{entry['type']} -> {len(items)} items")
        for opp in items[: args.limit]:
            print(f"  {opp.title}  |  {opp.org}  |  {opp.location_raw}")
            print(f"    {opp.url}")
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    from carodi.sources.deadlines import Deadlines

    config = Config.load(args.config)
    entry = next((e for e in config.sources if e["type"] == "deadlines"), None)
    if entry is None:
        print("no 'deadlines' source configured", file=sys.stderr)
        return 1

    cal = Deadlines(**entry.get("params", {}))
    upcoming = cal.upcoming(within_days=args.within)
    if not upcoming:
        print(f"nothing due in the next {args.within} days")
    for when, name in upcoming:
        print(f"  {when.isoformat()}  {name}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    with Store(config.db_path) as store:
        if not store.set_status(args.fingerprint, args.state, args.note):
            print(f"no opportunity with fingerprint {args.fingerprint!r}", file=sys.stderr)
            return 1
        print(f"{args.fingerprint} -> {args.state}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    with Store(config.db_path) as store:
        rows = store.open_items(limit=args.limit)
        if not rows:
            print("nothing undecided — inbox zero")
        for row in rows:
            print(f"  [{row['score']:g}] {row['fingerprint']}  {row['title']}")
            print(f"        {row['org']} · {row['location_raw']}")
            print(f"        {row['url']}")
        stats = store.accountability()
        print(
            f"\nLast {stats['days']}d: {stats['delivered']} delivered · "
            f"{stats['applied']} applied · {stats['undecided']} undecided"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carodi", description=__doc__)
    parser.add_argument("-c", "--config", default="config", help="config directory")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the funnel and deliver a digest")
    p_run.add_argument("--dry-run", action="store_true", help="no DB writes, print to console")
    p_run.add_argument("--console", action="store_true", help="write to console but still record")
    p_run.add_argument("--limit", type=int, default=0, help="max items in the digest")
    p_run.add_argument("--explain", action="store_true", help="print rejection breakdown")
    p_run.set_defaults(func=cmd_run)

    p_sources = sub.add_parser("sources", help="list source types and configured instances")
    p_sources.set_defaults(func=cmd_sources)

    p_reg = sub.add_parser("registers", help="check sponsor register health")
    p_reg.add_argument("--probe", nargs="*", default=[], help="test employer names against them")
    p_reg.set_defaults(func=cmd_registers)

    p_check = sub.add_parser("check", help="fetch one source raw, for debugging")
    p_check.add_argument("name", help="source name or type")
    p_check.add_argument("--limit", type=int, default=10)
    p_check.set_defaults(func=cmd_check)

    p_cal = sub.add_parser("calendar", help="show upcoming curated deadlines")
    p_cal.add_argument("--within", type=int, default=365, help="days ahead")
    p_cal.set_defaults(func=cmd_calendar)

    p_status = sub.add_parser("mark", help="record what you did about an opportunity")
    p_status.add_argument("fingerprint")
    p_status.add_argument("state", choices=["applied", "skipped", "notified"])
    p_status.add_argument("--note", default=None)
    p_status.set_defaults(func=cmd_status)

    p_open = sub.add_parser("open", help="list delivered items you have not decided on")
    p_open.add_argument("--limit", type=int, default=50)
    p_open.set_defaults(func=cmd_open)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

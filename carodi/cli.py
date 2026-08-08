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

    # Built before the funnel runs, not after. Constructing it validates the
    # Telegram credentials, and discovering they are missing should cost a
    # millisecond rather than a full thirty-second sweep of every source.
    sink = _sink(args, config)

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

        limit = args.limit or config.digest_limit
        # A dry run persists nothing, so the store has nothing to read back;
        # a real run must send everything still undelivered, not merely what
        # this run happened to discover.
        items = result.new[:limit] if args.dry_run else store.undelivered(limit)
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


def cmd_llm(args: argparse.Namespace) -> int:
    """Check the LLM stage: which models the key can reach, and a live extraction.

    Worth its own command because the stage runs unattended and degrades quietly
    by design -- a bad key or a renamed model would otherwise show up only as
    matches that never get enriched.
    """
    config = Config.load(args.config)
    cfg = config.settings.get("llm") or {}
    key_env = cfg.get("api_key_env", "CARODI_GEMINI_API_KEY")
    api_key = os.environ.get(key_env, "")

    print(f"enabled:  {bool(cfg.get('enabled'))}")
    print(f"model:    {cfg.get('model')}")
    print(f"key:      {'set via ' + key_env if api_key else 'MISSING (' + key_env + ')'}")
    if not api_key:
        return 1

    from google import genai

    client = genai.Client(api_key=api_key)

    if args.list_models:
        print("\nmodels this key can reach:")
        for m in client.models.list():
            if "generateContent" in (getattr(m, "supported_actions", None) or []):
                print(f"  {m.name}")
        return 0

    with Store(config.db_path) as store:
        from carodi.enrich_llm import LlmEnricher

        enricher = LlmEnricher(
            api_key=api_key, model=cfg.get("model", "gemini-2.5-flash"),
            store=store, min_interval=0.0,
            disable_thinking=cfg.get("disable_thinking", True),
        )
        sample = store.undecided(limit=1) or store.undelivered(limit=1)
        if not sample:
            print("\nno stored posting to test on — run `carodi run` first")
            return 1

        opp = sample[0]
        print(f"\ntest extraction on: {opp.title} @ {opp.org}")
        result = enricher.extract(opp)
        if result is None:
            print("  model returned nothing parseable")
            return 1
        for field, value in result.model_dump(mode="json").items():
            print(f"  {field:22s} {value}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Find public job boards belonging to employers who can sponsor you."""
    from carodi.discover import Discoverer, as_sources_yaml

    config = Config.load(args.config)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    with Store(config.db_path) as store:
        seeds = store.seed_orgs(sponsored_only=not args.all, limit=args.limit)
        if not seeds:
            print(
                "no seed employers yet.\n"
                "`carodi discover` learns from companies the funnel has seen, so run\n"
                "`carodi run` at least once first (a --dry-run does not record them)."
            )
            return 1

        print(f"seeding from {len(seeds)} employer(s)"
              f"{'' if args.all else ' on a sponsor register'}\n")

        disc = Discoverer(store, providers=providers, delay=args.delay)
        hits = disc.run(sponsored_only=not args.all, limit=args.limit)

        trusted = [h for h in hits if h.trusted]
        review = [h for h in hits if not h.trusted]

        print(f"\n{'=' * 62}")
        print(f"requests made:   {disc.requests_made}   (cache hits: {disc.cache_hits})")
        print(f"boards found:    {len(hits)}")
        print(f"  verified:      {len(trusted)}")
        print(f"  need review:   {len(review)}")
        print(f"{'=' * 62}\n")

        if trusted:
            print("VERIFIED — safe to add:")
            for h in sorted(trusted, key=lambda x: -x.confidence):
                jobs = f"{h.job_count} jobs" if h.job_count else ""
                print(f"  [{h.confidence:3d}] {h.provider:11s} {h.token:24s} "
                      f"{(h.declared_name or h.org)[:26]:28s} {'/'.join(h.sponsors):6s} {jobs}")

        if review:
            print("\nNEEDS REVIEW — provider does not declare a company name,"
                  "\nso the token could belong to someone else entirely:")
            for h in review:
                jobs = f"{h.job_count} jobs" if h.job_count else ""
                print(f"        {h.provider:11s} {h.token:24s} "
                      f"{h.org[:26]:28s} {'/'.join(h.sponsors):6s} {jobs}")

        if args.emit:
            print("\n# --- paste into config/sources.yaml under `sources:` ---")
            print(as_sources_yaml(hits))
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    """Long-poll for button taps from the digest. Runs until stopped."""
    from carodi.bot import Bot

    config = Config.load(args.config)
    bot = Bot(
        token=os.environ.get("CARODI_TELEGRAM_TOKEN", ""),
        chat_id=os.environ.get("CARODI_TELEGRAM_CHAT_ID", ""),
        db_path=config.db_path,
        poll_timeout=args.poll_timeout,
    )
    if args.once:
        with Store(config.db_path) as store:
            print(f"handled {bot.poll_once(store)} update(s)")
        return 0

    bot.run_forever()
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
        pending = store.undecided(limit=args.limit)
        if not pending:
            print("nothing undecided — inbox zero")
        for opp in pending:
            print(f"  [{opp.score:g}] {opp.fingerprint}  {opp.title}")
            print(f"        {opp.org} · {opp.location_raw}")
            print(f"        {opp.url}")
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

    p_llm = sub.add_parser("llm", help="check the LLM extraction stage")
    p_llm.add_argument("--list-models", action="store_true",
                       help="list models this API key can reach")
    p_llm.set_defaults(func=cmd_llm)

    p_disc = sub.add_parser("discover", help="find job boards of employers who can sponsor you")
    p_disc.add_argument("--all", action="store_true",
                        help="probe every seen employer, not just sponsor-verified ones")
    p_disc.add_argument("--limit", type=int, default=None, help="max employers to probe")
    p_disc.add_argument("--providers", default="greenhouse,lever,ashby")
    p_disc.add_argument("--delay", type=float, default=0.15, help="seconds between requests")
    p_disc.add_argument("--emit", action="store_true", help="print a sources.yaml block")
    p_disc.set_defaults(func=cmd_discover)

    p_bot = sub.add_parser("bot", help="handle Applied/Skipped taps from the digest")
    p_bot.add_argument("--once", action="store_true", help="drain pending updates and exit")
    p_bot.add_argument("--poll-timeout", type=int, default=50, help="long-poll seconds")
    p_bot.set_defaults(func=cmd_bot)

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
    except (FileNotFoundError, ValueError) as exc:
        # Misconfiguration, not a crash: a missing config file or absent
        # Telegram credentials should read as one line in the journal, not a
        # traceback that buries the one sentence telling you what to fix.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

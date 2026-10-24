"""Command line entry point for the GraphRAG demo.

Each module is one subcommand and writes one artifact, so you can test
them one at a time:

    python -m graphrag_demo.cli ingest      # M1  corpus
    python -m graphrag_demo.cli extract     # M2  entity/claim extraction
    python -m graphrag_demo.cli graph       # M3  graph + entity resolution
    python -m graphrag_demo.cli communities # M4  communities + summaries
    python -m graphrag_demo.cli query "..." # M5  retrieval
    python -m graphrag_demo.cli answer "..."# M6  grounded answer
    python -m graphrag_demo.cli evaluate    # M6  GraphRAG vs TF-IDF
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config

# The Windows console defaults to cp1252 and cannot print the ellipsis or
# the em dash the summaries contain.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover
    pass


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .corpus import write_corpus

    corpus = write_corpus()
    print(f"source: {corpus['source_path']}")
    print(f"sections: {len(corpus['sections'])}")
    print(f"units:    {len(corpus['units'])}")
    print(f"written:  {config.CORPUS_PATH}")

    if args.verbose:
        print()
        for section in corpus["sections"]:
            print(f"  {section['heading']}  (lines {section['start_line']}-{section['end_line']})")
        print()
        for unit in corpus["units"][: args.limit]:
            preview = unit["text"].replace("\n", " / ")[:110]
            print(f"  {unit['unit_id']:<38} {unit['kind']:<10} {preview}")
    return 0


def _cmd_sections(args: argparse.Namespace) -> int:
    from .corpus import load_corpus

    corpus = load_corpus()
    for section in corpus["sections"]:
        count = len(section["unit_ids"])
        print(
            f"{section['section_id']:<26} {count:>3} units  "
            f"lines {section['start_line']}-{section['end_line']}  {section['title']}"
        )
    return 0


def _cmd_units(args: argparse.Namespace) -> int:
    from .corpus import load_corpus

    corpus = load_corpus()
    units = corpus["units"]
    if args.section:
        units = [u for u in units if u["section_id"] == args.section]
    if args.kind:
        units = [u for u in units if u["kind"] == args.kind]

    for unit in units[: args.limit]:
        print(f"[{unit['unit_id']}] ({unit['kind']}, lines {unit['start_line']}-{unit['end_line']})")
        print(f"  {unit['text']}")
        print()
    print(f"{len(units)} unit(s)")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from .extract import run_extraction
    from .extract_rules import run_extraction_rules

    if args.engine == "rules":
        result = run_extraction_rules(limit=args.limit)
    else:
        result = run_extraction(limit=args.limit, force=args.force)

    print()
    print("totals:")
    for key, value in result["totals"].items():
        print(f"  {key:<14} {value}")
    if result.get("usage"):
        print()
        print("usage:", json.dumps(result["usage"]["by_label"], indent=2))
        print("tokens:", result["usage"]["total_tokens"], "|", result["cost"])
    print(f"written: {config.EXTRACTED_PATH}")
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    from .graph import build_graph

    graph = build_graph()
    stats = graph["stats"]
    for key, value in stats.items():
        print(f"  {key:<20} {value}")
    print(f"written: {config.GRAPH_PATH}")
    return 0


def _cmd_communities(args: argparse.Namespace) -> int:
    from .community import build_communities

    result = build_communities(force_summaries=args.force, use_llm=args.llm)
    print(f"levels: {result['levels']}   communities: {len(result['communities'])}")
    print()
    for comm in result["communities"]:
        print(
            f"  {comm['community_id']:<5} L{comm['level']}  {comm['size']:>2} nodes  "
            f"{comm['claim_count']:>3} claims  [{comm['summary_source']}]"
        )
        print(f"        {comm['title']}")
        print(f"        {comm['summary'][:150]}")
    print()
    print(f"written: {config.COMMUNITIES_PATH}")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    from .query import QueryEngine

    engine = QueryEngine()
    result = engine.query(args.question, mode=args.mode, top_k=args.top_k)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    from .answer import AnswerEngine

    engine = AnswerEngine()
    result = engine.answer(args.question, mode=args.mode)
    print(result["answer"])
    print()
    print("citations:")
    for cite in result["citations"]:
        print(f"  - {cite['label']}  verified={cite['verified']}")
    if result.get("abstained"):
        print("\n(abstained: evidence below threshold)")
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from .evaluate import evaluate

    report = evaluate()
    summary = report["summary"]
    print("SUMMARY")
    for key, value in summary.items():
        print(f"  {key:<32} {value}")
    print(f"written: {config.EVAL_PATH}")
    return 0


def _cmd_workflow(args: argparse.Namespace) -> int:
    from .core.orchestrator import Orchestrator

    orchestrator = Orchestrator(use_model=args.llm)
    result = orchestrator.run(args.question, write_trace=not args.no_trace)
    print(f"decision={result.decision}  route={result.route}  mode={result.mode}")
    print(f"confidence={result.confidence:.2f}  composer={result.composer}")
    print()
    print(result["answer"] if isinstance(result, dict) else result.answer)
    print()
    for citation in result.citations:
        print(f"  [{citation['label']}]")
    if result.usage:
        print(f"\nusage: {result.usage['calls']} calls, "
              f"{result.usage['total_tokens']} tokens | {result.usage['cost']}")
    print(f"\ntotal: {result.total_ms:.0f} ms (trace: {config.TRACE_PATH})")
    return 0


def _cmd_app(args: argparse.Namespace) -> int:
    import subprocess

    module_dir = Path(__file__).resolve().parent.parent
    app_path = Path(__file__).resolve().parent / "app.py"
    print(f"starting Streamlit on http://localhost:{args.port}")
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port",
            str(args.port),
            "--server.headless",
            "true" if args.headless else "false",
        ],
        cwd=str(module_dir),
    )


def _cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    return run_selftest()


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m graphrag_demo.cli",
        description="GraphRAG demo for the Kallang Auto Care FAQ knowledge base.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="M1: parse faq_kb.md into sections and text units")
    p.add_argument("--verbose", action="store_true", help="list sections and sample units")
    p.add_argument("--limit", type=int, default=15, help="how many units to preview")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("sections", help="list the parsed sections")
    p.set_defaults(func=_cmd_sections)

    p = sub.add_parser("units", help="inspect text units")
    p.add_argument("--section", help="filter by section id")
    p.add_argument("--kind", choices=["para", "list_item", "table_row"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_units)

    p = sub.add_parser("extract", help="M2: extract entities, aliases and claims")
    p.add_argument(
        "--engine",
        choices=["llm", "rules"],
        default="llm",
        help="llm = call the model; rules = deterministic, no API calls",
    )
    p.add_argument("--limit", type=int, default=0, help="only process N sections (0 = all)")
    p.add_argument("--force", action="store_true", help="re-extract even if cached")
    p.set_defaults(func=_cmd_extract)

    p = sub.add_parser("graph", help="M3: build the knowledge graph and resolve entities")
    p.set_defaults(func=_cmd_graph)

    p = sub.add_parser("communities", help="M4: detect communities and summarise them")
    p.add_argument("--force", action="store_true", help="re-summarise even if cached")
    p.add_argument(
        "--llm",
        action="store_true",
        help="summarise with the model (default: deterministic extractive summary)",
    )
    p.set_defaults(func=_cmd_communities)

    p = sub.add_parser("query", help="M5: retrieve from the graph (no answer generation)")
    p.add_argument("question")
    p.add_argument("--mode", choices=["local", "global", "hybrid"], default="hybrid")
    p.add_argument("--top-k", type=int, default=8)
    p.set_defaults(func=_cmd_query)

    p = sub.add_parser("answer", help="M6: grounded answer with citations")
    p.add_argument("question")
    p.add_argument("--mode", choices=["local", "global", "hybrid"], default="hybrid")
    p.set_defaults(func=_cmd_answer)

    p = sub.add_parser("evaluate", help="M6: benchmark GraphRAG against the TF-IDF baseline")
    p.set_defaults(func=_cmd_evaluate)

    p = sub.add_parser(
        "workflow",
        help="G9: the full Workflow A - guardrail, router, retrieval, compose, trace",
    )
    p.add_argument("question")
    p.add_argument("--llm", action="store_true", help="let the model phrase the answer")
    p.add_argument("--no-trace", action="store_true", help="do not write to the trace log")
    p.set_defaults(func=_cmd_workflow)

    p = sub.add_parser("app", help="G10: launch the Streamlit front-end")
    p.add_argument("--port", type=int, default=8600)
    p.add_argument("--headless", action="store_true", help="do not open a browser")
    p.set_defaults(func=_cmd_app)

    p = sub.add_parser("selftest", help="run the offline checks")
    p.set_defaults(func=_cmd_selftest)

    p = sub.add_parser("doctor", help="check credentials, packages and artifacts")
    p.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"missing file: {exc.filename}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx

from .demo import run_demo
from .errors import InterfaceError
from .fhir import transform, validate_resource
from .hl7 import parse
from .http import ClientCredentials, FhirClient
from .ledger import Ledger
from .mllp import listening, send
from .operations import healthy, probe, summarize_logs
from .pipeline import Bridge


@contextmanager
def api(config):
    with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
        tokens = ClientCredentials(
            config["token_endpoint"],
            config["client_id"],
            os.environ.get("BRIDGE_CLIENT_SECRET", ""),
            config["scope"],
            client,
        )
        yield FhirClient(config["fhir_base"], tokens, client), client


def parser():
    root = argparse.ArgumentParser(description="HL7/FHIR modernization bridge and operations")
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the isolated socket-based integration exercise")
    demo.add_argument("--samples", type=Path, default=Path("samples"))
    for command in ("validate", "transform", "logs", "source-record"):
        sub = commands.add_parser(command)
        sub.add_argument("file", type=Path)
        if command == "validate":
            sub.add_argument("--format", choices=("hl7", "json"), default="hl7")
        if command == "source-record":
            sub.add_argument("--database", required=True)
    sender = commands.add_parser("send")
    sender.add_argument("file", type=Path)
    sender.add_argument("--host", default="127.0.0.1")
    sender.add_argument("--port", type=int, default=2575)
    for command in ("serve", "probe", "reconcile", "health", "exceptions", "requeue"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", type=Path, default=Path("config/local.json"))
        if command == "reconcile":
            sub.add_argument(
                "--refresh", action="store_true", help="Read current destination resources"
            )
        if command == "health":
            sub.add_argument("--max-age", type=float, default=60)
        if command == "requeue":
            sub.add_argument("id", type=int)
    return root


def execute(args):
    if args.command == "demo":
        return run_demo(args.samples), 0
    if args.command == "validate":
        if args.format == "hl7":
            parse(args.file.read_text())
        else:
            validate_resource(json.loads(args.file.read_text()))
        return {"valid_for_project_profile": True}, 0
    if args.command == "transform":
        return transform(parse(args.file.read_text())), 0
    if args.command == "logs":
        with args.file.open() as lines:
            return summarize_logs(lines), 0
    if args.command == "send":
        return {"ack": send(args.host, args.port, args.file.read_text())}, 0
    if args.command == "source-record":
        message = parse(args.file.read_text())
        ledger = Ledger(args.database)
        try:
            ledger.record_source(message.source_key, message.raw)
            return {"source_manifest_recorded": True}, 0
        finally:
            ledger.close()
    config = json.loads(args.config.read_text())
    if args.command == "probe":
        with httpx.Client(timeout=8, trust_env=False) as client:
            return probe(config["fhir_base"], client), 0
    ledger = Ledger(config["database"])
    try:
        if args.command == "health":
            summary = ledger.health()
            return summary, 0 if healthy(summary, args.max_age) else 1
        if args.command == "exceptions":
            return ledger.exception_rows(), 0
        if args.command == "requeue":
            ledger.requeue(args.id)
            return {"requeued": args.id}, 0
        if args.command == "reconcile":
            if args.refresh:
                with api(config) as (destination, _):
                    snapshots = {}
                    for path in ledger.resource_paths():
                        try:
                            resource = destination.request("GET", "/" + path)
                            snapshots[path] = {k: v for k, v in resource.items() if k != "meta"}
                        except InterfaceError as exc:
                            if exc.code != "http.404":
                                raise
                    ledger.snapshots(snapshots)
            summary = ledger.reconcile()
            success = all(r["outcome"] == "delivered" for r in summary["transactions"]) and all(
                r["outcome"] == "matched" for r in summary["resources"]
            )
            return summary, 0 if success else 1
        if args.command == "serve":
            if config["listen_host"] != "127.0.0.1":
                raise ValueError("Reference listener requires a loopback bind")
            with api(config) as (destination, _):
                bridge = Bridge(
                    ledger,
                    destination,
                    max_attempts=config["max_attempts"],
                    retry_base=config["retry_base_seconds"],
                )
                with ledger.lock, ledger.db:
                    ledger.db.execute(
                        "UPDATE transactions SET state='pending' WHERE state='delivering'"
                    )
                with listening(bridge, config["listen_host"], config["listen_port"]):
                    stop = threading.Event()
                    try:
                        while True:
                            if not bridge.dispatch_one():
                                stop.wait(0.1)
                    except KeyboardInterrupt:
                        return {"stopped": True}, 0
    finally:
        ledger.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parser().parse_args()
    try:
        result, status = execute(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return status
    except (InterfaceError, ValueError, KeyError, OSError) as exc:
        diagnostic = exc.code if isinstance(exc, InterfaceError) else "configuration-or-input-error"
        print(json.dumps({"error": diagnostic}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

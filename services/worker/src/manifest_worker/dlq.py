"""Inspect, diff and selectively redrive dead-lettered pipeline messages.

    python -m manifest_worker.dlq list [--stage page_extract]
    python -m manifest_worker.dlq show ID
    python -m manifest_worker.dlq diff ID_A ID_B
    python -m manifest_worker.dlq redrive (--id ID ... | --exception NAME | --failure KIND | --all)
                                          [--stage STAGE] [--dry-run]

IDs may be abbreviated to a unique prefix. ``list``, ``show`` and ``diff`` never remove
anything: messages are read unacknowledged and returned to their DLQ. ``redrive`` republishes a
message to its stage queue as a fresh standalone task (new id, retries reset, no chord), then
acks it. A redriven page task re-runs reconcile for its document when it succeeds.
"""

import argparse
import difflib
import json
import sys
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from kombu import Connection, Producer
from kombu.message import Message

from manifest_worker.app import settings
from manifest_worker.topology import EXCHANGE, STAGES, dead_letter_queue, work_queue

# Headers that differ between any two messages and say nothing about the payload.
VOLATILE = frozenset({
    "id", "root_id", "parent_id", "x-death", "x-delivery-count", "x-first-death-exchange",
    "x-first-death-queue", "x-first-death-reason", "x-last-death-exchange",
    "x-last-death-queue", "x-last-death-reason", "x-manifest-original-id",
    "x-manifest-parked-at",
})  # fmt: skip


@dataclass
class DeadLetter:
    stage: str
    message: Message

    @property
    def headers(self) -> dict[str, Any]:
        return dict(self.message.headers or {})

    @property
    def id(self) -> str:
        return str(self.headers.get("id", ""))

    @property
    def task(self) -> str:
        return str(self.headers.get("task", ""))

    @property
    def body(self) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
        args, kwargs, embed = self.message.decode()
        return list(args), dict(kwargs), dict(embed)

    @property
    def failure(self) -> str | None:
        return self.headers.get("x-manifest-failure")

    @property
    def exception(self) -> str | None:
        return self.headers.get("x-manifest-exception")

    @property
    def reason(self) -> str:
        if self.failure:
            return f"parked: {self.failure}"
        deaths = self.headers.get("x-death") or []
        if deaths:
            return f"broker: {deaths[0].get('reason')} x{deaths[0].get('count')}"
        return "unknown"

    def payload(self) -> dict[str, Any]:
        """What the message asks for, without per-delivery noise: what ``diff`` compares."""
        args, kwargs, _ = self.body
        headers = {k: v for k, v in sorted(self.headers.items()) if k not in VOLATILE}
        return {"task": self.task, "args": args, "kwargs": kwargs, "headers": headers}


def redrive_message(letter: DeadLetter) -> tuple[list[Any], dict[str, Any]]:
    """Body and headers for a fresh, standalone copy of ``letter``'s task."""
    args, kwargs, embed = letter.body
    task_id = str(uuid.uuid4())
    headers = {k: v for k, v in letter.headers.items() if not k.startswith("x-")}
    headers |= {
        "id": task_id,
        "root_id": task_id,
        "parent_id": None,
        "group": None,
        "group_index": None,
        "retries": 0,
        "eta": None,
        "expires": None,
        "x-manifest-redriven-from": letter.id,
    }
    embed |= {"chord": None, "callbacks": None, "errbacks": None, "chain": None}
    return [args, kwargs, embed], headers


def connect() -> Connection:
    return Connection(settings.broker_url, transport_options={"confirm_publish": True})


@contextmanager
def dead_letters(
    connection: Connection, stages: Iterable[str] = STAGES
) -> Iterator[list[DeadLetter]]:
    """Every message in the given DLQs, held unacked; whatever isn't acked goes back on exit."""
    channel = connection.channel()
    letters: list[DeadLetter] = []
    try:
        for stage in stages:
            queue = dead_letter_queue(stage)(channel)
            queue.declare()
            while (message := queue.get(no_ack=False)) is not None:
                letters.append(DeadLetter(stage, message))
        yield letters
    finally:
        for letter in letters:
            if not letter.message.acknowledged:
                letter.message.requeue()
        channel.close()


def redrive(connection: Connection, letters: Sequence[DeadLetter]) -> None:
    producer = Producer(connection.channel(), exchange=EXCHANGE)
    for letter in letters:
        body, headers = redrive_message(letter)
        producer.publish(
            body,
            routing_key=letter.stage,
            headers=headers,
            serializer="json",
            correlation_id=headers["id"],
            declare=[work_queue(letter.stage)],
        )
        letter.message.ack()  # only after the broker confirmed the republish


def find(letters: Sequence[DeadLetter], prefix: str) -> DeadLetter:
    matches = [letter for letter in letters if letter.id.startswith(prefix)]
    if len(matches) != 1:
        raise SystemExit(f"{len(matches)} dead letters match {prefix!r}; need exactly one")
    return matches[0]


def select(
    letters: Sequence[DeadLetter],
    *,
    ids: Sequence[str] = (),
    exception: str | None = None,
    failure: str | None = None,
) -> list[DeadLetter]:
    chosen = [find(letters, prefix) for prefix in ids] if ids else list(letters)
    if exception:
        chosen = [letter for letter in chosen if letter.exception == exception]
    if failure:
        chosen = [letter for letter in chosen if letter.failure == failure]
    return chosen


def diff(a: DeadLetter, b: DeadLetter) -> list[str]:
    def dump(letter: DeadLetter) -> list[str]:
        return json.dumps(letter.payload(), indent=2, sort_keys=True, default=str).splitlines()

    return list(difflib.unified_diff(dump(a), dump(b), a.id, b.id, lineterm=""))


def _describe(letter: DeadLetter) -> str:
    args, _, _ = letter.body
    retries = letter.headers.get("x-manifest-retries", "-")
    return (
        f"{letter.stage:<13} {letter.id[:8]}  {letter.task:<28} args={json.dumps(args):<44} "
        f"{letter.reason:<28} {letter.exception or '-'} retries={retries}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m manifest_worker.dlq", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "redrive"):
        command = commands.add_parser(name)
        command.add_argument("--stage", choices=STAGES, action="append")
    commands.add_parser("show").add_argument("id")
    diff_parser = commands.add_parser("diff")
    diff_parser.add_argument("a")
    diff_parser.add_argument("b")
    redrive_parser = commands.choices["redrive"]
    redrive_parser.add_argument("--id", action="append", default=[])
    redrive_parser.add_argument("--exception")
    redrive_parser.add_argument("--failure")
    redrive_parser.add_argument("--all", action="store_true")
    redrive_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with (
        connect() as connection,
        dead_letters(connection, getattr(args, "stage", None) or STAGES) as letters,
    ):
        if args.command == "list":
            for letter in letters:
                print(_describe(letter))
            print(f"{len(letters)} dead letter(s)", file=sys.stderr)
        elif args.command == "show":
            letter = find(letters, args.id)
            print(json.dumps({"reason": letter.reason, **letter.payload()}, indent=2, default=str))
        elif args.command == "diff":
            print("\n".join(diff(find(letters, args.a), find(letters, args.b))) or "identical")
        else:
            if not (args.id or args.exception or args.failure or args.all):
                raise SystemExit("redrive needs --id, --exception, --failure or --all")
            chosen = select(letters, ids=args.id, exception=args.exception, failure=args.failure)
            for letter in chosen:
                print(("would redrive " if args.dry_run else "redrive ") + _describe(letter))
            if not args.dry_run:
                redrive(connection, chosen)
            print(f"{len(chosen)} message(s) {'selected' if args.dry_run else 'redriven'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

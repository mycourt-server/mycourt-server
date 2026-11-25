"""Build public weekly and monthly rankings from an authoritative ledger snapshot.

Run: python3 build_rankings.py input.json rankings.json

Input contains revision (monotonic server snapshot revision), asOf (UTC to the
second, YYYY-MM-DDTHH:MM:SSZ), members (internal ID to approved public
display name), periods and entries. Each period (weekly/monthly) has id, label
and participants (the server-selected eligible member IDs for that period).
Each confirmed entry has id, member, status="confirmed", xp (signed delta),
sequence (global server confirmation order), weekly and monthly (allocated
period IDs). Pending and invalid entries have never posted. To revoke a posted
award, retain it and append its approved signed delta under a new ID. Supply one current
state per id. Identical delivery retries are accepted. Account IDs, event IDs
and ledger entries are never included in the output.

The iOS guest app reads the resulting public JSON over HTTPS from the Info.plist
key MYCourtRankingSnapshotURL (Debug: MYCOURT_RANKING_SNAPSHOT_URL).

Optional period.tieBreaks maps a member ID to {xp, sequence}. These are server
adjudications for the named period and final score. For a tied member with a
negative adjustment or no confirmed attainment, supply that adjudication.
A missing adjudication marks only the affected board as awaiting a decision.
Invalid input leaves the previously published file intact.
"""
import argparse
import fcntl
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PERIODS = ("weekly", "monthly")
MAX_INT = (1 << 63) - 1


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value):
    return type(value) is int and -MAX_INT <= value <= MAX_INT


def confirmed_entries(events):
    require(isinstance(events, list), "entries must be a list")
    seen, sequences = {}, set()
    for entry in events:
        key = entry["id"]
        require(nonempty(key), "invalid event ID")
        if key in seen:
            require(seen[key] == entry, "conflicting copies of an event")
            continue
        seen[key] = entry
        status = entry["status"]
        require(status in ("confirmed", "pending", "invalid"), "unknown event status")
        if status != "confirmed":
            require(entry.get("sequence") is None, "a previously confirmed award needs a signed adjustment")
            continue
        seq, xp = entry["sequence"], entry["xp"]
        require(integer(seq) and seq > 0 and seq not in sequences, "invalid or repeated confirmation order")
        require(integer(xp) and nonempty(entry["member"]), "invalid XP or member ID")
        require(all(nonempty(entry[p]) for p in PERIODS), "missing assigned period")
        sequences.add(seq)
        yield entry


def period_board(kind, period, names, events):
    require(nonempty(period["id"]) and nonempty(period["label"]), "missing period identity or display label")
    participants = period["participants"]
    require(isinstance(participants, list) and all(m in names for m in participants), "invalid participants")
    totals = dict.fromkeys(participants, 0)
    require(len(totals) == len(participants), "duplicate participant")
    latest, corrected = {}, set()
    for event in events:
        member = event["member"]
        if event[kind] != period["id"] or member not in totals:
            continue
        delta = event["xp"]
        totals[member] += delta
        if delta < 0:
            corrected.add(member)
        elif delta > 0:
            latest[member] = max(latest.get(member, 0), event["sequence"])

    decisions = period.get("tieBreaks", {})
    require(isinstance(decisions, dict), "invalid adjudications")
    for member, decision in decisions.items():
        require(member in totals and integer(decision["xp"]) and decision["xp"] == totals[member]
                and integer(decision["sequence"]) and decision["sequence"] > 0, "stale or invalid adjudication")

    groups = defaultdict(list)
    for member, xp in totals.items():
        require(0 <= xp <= MAX_INT, "invalid final XP")
        groups[xp].append(member)

    missing_decision = False
    rows = []
    for xp in sorted(groups, reverse=True):
        group = groups[xp]
        if len(group) > 1:
            order = {}
            for member in group:
                if member in corrected or member not in latest:
                    if member in decisions:
                        order[member] = decisions[member]["sequence"]
                else:
                    # With positive grants, the last grant is the first attainment of the final score.
                    order[member] = latest[member]
                    require(member not in decisions or decisions[member]["sequence"] == order[member],
                            "adjudication contradicts the confirmed attainment")
            require(len(set(order.values())) == len(order), "ambiguous server tie order")
            if len(order) != len(group):
                missing_decision = True
                continue
            group = sorted(group, key=order.__getitem__)
        for member in group:
            rows.append({"rank": len(rows) + 1, "displayName": names[member], "xp": xp})
    return {"periodID": period["id"], "label": period["label"],
            "status": "awaitingTieDecision" if missing_decision else "published",
            "rows": [] if missing_decision else rows}


def build(snapshot):
    revision = snapshot["revision"]
    require(integer(revision) and revision > 0, "invalid snapshot revision")
    timestamp = snapshot["asOf"]
    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    require(parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == timestamp, "invalid UTC timestamp")
    names = snapshot["members"]
    require(isinstance(names, dict) and all(nonempty(k) and nonempty(v) for k, v in names.items()), "invalid public names")
    events = list(confirmed_entries(snapshot["entries"]))
    result = {"version": 1, "revision": revision, "asOf": timestamp}
    for kind in PERIODS:
        result[kind] = period_board(kind, snapshot["periods"][kind], names, events)
    return result


def publish(source, destination):
    if source.resolve() == destination.resolve():
        raise ValueError("input and output must be separate files")
    publish_snapshot(build(json.loads(source.read_text(encoding="utf-8"))), destination)


def publish_snapshot(payload, destination):
    with destination.with_name("." + destination.name + ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if destination.exists():
            previous = json.loads(destination.read_text(encoding="utf-8"))
            if previous == payload:
                return
            require(previous["revision"] < payload["revision"] and previous["asOf"] <= payload["asOf"],
                    "refusing an older or conflicting snapshot")
        write_snapshot(payload, destination)


def write_snapshot(payload, destination):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, mode="w", encoding="utf-8", delete=False) as temp:
            temp_path = Path(temp.name)
            json.dump(payload, temp, ensure_ascii=False, separators=(",", ":"))
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        publish(args.source, args.destination)
    except (KeyError, ValueError, TypeError, OSError) as error:
        parser.exit(1, f"Rankings were not published: {error}\n")

"""Keep server-approved member XP in SQLite and publish the two public ranking boards."""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from build_rankings import PERIODS, build, integer, nonempty, publish_snapshot, require

REVIEW_XP = {"racket": 200, "shoe": 180}
SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL, as_of TEXT NOT NULL, settings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS awards (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    member TEXT NOT NULL, kind TEXT NOT NULL, reward_key TEXT NOT NULL,
    event_type TEXT NOT NULL, source TEXT NOT NULL, verification TEXT NOT NULL, xp INTEGER NOT NULL,
    weekly TEXT NOT NULL, monthly TEXT NOT NULL, awarded_at TEXT NOT NULL,
    reverses INTEGER REFERENCES awards(sequence),
    UNIQUE (member, kind, reward_key)
);
CREATE INDEX IF NOT EXISTS award_week ON awards(weekly);
CREATE INDEX IF NOT EXISTS award_month ON awards(monthly);
CREATE INDEX IF NOT EXISTS award_reversals ON awards(reverses);
CREATE TABLE IF NOT EXISTS reviews (
    source TEXT PRIMARY KEY NOT NULL, member TEXT NOT NULL, category TEXT NOT NULL,
    model TEXT NOT NULL, weekly TEXT NOT NULL, monthly TEXT NOT NULL,
    UNIQUE(member, model)
);
CREATE INDEX IF NOT EXISTS review_count ON reviews(member, category);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fields(data, required, optional=()):
    require(isinstance(data, dict) and set(required) <= data.keys() <= set(required) | set(optional),
            "unexpected or missing input fields")


def text_fields(data, keys):
    require(all(nonempty(data[key]) for key in keys), "nonempty identifiers required")


def read_state(db):
    row = db.execute("SELECT revision, as_of, settings FROM ledger_state WHERE singleton=1").fetchone()
    require(row is not None, "configure the member ledger first")
    return row


def current_snapshot(db):
    revision, as_of, settings = read_state(db)
    config = json.loads(settings)
    periods = config["periods"]
    rows = db.execute("""SELECT sequence, member, xp, weekly, monthly FROM awards
                         WHERE weekly=? OR monthly=? ORDER BY sequence""",
                      (periods["weekly"]["id"], periods["monthly"]["id"]))
    events = [dict(id=str(seq), member=member, sequence=seq, xp=xp, weekly=w, monthly=m, status="confirmed")
              for seq, member, xp, w, m in rows]
    return build(dict(revision=revision, asOf=as_of, entries=events, **config))


def configure(db, config, at):
    fields(config, ("members", "periods"))
    fields(config["periods"], PERIODS)
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True)
    prior = db.execute("SELECT settings FROM ledger_state WHERE singleton=1").fetchone()
    if prior and prior[0] == encoded:
        return False
    db.execute("""INSERT INTO ledger_state VALUES (1, 1, ?, ?)
                  ON CONFLICT(singleton) DO UPDATE SET revision=revision+1,
                  as_of=MAX(as_of, excluded.as_of), settings=excluded.settings""", (at, encoded))
    current_snapshot(db)  # Invalid period configuration rolls back before it can replace the current one.
    return True


def insert_award(db, member, kind, key, data, xp, at, reversal=None):
    values = (data.get("eventType", kind), data["source"], data["verification"], xp, data["weekly"], data["monthly"], reversal)
    existing = db.execute("""SELECT event_type, source, verification, xp, weekly, monthly, reverses FROM awards
                             WHERE member=? AND kind=? AND reward_key=?""", (member, kind, key)).fetchone()
    if existing is not None:
        require(existing == values, "different decisions use the same reward key")
        return False
    db.execute("""INSERT INTO awards(member,kind,reward_key,event_type,source,verification,xp,weekly,monthly,reverses,awarded_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (member, kind, key, *values, at))
    return True


def record_decision(db, data, at):
    common = ("member", "source", "verification", "weekly", "monthly", "rewardKey", "eventType")
    fields(data, (*common, "xp"), ("reverses",))
    text_fields(data, common)
    require(integer(data["xp"]) and data["xp"] != 0, "a nonzero signed XP decision is required")
    reversal = data.get("reverses")
    require((data["xp"] < 0 and integer(reversal) and reversal > 0)
            or (data["xp"] > 0 and reversal is None), "a deduction must identify its original award")
    # The external decision uses a stable per-member reward key across all external event types.
    if reversal is not None:
        original = db.execute("SELECT member, weekly, monthly, xp FROM awards WHERE sequence=?",
                              (reversal,)).fetchone()
        require(original and original[:3] == (data["member"], data["weekly"], data["monthly"])
                and original[3] > 0, "deduction and original award must match")
    changed = insert_award(db, data["member"], "decision", data["rewardKey"], data, data["xp"], at, reversal)
    if changed and reversal is not None:
        delta = db.execute("SELECT SUM(xp) FROM awards WHERE reverses=?", (reversal,)).fetchone()[0]
        require(original[3] + delta >= 0, "total deductions exceed the original award")
    return changed


def approve_review(db, data, at):
    keys = ("member", "source", "verification", "weekly", "monthly", "category", "model")
    fields(data, keys)
    text_fields(data, keys)
    member, category, model = data["member"], data["category"], data["model"]
    require(category in REVIEW_XP, "unsupported review category")
    identity = (member, category, model, data["weekly"], data["monthly"])
    prior = db.execute("SELECT member, category, model, weekly, monthly FROM reviews WHERE source=?",
                       (data["source"],)).fetchone()
    if prior is not None:
        require(prior == identity, "a review was retried with a different identity or period")
        return False
    prior = db.execute("SELECT category FROM reviews WHERE member=? AND model=?", (member, model)).fetchone()
    if prior is not None:
        require(prior[0] == category, "model category conflicts with its existing review")
        return False
    db.execute("INSERT INTO reviews(source,member,category,model,weekly,monthly) VALUES (?,?,?,?,?,?)",
               (data["source"], *identity))
    count = db.execute("SELECT COUNT(*) FROM reviews WHERE member=? AND category=?", (member, category)).fetchone()[0]
    points = REVIEW_XP[category]
    insert_award(db, member, "first_review", model, data, points, at)
    if count % 5 == 0:
        insert_award(db, member, category + "_milestone", str(count), data, points, at)
    return True


def update(path, action, data):
    if action != "configure":
        require(path.is_file(), "configure the member ledger first")
    with closing(sqlite3.connect(path, timeout=20)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        if action == "configure":
            db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        at = now()
        if action == "configure":
            return configure(db, data, at)
        revision, previous_time, settings = read_state(db)
        require(isinstance(data, dict) and nonempty(data.get("member")) and data["member"] in json.loads(settings)["members"],
                "a registered member is required")
        at = max(at, previous_time)
        if action == "approve-review":
            changed = approve_review(db, data, at)
        elif action == "post":
            changed = record_decision(db, data, at)
        else:
            raise ValueError("unknown ledger action")
        if changed:
            db.execute("UPDATE ledger_state SET revision=?, as_of=? WHERE singleton=1", (revision+1, at))
        return changed


def check_output(path, output):
    reserved = [path, *(Path(str(path) + s) for s in ("-journal", "-wal", "-shm"))]
    require(all(output.resolve() != p.resolve() and not (p.exists() and output.exists() and output.samefile(p))
                for p in reserved), "choose a separate public output path")


def publish_ledger(path, output):
    require(path.is_file(), "member ledger is missing")
    check_output(path, output)
    with closing(sqlite3.connect(path, timeout=20)) as db, db:
        db.execute("BEGIN")
        public = current_snapshot(db)
    publish_snapshot(public, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["configure", "approve-review", "post", "publish"])
    parser.add_argument("database", type=Path)
    parser.add_argument("file", type=Path, help="private JSON input, or the public JSON output for publish")
    parser.add_argument("--publish-to", type=Path, help="refresh this public JSON after a write or a retry")
    args = parser.parse_args()
    try:
        if args.publish_to is not None:
            require(args.action != "publish", "publish already takes an output file")
            check_output(args.database, args.publish_to)
            require(args.file.resolve() != args.publish_to.resolve()
                    and not (args.publish_to.exists() and args.file.samefile(args.publish_to)),
                    "public output must be separate from the input")
        if args.action == "publish":
            publish_ledger(args.database, args.file)
        else:
            changed = update(args.database, args.action, json.loads(args.file.read_text(encoding="utf-8")))
            if args.publish_to is not None:
                try:
                    publish_ledger(args.database, args.publish_to)
                except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
                    parser.exit(1, f"Ledger committed. Publication failed; retry publication: {error}\n")
            print("applied" if changed else "already applied")
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.exit(1, f"Ledger operation failed: {error}\n")

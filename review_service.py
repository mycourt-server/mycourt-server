"""Member-only review API and local moderation commands for a configured member ledger."""
import argparse
import base64
import binascii
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from member_ledger import SCHEMA as LEDGER_SCHEMA
from member_ledger import approve_review, check_output, now, publish_ledger, read_state

LIMIT = 16 * 1024 * 1024
AXES = {"racket": set("smash drive defense control feel handling".split()),
        "shoe": set("grip support cushion lightness fit durability".split())}
SCHEMA = """
CREATE TABLE IF NOT EXISTS login_members (
 member TEXT PRIMARY KEY NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL, UNIQUE(issuer,subject)
);
CREATE TABLE IF NOT EXISTS review_submissions (
 id TEXT PRIMARY KEY NOT NULL, member TEXT NOT NULL REFERENCES login_members(member),
 fingerprint TEXT NOT NULL, content TEXT NOT NULL, submitted TEXT NOT NULL,
 status TEXT NOT NULL, decision TEXT, xp INTEGER NOT NULL DEFAULT 0, UNIQUE(member,fingerprint)
);
CREATE INDEX IF NOT EXISTS moderation_queue ON review_submissions(status, submitted);
CREATE TABLE IF NOT EXISTS review_decisions (
 id INTEGER PRIMARY KEY, submission TEXT NOT NULL REFERENCES review_submissions(id),
 actor TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL
);
"""


class Problem(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def check(condition, status, message):
    if not condition:
        raise Problem(status, message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class IdentityProvider:
    def __init__(self, settings):
        self.endpoint = settings["MYCOURT_INTROSPECTION_URL"]
        self.issuer = settings["MYCOURT_ISSUER"]
        resource = settings["MYCOURT_API_RESOURCE"]
        self.client = settings["MYCOURT_MOBILE_CLIENT_ID"]
        for key in (self.endpoint, self.issuer, resource):
            u = urlsplit(key)
            if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment:
                raise ValueError("HTTPS identity and resource URLs are required")
        if not all(settings[k].strip() for k in ("MYCOURT_MOBILE_CLIENT_ID", "MYCOURT_INTROSPECTION_CLIENT",
                                                  "MYCOURT_INTROSPECTION_SECRET")):
            raise ValueError("The identity client settings must be populated")
        project = settings.get("MYCOURT_ZITADEL_PROJECT_ID", "")
        if project and (not project.isascii() or not project.isdigit()):
            raise ValueError("Invalid ZITADEL project ID")
        self.audience = project or resource
        # In ZITADEL this dedicated project is the member API audience. The native client
        # explicitly requests the reserved audience scope, whose grant is conveyed by aud.
        self.required_scope = "openid" if project else "reviews"
        from urllib.parse import quote_plus
        credentials = ":".join(quote_plus(settings[key]) for key in
                               ("MYCOURT_INTROSPECTION_CLIENT", "MYCOURT_INTROSPECTION_SECRET"))
        self.basic = base64.b64encode(credentials.encode()).decode()
        self.opener = build_opener(NoRedirect())

    def verify(self, token):
        request = Request(self.endpoint, data=urlencode({"token": token, "token_type_hint": "access_token"}).encode(),
                          headers={"Authorization": "Basic " + self.basic, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=8) as response:
                claims = json.loads(response.read(65537))
        except (HTTPError, URLError, OSError, ValueError):
            raise Problem(503, "인증 서버에 연결할 수 없어요.")
        return self.identity(claims)

    def identity(self, c):
        check(isinstance(c, dict), 503, "인증 응답을 확인할 수 없어요.")
        audiences = c.get("aud")
        if isinstance(audiences, str):
            audiences = [audiences]
        check(c.get("active") is True and c.get("iss") == self.issuer and c.get("client_id") == self.client
              and isinstance(audiences, list) and self.audience in audiences
              and isinstance(c.get("scope"), str) and self.required_scope in c["scope"].split()
              and isinstance(c.get("token_type"), str) and c["token_type"].lower() == "bearer"
              and type(c.get("exp")) in (float, int) and c["exp"] > time.time()
              and isinstance(c.get("sub"), str) and bool(c["sub"].strip()), 401, "다시 로그인해 주세요.")
        return self.issuer, c["sub"]


def connect(path):
    check(path.is_file(), 503, "회원 원장 연결이 필요해요.")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def create_database(path):
    # Build privately first. An existing ledger wins even when two initializers run together.
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        temp = Path(folder) / "member.sqlite"
        temp.touch(mode=0o600)
        with closing(sqlite3.connect(temp)) as db:
            db.executescript(LEDGER_SCHEMA + SCHEMA)
        os.link(temp, path)


def initialize(path):
    with closing(connect(path)) as db:
        db.execute("SELECT revision, as_of, settings FROM ledger_state LIMIT 0")
        db.executescript(SCHEMA)


def valid_review(data):
    keys = {"category", "model", "scores", "period", "strengths", "limitations", "body", "photos",
            "usedPersonally", "consent"}
    check(isinstance(data, dict) and data.keys() == keys, 400, "리뷰 항목을 확인해 주세요.")
    check(data["usedPersonally"] is True and data["consent"] is True, 400, "실사용 확인과 제출 동의가 필요해요.")
    category = data["category"]
    check(isinstance(category, str) and category in AXES, 400, "제품 종류를 확인해 주세요.")
    for name in ("model", "period", "strengths", "limitations", "body"):
        check(isinstance(data[name], str) and data[name].strip(), 400, "리뷰 내용을 확인해 주세요.")
    scores = data["scores"]
    check(isinstance(scores, dict) and AXES[category] <= scores.keys() <= AXES[category] | {"overall"}
          and all(type(n) is int and 10 <= n <= 100 for n in scores.values()), 400, "평가 점수를 확인해 주세요.")
    check(isinstance(data["photos"], list), 400, "사진 목록을 확인해 주세요.")
    for photo in data["photos"]:
        try:
            content = base64.b64decode(photo, validate=True) if isinstance(photo, str) else b""
        except (ValueError, binascii.Error):
            content = b""
        check(content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"), 400, "사진 형식을 확인해 주세요.")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt(row):
    reason = json.loads(row["decision"])["reason"] if row["decision"] else ""
    return {"id": row["id"], "model": row["model"], "status": row["status"], "xp": row["xp"], "reason": reason}


# Extract only the model label when listing receipts. Photo evidence stays private.
RECEIPT = "SELECT id, json_extract(content,'$.model') AS model, status, xp, decision FROM review_submissions"


def member_request(path, identity, method, route, data):
    content = valid_review(data) if (method, route) == ("POST", "/reviews") else None
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        account = db.execute("SELECT member FROM login_members WHERE issuer=? AND subject=?", identity).fetchone()
        if account:
            member = account[0]
        else:
            member = str(uuid.uuid4())
            db.execute("INSERT INTO login_members VALUES (?,?,?)", (member, *identity))
        if (method, route) == ("GET", "/me"):
            return {"member": member}
        if (method, route) == ("GET", "/reviews"):
            rows = db.execute(RECEIPT + " WHERE member=? ORDER BY submitted DESC,rowid DESC", (member,))
            return {"reviews": [receipt(r) for r in rows]}
        if content is not None:
            fingerprint = hashlib.sha256(content.encode()).hexdigest()
            db.execute("""INSERT INTO review_submissions(id,member,fingerprint,content,submitted,status)
                          VALUES (?,?,?,?,?,'pending') ON CONFLICT(member,fingerprint) DO NOTHING""",
                       (str(uuid.uuid4()), member, fingerprint, content, now()))
            row = db.execute(RECEIPT + " WHERE member=? AND fingerprint=?", (member, fingerprint)).fetchone()
            return receipt(row)
        raise Problem(404, "요청 경로를 확인해 주세요.")


def application(path, verify):
    def handle(env, start):
        if (env.get("REQUEST_METHOD"), env.get("PATH_INFO")) == ("GET", "/health"):
            try:
                with closing(connect(path)) as db:
                    db.execute("SELECT id FROM review_submissions LIMIT 1").fetchone()
                status, body = "200 OK", b'{"ready":true}'
            except (Problem, sqlite3.Error):
                status, body = "503 Service Unavailable", b'{"ready":false}'
            start(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                           ("Content-Length", str(len(body)))])
            return [body]
        try:
            check(env.get("wsgi.url_scheme") == "https", 400, "HTTPS 연결이 필요해요.")
            method, route = env["REQUEST_METHOD"], env["PATH_INFO"]
            check((method, route) in {("GET", "/me"), ("GET", "/reviews"), ("POST", "/reviews")}, 404, "경로를 확인해 주세요.")
            scheme, _, token = env.get("HTTP_AUTHORIZATION", "").partition(" ")
            check(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(c.isspace() for c in token),
                  401, "로그인이 필요해요.")
            identity = verify(token)
            data = None
            if method == "POST":
                check(env.get("CONTENT_TYPE", "").split(";")[0] == "application/json", 415, "JSON 입력이 필요해요.")
                try:
                    size = int(env.get("CONTENT_LENGTH", ""))
                except (ValueError, TypeError):
                    raise Problem(411, "전송 크기를 확인해 주세요.")
                check(0 < size <= LIMIT, 413, "제출 용량을 확인해 주세요.")
                try:
                    raw = env["wsgi.input"].read(size)
                    check(len(raw) == size, 400, "전송이 완료되지 않았어요.")
                    data = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise Problem(400, "리뷰 데이터를 읽을 수 없어요.")
            result, status = member_request(path, identity, method, route, data), 200
        except Problem as error:
            status, result = error.status, {"error": str(error)}
        except sqlite3.Error:
            status, result = 503, {"error": "잠시 뒤 다시 시도해 주세요."}
        body = json.dumps(result, ensure_ascii=False).encode()
        start(f"{status} {HTTPStatus(status).phrase}", [("Content-Type", "application/json; charset=utf-8"),
                  ("Cache-Control", "no-store"), ("Content-Length", str(len(body)))])
        return [body]
    return handle


def make_application():
    path = Path(os.environ["MYCOURT_MEMBER_DB"])
    provider = IdentityProvider(os.environ)
    initialize(path)
    return application(path, provider.verify)


def moderate(path, receipt_id, decision):
    status = decision.get("status")
    required = {"actor", "reason", "status"} | ({"model", "weekly", "monthly"} if status == "approved" else set())
    check(status in ("approved", "needsChanges", "held") and decision.keys() == required and
          all(isinstance(x, str) and x.strip() for x in decision.values()), 400, "검토 정보가 필요해요.")
    details = json.dumps(decision, sort_keys=True, ensure_ascii=False)
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT member,content,status,decision FROM review_submissions WHERE id=?", (receipt_id,)).fetchone()
        check(row is not None, 404, "접수 내역이 없어요.")
        if row["decision"] == details:
            return
        check(row["status"] in ("pending", "held"), 409, "이미 검토를 마친 제출이에요.")
        at, xp = now(), 0
        if status == "approved":
            rev, previous, config = read_state(db)
            check(row["member"] in json.loads(config)["members"], 409, "회원의 공개명과 집계 설정을 먼저 등록해 주세요.")
            # Ledger routines use tuple equality with SQLite's default row factory.
            member, category = row["member"], json.loads(row["content"])["category"]
            db.row_factory = None
            at = max(at, previous)
            source = "submission:" + receipt_id
            changed = approve_review(db, dict(member=member, category=category, source=source,
                                      verification=receipt_id, model=decision["model"],
                                      weekly=decision["weekly"], monthly=decision["monthly"]), at)
            if changed:
                xp = db.execute("SELECT COALESCE(SUM(xp),0) FROM awards WHERE member=? AND source=?", (member, source)).fetchone()[0]
                db.execute("UPDATE ledger_state SET revision=?, as_of=? WHERE singleton=1", (rev+1, at))
        db.execute("UPDATE review_submissions SET status=?, decision=?, xp=? WHERE id=?", (status, details, xp, receipt_id))
        db.execute("INSERT INTO review_decisions(submission,actor,at,details) VALUES (?,?,?,?)",
                   (receipt_id, decision["actor"], at, details))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create a new private member database without registering ranking periods")
    sub.add_parser("queue")
    show = sub.add_parser("inspect")
    show.add_argument("receipt")
    decide = sub.add_parser("decide")
    decide.add_argument("receipt")
    decide.add_argument("decision", type=Path)
    decide.add_argument("--publish-to", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            create_database(args.database)
            parser.exit(0)
        initialize(args.database)
        if args.command == "decide":
            if args.publish_to:
                check_output(args.database, args.publish_to)
            moderate(args.database, args.receipt, json.loads(args.decision.read_text()))
            if args.publish_to:
                publish_ledger(args.database, args.publish_to)
        else:
            with closing(connect(args.database)) as db:
                if args.command == "queue":
                    for row in db.execute("SELECT id,member,status,submitted FROM review_submissions ORDER BY submitted,rowid"):
                        print(json.dumps(dict(row), ensure_ascii=False))
                else:
                    row = db.execute("SELECT content FROM review_submissions WHERE id=?", (args.receipt,)).fetchone()
                    check(row, 404, "접수 내역이 없어요.")
                    print(row[0])
    except (OSError, ValueError, sqlite3.Error, Problem) as error:
        parser.exit(1, f"{error}\n")

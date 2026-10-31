#!/usr/bin/env python3
# Hermetic Apache Iceberg REST Catalog fixture, for test/iceberg_rest.sh
# (#388 phase 7). It answers the read-only subset the pgColumnar REST client
# speaks -- GET /v1/config, loadTable, and listing -- and it VERIFIES the
# Authorization: Bearer header, so a green read proves the C client and an
# independent server agree on the request the same way the SigV4 fixture proves
# the signer. No third-party catalog, no container: the suite runs in CI.
#
# It never logs the token value (only whether an Authorization header was
# present), so the suite can assert both "the token was sent" (the server would
# 401 otherwise) and "the token never appears in any log".
#
# Endpoints:
#   GET /v1/config[?warehouse=...]                 -> {"defaults":{},"overrides":{...}}
#   GET /v1/namespaces                             -> {"namespaces":[["db"],...]}
#   GET /v1/namespaces/{ns}/tables                 -> {"identifiers":[...]}
#   GET /v1/namespaces/{ns}/tables/{table}         -> loadTable {metadata-location,...}
# with an optional {prefix} segment after /v1/ when --prefix is given, exactly
# as a real catalog splices the config-returned prefix.
#
# Special table names drive the failure arms:
#   toobig   loadTable answers 200 with a huge Content-Length and no body, so a
#            client honoring a response cap refuses before reading it.
#   closedelim  loadTable answers 200 with Connection: close and neither
#            Content-Length nor chunked framing, so the body ends when the server
#            closes the connection (RFC 9112 section 6.3, rule 8). The Apache
#            Iceberg REST catalog frames any reply over about 32 KB this way when
#            the client sends Connection: close (#1787). The body is padded past
#            the client's 64 KB receive buffer so it arrives in several reads.
#   closebig loadTable answers the same close-delimited way with a body larger
#            than the client's 64 MB REST response cap, so a client that reads to
#            end of stream must still refuse once the body passes the cap.
#
# Written fresh for pgColumnar. Reuses no upstream test harness.

import argparse
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):     # silence stderr chatter
        pass

    # ---- helpers ----------------------------------------------------------
    def _logline(self, verb, path):
        # AUTH=yes/no ONLY -- never the token value.
        has_auth = "yes" if self.headers.get("Authorization") else "no"
        with open(ARGS.log, "a") as fh:
            fh.write("%s %s AUTH=%s\n" % (verb, path, has_auth))

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        # When --token is set, every resource requires exactly "Bearer <token>".
        if not ARGS.token:
            return True
        got = self.headers.get("Authorization", "")
        return got == ("Bearer " + ARGS.token)

    def _unauthorized(self):
        self._send_json(401, {"error": {"message": "not authorized",
                                        "type": "NotAuthorizedException",
                                        "code": 401}})

    def _not_found(self, msg, kind="NoSuchTableException"):
        # The spec's ErrorModel. The type tells a missing namespace from a
        # missing table, and the message reaches the client's errdetail (#1820).
        self._send_json(404, {"error": {"message": msg,
                                        "type": kind,
                                        "code": 404}})

    def _strip_prefix(self, rest):
        # rest is the path after "/v1/". Consume the optional {prefix} segment.
        if ARGS.prefix:
            want = ARGS.prefix.strip("/") + "/"
            if not rest.startswith(want):
                return None
            rest = rest[len(want):]
        return rest

    # ---- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # log the full request target (path + any query) so a test can assert a
        # query parameter such as ?warehouse=; routing still uses the bare path.
        self._logline("GET", self.path)

        if not path.startswith("/v1/"):
            self._not_found("no such path")
            return
        if not self._auth_ok():
            self._unauthorized()
            return

        # /v1/config is not under the prefix (the client calls it to LEARN the
        # prefix), so handle it before stripping.
        if path == "/v1/config":
            if ARGS.bad_config:
                # a hostile/broken catalog: config is not a JSON object. The
                # client must refuse this, not walk it as key/value pairs.
                self._send_json(200, [])
                return
            overrides = {}
            if ARGS.prefix:
                overrides["prefix"] = ARGS.prefix.strip("/")
            self._send_json(200, {"defaults": {}, "overrides": overrides})
            return

        rest = self._strip_prefix(path[len("/v1/"):])
        if rest is None:
            self._not_found("bad prefix")
            return

        if rest == "namespaces":
            self._send_json(200, {"namespaces": [[ARGS.namespace]]})
            return

        parts = rest.split("/")
        # namespaces/{ns}/tables  and  namespaces/{ns}/tables/{table}
        if len(parts) >= 3 and parts[0] == "namespaces" and parts[2] == "tables":
            ns = urllib.parse.unquote(parts[1])
            if len(parts) == 3:
                # Only the namespace this catalog serves has tables. Echoing any
                # namespace back let a client that ignored its namespace argument
                # list "events" anyway.
                if ns.replace("\x1f", ".") != ARGS.namespace:
                    self._not_found("no such namespace %s" % ns,
                                    "NoSuchNamespaceException")
                    return
                self._send_json(200, {"identifiers": [
                    {"namespace": [ns], "name": ARGS.table}]})
                return
            table = urllib.parse.unquote(parts[3])
            self._load_table(ns, table)
            return

        self._not_found("no such resource")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self._logline("POST", path)     # never logs the body, so no secret leaks
        if path != "/v1/oauth/tokens" or not ARGS.oauth_client_id:
            self._not_found("no such path")
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode() if n > 0 else ""
        form = urllib.parse.parse_qs(body)

        def one(k):
            v = form.get(k, [""])
            return v[0] if v else ""
        # client-credentials grant: the id and secret must match exactly. A wrong
        # secret is 401, which the client maps to 28000 with no secret in the log.
        if (one("grant_type") == "client_credentials"
                and one("client_id") == ARGS.oauth_client_id
                and one("client_secret") == ARGS.oauth_client_secret):
            self._send_json(200, {"access_token": ARGS.token,
                                  "token_type": "bearer", "expires_in": 3600})
        else:
            self._unauthorized()

    def _send_close_delimited(self, chunks):
        # no Content-Length, no Transfer-Encoding: the close ends the body.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in chunks:
                self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                        # the client stopped reading at its cap

    def _load_table(self, ns, table):
        if table == "closedelim":
            pad = [{"snapshot-id": i, "summary": {"operation": "append"}}
                   for i in range(4000)]
            body = json.dumps({
                "metadata-location": ARGS.metadata_location,
                "metadata": {"format-version": 2, "location": ARGS.table_location,
                             "snapshots": pad},
                "config": {},
            }).encode()
            self._send_close_delimited([body])
            return
        if table == "closebig":
            # 65 MB of a JSON string, sent in 1 MB writes, never completed.
            head = b'{"metadata-location": "' + ARGS.metadata_location.encode() + \
                b'", "metadata": {"pad": "'
            self._send_close_delimited([head] + [b"x" * (1 << 20)] * 65)
            return
        if table == "toobig":
            # advertise a body far larger than any sane response cap, send none:
            # a client honoring the cap must refuse on the advertised length.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000000")
            self.end_headers()
            return
        # the namespace wire form joins multi-level parts with 0x1F; accept both
        # a single "db" and the unit-separated form.
        ns_norm = ns.replace("\x1f", ".")
        if ns_norm != ARGS.namespace:
            self._not_found("no such namespace %s" % ns_norm,
                            "NoSuchNamespaceException")
            return
        if table != ARGS.table:
            self._not_found("no such table %s.%s" % (ns, table))
            return
        config = {}
        # vended storage credentials: the client must sign its S3 reads with
        # these, not with any ambient environment credential.
        if ARGS.vend_key:
            config["s3.access-key-id"] = ARGS.vend_key
            config["s3.secret-access-key"] = ARGS.vend_secret
            config["s3.region"] = ARGS.vend_region
            if ARGS.vend_endpoint:
                config["s3.endpoint"] = ARGS.vend_endpoint
            if ARGS.vend_token:
                config["s3.session-token"] = ARGS.vend_token
        resp = {
            "metadata-location": ARGS.metadata_location,
            "metadata": {"format-version": 2, "location": ARGS.table_location},
            "config": config,
        }
        # storage-credentials array form (newer spec), longest-prefix selected by
        # the client. When --storage-cred-prefix is given, offer TWO entries: a
        # deliberately wrong catch-all and the correct, more specific one, so a
        # green read proves longest-prefix selection, and the flat config is wrong
        # as well, so it proves the array was read at all.
        if ARGS.storage_cred_prefix and ARGS.vend_key:
            # The flat config is wrong too: a reader that ignored the array and
            # used the flat config would otherwise read with the right creds.
            resp["config"] = dict(config, **{"s3.access-key-id": "WRONGFLATKEY",
                                             "s3.secret-access-key": "wrong-flat-secret"})
            resp["storage-credentials"] = [
                {"prefix": "s3://", "config": {
                    "s3.access-key-id": "WRONGKEY",
                    "s3.secret-access-key": "wrong-secret",
                    "s3.region": ARGS.vend_region,
                    "s3.endpoint": ARGS.vend_endpoint}},
                {"prefix": ARGS.storage_cred_prefix, "config": config},
            ]
        # --storage-cred PREFIX=good|bad, repeatable (#1820): an explicit
        # storage-credentials array. "good" is the vended key, "bad" a key the
        # SigV4 fixture refuses with 403. The flat config follows --flat-config:
        # "none" (the default here) vends nothing flat, "good" the vended key.
        # A reader that matched one entry against the metadata-location only, or
        # fell back to ambient for a path no entry covers, reads where it must not.
        if ARGS.storage_cred:
            bad = dict(config, **{"s3.access-key-id": "BADSCOPEDKEY",
                                  "s3.secret-access-key": "bad-scoped-secret"})
            entries = []
            for spec in ARGS.storage_cred:
                prefix, _, kind = spec.rpartition("=")
                entries.append({"prefix": prefix,
                                "config": config if kind == "good" else bad})
            resp["storage-credentials"] = entries
            resp["config"] = config if ARGS.flat_config == "good" else {}
        self._send_json(200, resp)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--namespace", default="db")
    ap.add_argument("--table", default="events")
    ap.add_argument("--metadata-location", required=True)
    ap.add_argument("--table-location", default="")
    ap.add_argument("--bad-config", action="store_true")
    ap.add_argument("--vend-key", default="")
    ap.add_argument("--vend-secret", default="")
    ap.add_argument("--vend-token", default="")
    ap.add_argument("--vend-region", default="")
    ap.add_argument("--vend-endpoint", default="")
    ap.add_argument("--storage-cred-prefix", default="")
    ap.add_argument("--storage-cred", action="append", default=[])
    ap.add_argument("--flat-config", choices=["none", "good"], default="none")
    ap.add_argument("--oauth-client-id", default="")
    ap.add_argument("--oauth-client-secret", default="")
    ARGS = ap.parse_args()
    open(ARGS.log, "w").close()
    httpd = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    print("READY", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

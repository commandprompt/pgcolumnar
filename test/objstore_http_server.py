#!/usr/bin/env python3
# Range-capable logging HTTP fixture for test/objstore_http_read.sh (#393 M1)
# and test/objstore_s3_read.sh (#393 M2).
#
# Serves files from --dir and appends one line per request to --log:
#     METHOD <path> <value of the Range header, or "-">
# The log is the suite's seam B: request counts are asserted against it, so this
# server must log every request exactly once, before serving it.
#
# Special path prefixes, used by the failure and cancel arms:
#   /norange/<file>  serve <file> but IGNORE any Range header (status 200, whole
#                    body): the client must treat this as an error, never as data.
#   /stall/<file>    answer the first ranged GET's headers, send half the body,
#                    then sleep: the cancel arm proves statement_timeout gets the
#                    backend back while the transfer is wedged.
#   /short/<file>    answer each ranged GET with an honest 206 one byte SHORT of
#                    the range asked for (Content-Range and Content-Length agree):
#                    the client must refuse the short body, never pad or use it.
#   /trunc/<file>    answer each ranged GET's headers with the full length, send
#                    half the body, then close: EOF mid-body is an error.
#   /dropka/<file>   serve normally, but close the connection after every
#                    response WITHOUT a Connection: close header: the client's
#                    next request on it fails before a status line, and must be
#                    retried once on a fresh connection (the keep-alive race).
#   /hangup/<file>   close every connection without answering at all: on a fresh
#                    connection that is the server's answer, not a race to retry.
#   /swap/<file>     serve normally, but after the 4th ranged GET of <file> move
#                    <file>.next over <file>, once: another writer overwrote the
#                    key mid-read (#1805). The 4th GET is the first data chunk
#                    (open = 1 HEAD + 3 GETs), so the footer is the old object's.
#   /weak/<file>     send a weak ETag (W/"..."): If-Match compares strongly, so a
#                    weak ETag never matches and any If-Match is answered 412.
#   /swap79/<file>   as /swap/, with a strong ETag of 79 characters: the longest
#                    the client keeps, so the read is still pinned.
#   /etag79/<file>   send a strong ETag of 79 characters and never swap: the
#                    pinned read of an unchanged object must succeed whole, so a
#                    pin cut shorter than the ETag shows up (#1805 review).
#   /longetag/<file> send a strong ETag of 80 characters: one too long for the
#                    client's buffer, which must keep none rather than a cut one.
#   /fickle/<file>   send a different strong ETag on every request for the same
#                    bytes, as nodes behind a load balancer can (#1805 review).
#
# Every HEAD and GET of an object sends ETag: "<md5 of the current bytes>", and a
# GET whose If-Match names any other ETag is answered 412, as S3 does (#1805).
#
# Hook file, read per request from --dir:
#   __delete_delay__ holds a number of seconds; every DELETE is logged at once and
#                    answered only after that long. The double-cancel suite uses it
#                    to hold the dispatcher's cleanup DELETE in flight while a
#                    second cancel lands (#1741).
#   __deny_reads__   when it exists, every HEAD and every GetObject is refused
#                    with 403 AccessDenied after the signature check; PUT, POST,
#                    DELETE and the listings go on. This is a key that can write
#                    but not read, an IAM policy with s3:PutObject and no
#                    s3:GetObject (#1795).
#
# Multipart introspection and hooks (#1743), read per request from --dir:
#   __open_uploads__      written by the server, never read: the key of every
#                         upload created and neither completed nor aborted, one
#                         per line, rewritten on each change. These are the
#                         billable orphans a real store would keep.
#   __uploads_delay__     a number of seconds: CreateMultipartUpload (POST
#                         ?uploads) creates the upload at once and answers only
#                         after that long, so a suite can end the client while
#                         the upload exists and its id has not reached it.
#   __part_delay__        a number of seconds: every part PUT is stored at once
#                         and answered only after that long, so a suite can end
#                         the client while its uploads are in progress.
#   __list_uploads_delay__ a number of seconds: ListMultipartUploads (GET
#                         ?uploads) is answered only after that long, a silent
#                         store for the client's bounded wait.
#   __complete_delay__    a number of seconds (#1813): CompleteMultipartUpload
#                         (POST ?uploadId=) writes the object at once and
#                         answers only after that long, so a suite can end the
#                         client after the store committed the object.
#   __put_delay__         a number of seconds (#1813): a single-request PUT
#                         (no partNumber) writes the object at once and answers
#                         only after that long, for the same window.
#   __complete_drop__     a number of seconds (#1846): CompleteMultipartUpload
#                         writes the object at once, waits that long, and then
#                         closes the connection with no answer, as a store
#                         that applied the request and lost the connection.
#   __list_uploads_status__ an HTTP status: ListMultipartUploads (GET ?uploads)
#                         is answered with it and an AccessDenied body, so a
#                         suite can refuse the upload listing alone (#1869).
#   __put_status__        "STATUS KEY": a plain PUT (no multipart) of exactly
#                         "bucket/KEY" is answered with STATUS and not stored,
#                         so a suite can refuse one object, such as a parallel
#                         export's _SUCCESS marker, and let every other through.
# --seed-upload KEY (repeatable) opens an upload at KEY ("bucket/key") before
# the first request: an upload some other writer left, which a client must not
# abort as its own.
#
# SigV4 verification mode (#393 M2): with --sigv4-key/--sigv4-secret/
# --sigv4-region, every request must carry a valid AWS Signature Version 4
# Authorization header. The signature is recomputed here with python's stdlib
# hmac/hashlib, so a green data check proves the C signer and an INDEPENDENT
# implementation agree on every byte of the canonical request; any mismatch is
# a 403 with SignatureDoesNotMatch. --sigv4-token additionally requires the
# x-amz-security-token header, with the right value, inside the signed set.
# --tamper-bucket names a top-level path component whose requests are verified
# against a deliberately different secret, so a correctly signing client is
# always refused there: the suite's 403-surface arm. The refusal comes before
# the body is read, and closes the connection with the body unread, as a real
# store does: a body larger than the socket buffers then fails to send after
# the 403 has arrived (#1796 D).
#
# Written fresh for pgColumnar.

import argparse
import hashlib
import hmac
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TLSServer(ThreadingHTTPServer):
    """TLS wrap per connection, with the ATTEMPT logged before the handshake.

    A client that refuses our certificate (wrong host, expired, untrusted CA)
    aborts during the handshake and never reaches the request handler, so the
    request log would show nothing and a refusal arm could pass vacuously
    against a server that was never contacted. Logging HANDSHAKE at accept
    time gives the suite its reached-the-code premise (#393 M3).
    """

    def __init__(self, addr, handler, certfile, keyfile):
        super().__init__(addr, handler)
        self.tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls_ctx.load_cert_chain(certfile, keyfile)

    def get_request(self):
        sock, addr = self.socket.accept()
        with open(self.reqlog, "a") as f:
            f.write("HANDSHAKE - -\n")
        return self.tls_ctx.wrap_socket(sock, server_side=True), addr

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
AUTH_RE = re.compile(
    r"^AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request,\s*"
    r"SignedHeaders=([^,]+),\s*Signature=([0-9a-f]{64})$")


def canonical_query(qs):
    """AWS canonical query: sorted, strictly UriEncoded (the query variant
    encodes '/'). qs is the raw string after '?'."""
    if not qs:
        return ""
    def enc(x):
        return urllib.parse.quote(urllib.parse.unquote_plus(x), safe="-._~")
    pairs = []
    for part in qs.split("&"):
        k, _, v = part.partition("=")
        pairs.append((enc(k), enc(v)))
    return "&".join("%s=%s" % kv for kv in sorted(pairs))


def sigv4_expected(secret, method, path, query, headers, signed_headers,
                   amzdate, datestamp, region, payload_hash):
    canon_headers = "".join(
        "%s:%s\n" % (h, headers.get(h, "").strip()) for h in signed_headers)
    creq = "\n".join([method, path, canonical_query(query), canon_headers,
                      ";".join(signed_headers), payload_hash])
    scope = "%s/%s/s3/aws4_request" % (datestamp, region)
    sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                     hashlib.sha256(creq.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (datestamp, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # silence stderr chatter
        pass

    def parse_request(self):
        # The handler serves every request of a connection: forget the last
        # request's body, so _deny can tell whether this one's was read.
        self._body = None
        return super().parse_request()

    def _log_line(self):
        rng = self.headers.get("Range", "-").replace(" ", "")
        rawpath, _, query = self.path.partition("?")
        with open(self.server.reqlog, "a") as f:
            f.write("%s %s %s %s\n" % (self.command, rawpath,
                                        query or "-", rng))

    def _vhost_bucket(self):
        """The bucket a virtual-host request names in its Host header (the
        leftmost label(s) before --virtual-host-base), or None for a
        path-style request."""
        base = getattr(self.server, "vhost_base", None)
        if base:
            hosthdr = self.headers.get("Host", "").split(":", 1)[0]
            if hosthdr.endswith("." + base):
                return hosthdr[: -len(base) - 1]
        return None

    def _list_bucket(self):
        """The bucket a LIST names: from the Host header under virtual-host
        addressing (#1783), else the first path segment."""
        bucket = self._vhost_bucket()
        if bucket is not None:
            return bucket
        return self.path.partition("?")[0].lstrip("/").split("/", 1)[0]

    def _resolve(self):
        # SigV4 verification runs over the RAW request target (that is what
        # the client signed); only the filesystem lookup decodes it, and the
        # query never names a file.
        path = urllib.parse.unquote(self.path.partition("?")[0])
        mode = "normal"
        for prefix in ("/norange/", "/stall/", "/short/", "/trunc/", "/dropka/", "/hangup/",
                       "/swap/", "/weak/", "/swap79/", "/etag79/", "/longetag/", "/fickle/"):
            if path.startswith(prefix):
                mode = prefix.strip("/")
                path = "/" + path[len(prefix):]
                break
        # virtual-host addressing (#621): the bucket is the leftmost Host
        # label(s) before the configured base, and the request path is the key
        # alone, so re-attach the bucket for the on-disk lookup.
        bucket = self._vhost_bucket()
        if bucket is not None:
            path = "/" + bucket + path
        local = os.path.join(self.server.rootdir, path.lstrip("/"))
        return mode, local

    def _deny(self, status, code):
        body = ("<?xml version=\"1.0\"?><Error><Code>%s</Code></Error>"
                % code).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        # A refusal that leaves a request body unread closes the connection, as
        # a real store does (#1796 D): the unread bytes are not a next request.
        # Closing with them unread resets the connection, so a client still
        # sending a body larger than the socket buffers fails its send after
        # the answer has arrived.
        clen = int(self.headers.get("Content-Length", "0") or "0")
        if clen > 0 and getattr(self, "_body", None) is None:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _reads_denied(self):
        """True, with a 403 sent, when the __deny_reads__ hook exists."""
        if os.path.exists(os.path.join(self.server.rootdir, "__deny_reads__")):
            self._deny(403, "AccessDenied")
            return True
        return False

    def _sigv4_check(self):
        """True when the request may proceed. Answers 403 itself otherwise."""
        srv = self.server
        if srv.sigv4_secret is None:
            # Unsigned mode still reads a write's body (#1779): the PUT and
            # POST handlers use self._body, which only the verifier below set,
            # so an unsigned write killed the handler before any answer.
            if self.command in ("PUT", "POST"):
                clen = int(self.headers.get("Content-Length", "0") or "0")
                self._body = self.rfile.read(clen) if clen else b""
            return True
        auth = self.headers.get("Authorization", "")
        m = AUTH_RE.match(auth)
        if m is None:
            self._deny(403, "AccessDenied")
            return False
        keyid, datestamp, region, signed, got_sig = m.groups()
        signed_headers = signed.split(";")
        secret = srv.sigv4_secret
        # The tamper bucket verifies against a different secret, so a client
        # signing correctly with the real one is always refused there.
        top = self.path.lstrip("/").split("/", 1)[0]
        if srv.tamper_bucket is not None and top == srv.tamper_bucket:
            secret = srv.sigv4_secret + "-tampered"
        if keyid != srv.sigv4_key or region != srv.sigv4_region:
            self._deny(403, "InvalidAccessKeyId")
            return False
        if srv.sigv4_token is not None:
            if ("x-amz-security-token" not in signed_headers or
                    self.headers.get("x-amz-security-token") != srv.sigv4_token):
                self._deny(403, "InvalidToken")
                return False
        # Stricter than AWS on purpose: our client PROMISES to sign the Range
        # header (design/ISSUE_393_M2_SIGV4.md), and a compliant verifier
        # cannot falsify that promise because it follows the client's own
        # SignedHeaders list. This pin can: a ranged request whose signature
        # excludes Range is refused, so the removal proof (drop range from the
        # signed set) goes red here instead of passing as protocol-legal.
        if self.headers.get("Range") and "range" not in signed_headers:
            self._deny(403, "UnsignedRange")
            return False
        amzdate = self.headers.get("x-amz-date", "")
        payload_hash = self.headers.get("x-amz-content-sha256", "")
        rawpath, _, query = self.path.partition("?")
        want = sigv4_expected(secret, self.command, rawpath, query,
                              self.headers, signed_headers, amzdate,
                              datestamp, region, payload_hash)
        if not hmac.compare_digest(want, got_sig):
            self._deny(403, "SignatureDoesNotMatch")
            return False
        # Requests carrying a body must hash it truthfully (#394): the write
        # arms are cross-implementation checks of the payload hash too.
        clen = int(self.headers.get("Content-Length", "0") or "0")
        if self.command in ("PUT", "POST") :
            self._body = self.rfile.read(clen) if clen else b""
            if payload_hash != "UNSIGNED-PAYLOAD" and                hashlib.sha256(self._body).hexdigest() != payload_hash:
                self._deny(403, "XAmzContentSHA256Mismatch")
                return False
        return True

    def _head_common(self, local):
        if not os.path.isfile(local):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return -1
        return os.path.getsize(local)

    def _etag(self, local, mode):
        with open(local, "rb") as f:
            md5 = hashlib.md5(f.read()).hexdigest()
        if mode == "weak":
            return 'W/"%s"' % md5
        if mode in ("swap79", "etag79", "longetag"):
            width = 80 if mode == "longetag" else 79
            return '"%s"' % (md5 + "x" * (width - 2 - len(md5)))
        if mode == "fickle":
            with self.server.uploads_lock:
                self.server.fickle_seq += 1
                return '"%s-%d"' % (md5, self.server.fickle_seq)
        return '"%s"' % md5

    def do_HEAD(self):
        self._log_line()
        if not self._sigv4_check():
            return
        if self._reads_denied():
            return
        mode, local = self._resolve()
        if mode == "hangup":
            self.close_connection = True
            return
        size = self._head_common(local)
        if size < 0:
            return
        if mode == "dropka":
            self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", self._etag(local, mode))
        self.end_headers()

    def _do_list_v2(self, query):
        # ListObjectsV2 over the objects on disk under rootdir/<bucket>. Paged
        # by --list-page-size using an opaque continuation token (the last key
        # returned), so the client's paging loop is exercised.
        #
        # Fuzz hook: if <rootdir>/__listing_override__ exists, its raw bytes are
        # returned verbatim as the listing body, so a harness can feed the C
        # parser arbitrary (malformed) XML and check the backend survives.
        #
        # Status hook: if <rootdir>/__listing_status__ exists, its content is an
        # HTTP status every listing is answered with, an empty body, so a harness
        # can prove a failed LIST is refused rather than read as an empty prefix.
        status = os.path.join(self.server.rootdir, "__listing_status__")
        if os.path.exists(status):
            with open(status) as f:
                code = int(f.read().strip())
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        override = os.path.join(self.server.rootdir, "__listing_override__")
        if os.path.exists(override):
            with open(override, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        q = urllib.parse.parse_qs(query)
        bucket = self._list_bucket()
        prefix = q.get("prefix", [""])[0]
        token = q.get("continuation-token", [None])[0]
        base = os.path.join(self.server.rootdir, bucket)
        keys = []
        for root, _dirs, names in os.walk(base):
            for nm in names:
                full = os.path.join(root, nm)
                key = os.path.relpath(full, base).replace(os.sep, "/")
                if key.startswith(prefix):
                    keys.append(key)
        keys.sort()
        start = 0
        if token is not None:
            start = len([k for k in keys if k <= token])
        page_size = self.server.list_page_size
        page = keys[start:start + page_size]
        truncated = start + page_size < len(keys)
        next_token = page[-1] if (truncated and page) else None

        def esc(s):
            return (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))
        parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
                 "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">",
                 "<Name>%s</Name>" % esc(bucket),
                 "<Prefix>%s</Prefix>" % esc(prefix),
                 "<KeyCount>%d</KeyCount>" % len(page),
                 "<IsTruncated>%s</IsTruncated>" % ("true" if truncated else "false")]
        if next_token is not None:
            parts.append("<NextContinuationToken>%s</NextContinuationToken>"
                         % esc(next_token))
        for k in page:
            parts.append("<Contents><Key>%s</Key><Size>0</Size></Contents>"
                         % esc(k))
        parts.append("</ListBucketResult>")
        body = "".join(parts).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _do_list_uploads(self):
        # ListMultipartUploads (#1743): the uploads in progress whose key starts
        # with `prefix`, sorted by key then upload id, paged by --list-page-size
        # with key-marker/upload-id-marker as S3 does it.
        _hook_sleep(self.server.rootdir, "__list_uploads_delay__")
        lust = os.path.join(self.server.rootdir, "__list_uploads_status__")
        if os.path.exists(lust):
            with open(lust) as f:
                self._deny(int(f.read().strip()), "AccessDenied")
            return
        q = self._qdict()
        bucket = self._list_bucket()
        prefix = q.get("prefix", "")
        kmark = q.get("key-marker")
        umark = q.get("upload-id-marker", "")
        base = os.path.join(self.server.rootdir, bucket)
        with self.server.uploads_lock:
            ups = []
            for uid, up in self.server.uploads.items():
                key = os.path.relpath(up["local"], base).replace(os.sep, "/")
                if key.startswith(prefix) and not key.startswith(".."):
                    ups.append((key, uid))
        ups.sort()
        if kmark is not None:
            ups = [u for u in ups if u > (kmark, umark)]
        page_size = self.server.list_page_size
        page = ups[:page_size]
        truncated = len(ups) > page_size

        def esc(x):
            return (x.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))
        parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
                 "<ListMultipartUploadsResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">",
                 "<Bucket>%s</Bucket>" % esc(bucket),
                 "<Prefix>%s</Prefix>" % esc(prefix),
                 "<MaxUploads>%d</MaxUploads>" % page_size,
                 "<IsTruncated>%s</IsTruncated>" % ("true" if truncated else "false")]
        if truncated and page:
            parts.append("<NextKeyMarker>%s</NextKeyMarker>" % esc(page[-1][0]))
            parts.append("<NextUploadIdMarker>%s</NextUploadIdMarker>" % esc(page[-1][1]))
        for key, uid in page:
            parts.append("<Upload><Key>%s</Key><UploadId>%s</UploadId>"
                         "<Initiated>2026-10-22T00:00:00.000Z</Initiated></Upload>"
                         % (esc(key), esc(uid)))
        parts.append("</ListMultipartUploadsResult>")
        body = "".join(parts).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._log_line()
        if not self._sigv4_check():
            return
        _rawpath, _, _query = self.path.partition("?")
        # Under virtual-host addressing a LIST goes to the bucket root, "/".
        # S3 reads a GET with any other path as a GetObject of that key, so
        # this server does too (#1783): a client that sends
        # GET /bucket?list-type=2 to bucket.endpoint gets the 404 S3 gives it.
        is_get_object = self._vhost_bucket() is not None and _rawpath != "/"
        if "list-type=2" in _query and not is_get_object:
            self._do_list_v2(_query)
            return
        if "uploads" in self._qdict() and not is_get_object:
            self._do_list_uploads()
            return
        if self._reads_denied():
            return
        mode, local = self._resolve()
        if mode == "hangup":
            self.close_connection = True
            return
        size = self._head_common(local)
        if size < 0:
            return
        if mode == "dropka":
            self.close_connection = True
        etag = self._etag(local, mode)
        want = self.headers.get("If-Match")
        if want is not None and (mode == "weak" or want.strip() != etag):
            self._deny(412, "PreconditionFailed")
            return

        rng = self.headers.get("Range")
        m = RANGE_RE.match(rng.replace(" ", "")) if rng else None
        if m is None or mode == "norange":
            # No Range, or deliberately ignoring it: whole object, status 200.
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(local, "rb") as f:
                self.wfile.write(f.read())
            return

        if m.group(1) == "":                     # suffix form bytes=-N
            n = int(m.group(2))
            lo, hi = max(0, size - n), size - 1
        else:
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) != "" else size - 1
        hi = min(hi, size - 1)
        if lo > hi or lo >= size:
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if mode == "short" and hi > lo:
            hi -= 1
        n = hi - lo + 1
        with open(local, "rb") as f:
            f.seek(lo)
            body = f.read(n)
        self.send_response(206)
        self.send_header("Content-Range", "bytes %d-%d/%d" % (lo, hi, size))
        self.send_header("Content-Length", str(n))
        self.send_header("ETag", etag)
        self.end_headers()
        if mode == "trunc":
            self.wfile.write(body[: n // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        if mode == "stall":
            self.wfile.write(body[: n // 2])
            self.wfile.flush()
            time.sleep(300)                      # the suite cancels long before
            return
        self.wfile.write(body)
        if mode in ("swap", "swap79"):
            with self.server.uploads_lock:
                seen = self.server.swap_gets.get(local, 0) + 1
                self.server.swap_gets[local] = seen
                if seen == 4 and os.path.exists(local + ".next"):
                    os.replace(local + ".next", local)


    # ---- write side (#394): plain PUT, and a faithful multipart emulation --
    def _qdict(self):
        q = {}
        for part in self.path.partition("?")[2].split("&"):
            if part:
                k, _, v = part.partition("=")
                q[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        return q

    def do_PUT(self):
        self._log_line()
        if not self._sigv4_check():
            return
        q = self._qdict()
        _, local = self._resolve()
        if "uploadId" in q and "partNumber" in q:
            up = self.server.uploads.get(q["uploadId"])
            if up is None or up["local"] != local:
                self._deny(404, "NoSuchUpload")
                return
            # Part-failure hook: if <rootdir>/__part_status__ exists, it holds
            # "STATUS FROM": every part numbered FROM or above is answered with
            # STATUS and not stored, so a harness can prove a failed part fails
            # the export rather than completing a short object.
            pstat = os.path.join(self.server.rootdir, "__part_status__")
            if os.path.exists(pstat):
                with open(pstat) as f:
                    code, first = (int(x) for x in f.read().split())
                if int(q["partNumber"]) >= first:
                    self._deny(code, "InternalError")
                    return
            up["parts"][int(q["partNumber"])] = self._body
            _hook_sleep(self.server.rootdir, "__part_delay__")
            etag = hashlib.md5(self._body).hexdigest()
            self.send_response(200)
            self.send_header("ETag", '"%s"' % etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        putst = os.path.join(self.server.rootdir, "__put_status__")
        if os.path.exists(putst):
            with open(putst) as f:
                code, key = f.read().split()
            if os.path.relpath(local, self.server.rootdir) == key:
                self._deny(int(code), "AccessDenied")
                return
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(self._body)
        _hook_sleep(self.server.rootdir, "__put_delay__")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._log_line()
        if not self._sigv4_check():
            return
        q = self._qdict()
        _, local = self._resolve()
        if "uploads" in q:
            # A reserved character ('/', '+') in the id on purpose: a real AWS
            # UploadId can carry them, and the client must percent-encode the id
            # in the canonical query or its signature diverges. With this id the
            # multipart round trip passes ONLY if that encoding is correct
            # (#394 review turned from "unproven" to pinned).
            with self.server.uploads_lock:
                self.server.upload_seq += 1
                uid = "up/%d+x" % self.server.upload_seq
                self.server.uploads[uid] = {"local": local, "parts": {}}
                _write_open_uploads(self.server)
            _hook_sleep(self.server.rootdir, "__uploads_delay__")
            body = ("<?xml version=\"1.0\"?><InitiateMultipartUploadResult>"
                    "<UploadId>%s</UploadId>"
                    "</InitiateMultipartUploadResult>" % uid).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "uploadId" in q:
            # As S3 does, the object is the parts the request LISTS, in order,
            # each checked against its stored ETag; an unlisted part is
            # dropped, and a listed part that was never stored fails the
            # complete and leaves the upload open (abortable).
            listed = [(int(n), e.replace(b"&quot;", b"").strip(b'"')) for n, e in re.findall(
                rb"<PartNumber>\s*(\d+)\s*</PartNumber>\s*<ETag>([^<]*)</ETag>",
                self._body)]
            with self.server.uploads_lock:
                up = self.server.uploads.get(q["uploadId"])
                if up is None or up["local"] != local:
                    self._deny(404, "NoSuchUpload")
                    return
                nums = [n for n, _ in listed]
                if not listed or nums != sorted(set(nums)):
                    self._deny(400, "InvalidPartOrder" if listed else "MalformedXML")
                    return
                for n, e in listed:
                    if n not in up["parts"] or (e and e.decode() !=
                                                hashlib.md5(up["parts"][n]).hexdigest()):
                        self._deny(400, "InvalidPart")
                        return
                del self.server.uploads[q["uploadId"]]
                _write_open_uploads(self.server)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "wb") as f:
                for n, _ in listed:
                    f.write(up["parts"][n])
            if os.path.exists(os.path.join(self.server.rootdir, "__complete_drop__")):
                _hook_sleep(self.server.rootdir, "__complete_drop__")
                self.close_connection = True
                return
            _hook_sleep(self.server.rootdir, "__complete_delay__")
            body = b"<?xml version=\"1.0\"?><CompleteMultipartUploadResult/>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._deny(400, "InvalidRequest")

    def do_DELETE(self):
        self._log_line()
        if not self._sigv4_check():
            return
        try:
            with open(os.path.join(self.server.rootdir, "__delete_delay__")) as fh:
                time.sleep(float(fh.read().strip() or 0))
        except (OSError, ValueError):
            pass
        q = self._qdict()
        _, local = self._resolve()
        if "uploadId" in q:
            with self.server.uploads_lock:
                up = self.server.uploads.pop(q["uploadId"], None)
                if up is not None:
                    _write_open_uploads(self.server)
            if up is None:
                self._deny(404, "NoSuchUpload")
                return
        elif os.path.isfile(local):
            os.unlink(local)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _write_open_uploads(srv):
    # <rootdir>/__open_uploads__: one key per open upload. Call with uploads_lock.
    with open(os.path.join(srv.rootdir, "__open_uploads__"), "w") as f:
        for up in srv.uploads.values():
            f.write(os.path.relpath(up["local"], srv.rootdir) + "\n")


def _hook_sleep(rootdir, name):
    # Sleep for the number of seconds in <rootdir>/<name>, when it exists.
    try:
        with open(os.path.join(rootdir, name)) as fh:
            time.sleep(float(fh.read().strip() or 0))
    except (OSError, ValueError):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--sigv4-key")
    ap.add_argument("--sigv4-secret")
    ap.add_argument("--sigv4-region")
    ap.add_argument("--sigv4-token")
    ap.add_argument("--tamper-bucket")
    ap.add_argument("--tls-cert")
    ap.add_argument("--tls-key")
    ap.add_argument("--virtual-host-base",
                    help="base host under which the leftmost Host label is the "
                         "bucket (S3 virtual-host addressing, #621)")
    ap.add_argument("--list-page-size", type=int, default=1000,
                    help="ListObjectsV2 keys per page; small values force paging")
    ap.add_argument("--seed-upload", action="append", default=[],
                    help="open a multipart upload at bucket/key before serving")
    args = ap.parse_args()

    if args.tls_cert:
        srv = TLSServer(("127.0.0.1", args.port), Handler,
                        args.tls_cert, args.tls_key)
    else:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.rootdir = args.dir
    srv.reqlog = args.log
    srv.sigv4_key = args.sigv4_key
    srv.sigv4_secret = args.sigv4_secret
    srv.sigv4_region = args.sigv4_region
    srv.sigv4_token = args.sigv4_token
    srv.tamper_bucket = args.tamper_bucket
    srv.vhost_base = args.virtual_host_base
    srv.list_page_size = args.list_page_size
    srv.uploads = {}
    srv.uploads_lock = threading.Lock()
    srv.swap_gets = {}
    srv.fickle_seq = 0
    srv.upload_seq = 0
    for key in args.seed_upload:
        srv.upload_seq += 1
        srv.uploads["seed/%d+x" % srv.upload_seq] = {
            "local": os.path.join(args.dir, key), "parts": {}}
    _write_open_uploads(srv)
    open(args.log, "a").close()
    # Readiness marker for the suite: print once the socket is bound.
    print("READY", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())

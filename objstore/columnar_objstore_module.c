/*-------------------------------------------------------------------------
 * columnar_objstore_module.c
 *		Object-store byte source for pgColumnar (#393).
 *
 * Loaded on demand by PgColumnarObjStoreGet, never preloaded. See
 * src/columnar_objstore.h for why it is a separate library and for the ABI.
 *
 * M1 scope (design/ISSUE_393_M1_HTTP_SOURCE.md): plain-HTTP/1.1 ranged reads,
 * exact object keys only. GET and HEAD are the whole request set. SigV4 (M2)
 * and TLS (M3) layer on top of the same transport; https:// and s3:// report
 * unsupported until then, which is a better failure than a cleartext fallback.
 *
 * Transport rules, which are constraints rather than style:
 *
 * - No blocking syscall ever touches the socket. The backend's signal handlers
 *   are installed with SA_RESTART, so a blocking recv would resume around a
 *   query cancel instead of failing with EINTR; the tree documents the same
 *   failure shape for FIFOs (columnar_parquet_reader.c, pq_path_is_candidate).
 *   Every wait is WaitLatchOrSocket + CHECK_FOR_INTERRUPTS, which is what makes
 *   statement_timeout able to get the backend back mid-transfer.
 *
 * - A server that answers a Range request with 200 is an error, never a silent
 *   whole-object read: the caller sized its buffer for `n` bytes, and "the
 *   wrong bytes quietly" is the failure mode this tree keeps deciding not to
 *   ship.
 *
 * - One read sees one version of the object (#1805). Open keeps the HEAD's
 *   strong ETag and every ranged GET sends it as If-Match, so a key that is
 *   overwritten mid-read answers 412 and the read fails, instead of mixing a
 *   footer from one object with chunks from another. When the HEAD names a
 *   version (x-amz-version-id), every ranged GET also asks for that version,
 *   so the read finishes on it (#1867). pgcolumnar.objstore_pin_reads = off
 *   sends neither (#1866). A 403 to a GET that names a version (a caller
 *   without s3:GetObjectVersion) drops the version for the rest of the read;
 *   If-Match stays. Whatever the pin, a 206 whose Content-Range total differs
 *   from the HEAD's length fails the read, and so does a 416 for bytes inside
 *   that length (#1867).
 *
 * - Sockets are cleaned up on abort through a resource-release callback over
 *   the open-handle list. The invariant that makes close-all-on-abort correct:
 *   no handle survives its statement, so a live handle during an abort belongs
 *   to the statement being aborted. The streaming FDW (#620) holds a read
 *   handle open across IterateForeignScan calls, so a handle no longer always
 *   closes before its opener returns. The invariant still holds: the handle is
 *   the running statement's, EndForeignScan closes it on the normal path, and
 *   the release callback closes it on abort, whoever holds the pointer.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifdef HAVE_OBJSTORE_OPENSSL
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#endif

#include "common/cryptohash.h"
#include "common/hmac.h"
#include "common/sha2.h"
#include "fmgr.h"
#include "lib/ilist.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "pgtime.h"
#include "storage/fd.h"
#include "storage/latch.h"
#include "utils/guc.h"
#include "utils/memutils.h"
#include "utils/resowner.h"
#include "utils/timestamp.h"
#include "utils/wait_event.h"

#include "columnar_objstore.h"

PG_MODULE_MAGIC;

PGDLLEXPORT const PgColumnarObjStoreApi *pgcolumnar_objstore_init(void);

/* Response head is bounded before it is parsed; a server cannot size us. */
#define OS_MAX_HEAD		16384
/* Receive staging buffer; body bytes stream through it into the caller's. */
#define OS_RBUF			65536
/* One wait slice; consecutive silent slices add up to the inactivity limit. */
#define OS_WAIT_SLICE_MS	5000
#define OS_MAX_QUIET_SLICES	12	/* 60 s of silence is a dead peer */
/*
 * Bytes kept of a response ETag, terminator included, so the longest ETag kept
 * is OS_ETAG_BUF - 1 characters. The parsed ETag and the read pin share this one
 * size: a pin smaller than the parse buffer would cut a kept ETag short, and a
 * cut ETag matches nothing, so every pinned GET would fail (#1805 review).
 */
#define OS_ETAG_BUF			80

/*
 * Bytes kept of a response x-amz-version-id, terminator included. S3 version
 * ids are at most 1024 bytes. A longer one is kept as none: a cut id names
 * no version, so every GET would fail.
 */
#define OS_VERSION_BUF		1025

/*
 * Where a handle's signing key came from (#1796). Only the 403 hint reads it:
 * a refused key is fixed where it was configured, and before this every 403
 * named the server environment, including a key from a user mapping or one a
 * REST catalog vended. OS_CRED_ENV is 0, so a handle that never resolves s3://
 * credentials keeps the hint it always had.
 */
typedef enum OsCredSource
{
	OS_CRED_ENV = 0,			/* AWS_* of the server process */
	OS_CRED_MAPPING,			/* the user mapping for the foreign server */
	OS_CRED_VENDED				/* a REST catalog's loadTable reply */
} OsCredSource;

struct PgColumnarObjHandle
{
	char	   *url;			/* full URL, for error messages */
	char	   *host;			/* authority, split for connect (and Host header) */
	char	   *auth_host;		/* endpoint authority for the allow-list, NULL =
								 * use host (#621: virtual-host puts the bucket in
								 * host, but the operator authorizes the endpoint) */
	int			port;
	char	   *abspath;		/* request-target, always starts with '/' */

	int			fd;				/* -1 when disconnected */
	bool		fd_reserved;	/* ReserveExternalFD accounting */
	int64		len;			/* object length from open's HEAD */

	/*
	 * The strong ETag open's HEAD returned, sent as If-Match on every ranged
	 * GET so one read sees one version of the object (#1805). "" when the
	 * server sent none, or only a weak one: then reads are not pinned.
	 */
	char		pin_etag[OS_ETAG_BUF];

	/*
	 * "versionId=<id, percent-encoded>" from the HEAD's x-amz-version-id, sent
	 * as the query of every ranged GET (#1867). NULL when the store named no
	 * version, or the pin is off.
	 */
	char	   *pin_query;
	uint64		served;			/* requests completed on this connection */

	/*
	 * SigV4 (#393 M2). sign=false is the plain-http M1 path, byte-identical to
	 * before. The credential strings come from the ambient environment at open
	 * and live with the handle; the signing key is the four-HMAC derivation,
	 * cached per UTC day the way pgBackRest caches it.
	 */
	bool		sign;
	char	   *akid;
	char	   *secret;
	char	   *token;			/* session token, or NULL */
	char	   *region;
	OsCredSource credsrc;		/* where akid came from, for the 403 hint */
	char		sigDate[9];		/* YYYYMMDD the cached key was derived for */
	uint8		sigKey[PG_SHA256_DIGEST_LENGTH];

	/*
	 * TLS (#393 M3). tls=false is the cleartext path, byte-identical to M1/M2.
	 * OpenSSL performs chain verification and hostname binding; this module
	 * only configures it (design/ISSUE_393_M3_TLS.md). The SSL object rides
	 * the same nonblocking fd, so the M1 wait loop and cancel semantics hold.
	 */
	bool		tls;
#ifdef HAVE_OBJSTORE_OPENSSL
	SSL_CTX    *sctx;
	SSL		   *ssl;
#endif

	/*
	 * When not 0, every wait on this handle ends by this time, and the request
	 * raises once it passes (#1743). Set by the calls that take a timeout.
	 */
	TimestampTz deadline;
	int			deadline_ms;	/* the timeout it came from, for the message */

	/*
	 * When true, os_write_request holds interrupts from the last body byte
	 * sent until the answer is read (#1813). Set around a request whose
	 * effect the store may apply even if its answer never arrives.
	 */
	bool		holdForAnswer;

	/* receive staging */
	uint8	   *rb;
	int			rb_off;			/* consumed up to here */
	int			rb_len;			/* valid bytes */

	dlist_node	node;			/* open-handle list, for abort cleanup */
};

/* SHA-256 of an empty payload; every request we sign sends no body. */
#define OS_EMPTY_SHA256 \
	"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

/* One parsed response head. */
typedef struct OsResponse
{
	int			status;
	int64		content_length; /* -1 when absent */
	bool		chunked;
	bool		conn_close;
	char		etag[OS_ETAG_BUF];	/* response ETag, "" when absent or too long */
	char		version_id[OS_VERSION_BUF]; /* x-amz-version-id, "" when absent
											 * or too long */
	int64		range_total;	/* Content-Range total, -1 when absent or "*" */
} OsResponse;

static dlist_head os_open_handles = DLIST_STATIC_INIT(os_open_handles);
static bool os_callback_registered = false;

static void os_tls_handshake(PgColumnarObjHandle *h);

/* ---------------------------------------------------------------- lifecycle */

static void
os_disconnect(PgColumnarObjHandle *h)
{
#ifdef HAVE_OBJSTORE_OPENSSL
	if (h->ssl != NULL)
	{
		/*
		 * Best-effort close_notify: one nonblocking attempt, never a wait. A
		 * peer that already went away would otherwise make disconnect block,
		 * and disconnect runs on error paths.
		 */
		ERR_clear_error();
		(void) SSL_shutdown(h->ssl);
		SSL_free(h->ssl);
		h->ssl = NULL;
	}
	if (h->sctx != NULL)
	{
		SSL_CTX_free(h->sctx);
		h->sctx = NULL;
	}
	ERR_clear_error();
#endif
	if (h->fd >= 0)
	{
		close(h->fd);
		h->fd = -1;
	}
	if (h->fd_reserved)
	{
		ReleaseExternalFD();
		h->fd_reserved = false;
	}
	h->served = 0;
	h->rb_off = h->rb_len = 0;
}

static void
os_free_handle(PgColumnarObjHandle *h)
{
	os_disconnect(h);
	dlist_delete(&h->node);
	if (h->rb)
		pfree(h->rb);
	if (h->url)
		pfree(h->url);
	if (h->host)
		pfree(h->host);
	if (h->abspath)
		pfree(h->abspath);
	if (h->akid)
		pfree(h->akid);
	if (h->secret)
		pfree(h->secret);
	if (h->token)
		pfree(h->token);
	if (h->region)
		pfree(h->region);
	if (h->pin_query)
		pfree(h->pin_query);
	pfree(h);
}

/*
 * Abort cleanup. ereport out of a read unwinds the scan without reaching
 * close(); this returns the socket and the ExternalFD reservation. See the
 * header comment for why closing every handle is correct.
 */
static void
os_resource_release(ResourceReleasePhase phase, bool isCommit,
					bool isTopLevel, void *arg)
{
	if (phase != RESOURCE_RELEASE_AFTER_LOCKS || !isTopLevel)
		return;

	while (!dlist_is_empty(&os_open_handles))
	{
		PgColumnarObjHandle *h =
			dlist_container(PgColumnarObjHandle, node,
							dlist_head_node(&os_open_handles));

		if (isCommit)
			elog(WARNING,
				 "columnar: object-store handle for \"%s\" leaked to commit",
				 h->url);
		os_free_handle(h);
	}
}

/* ------------------------------------------------------------------ waiting */

static void
os_deadline_passed(PgColumnarObjHandle *h)
{
	ereport(ERROR,
			(errcode(ERRCODE_CONNECTION_FAILURE),
			 errmsg("columnar: \"%s\" did not answer within %d ms",
					h->url, h->deadline_ms)));
}

static void
os_wait(PgColumnarObjHandle *h, uint32 sockEvents, int *quiet)
{
	int			rc;
	uint32		events = WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH | sockEvents;
	long		timeout = OS_WAIT_SLICE_MS;

	/*
	 * WL_TIMEOUT, or the slice is not a slice: without it WaitLatchOrSocket
	 * ignores the timeout and waits for the socket or the latch alone, so a
	 * silent peer never counts a quiet slice. Under proc_exit (a terminated
	 * worker aborting its upload, #1727) interrupts are held and the latch
	 * alone never ends the wait either. A handle with a deadline (#1743) has
	 * each slice cut to the time left, and the deadline is checked before and
	 * after the wait, so it is bounded whether or not interrupts are held.
	 */
	if (h->deadline != 0)
	{
		long		left = TimestampDifferenceMilliseconds(GetCurrentTimestamp(),
														   h->deadline);

		if (left <= 0)
			os_deadline_passed(h);
		timeout = Min(timeout, left);
	}
	rc = WaitLatchOrSocket(MyLatch, events, h->fd, timeout, PG_WAIT_EXTENSION);
	if (rc & WL_LATCH_SET)
		ResetLatch(MyLatch);
	CHECK_FOR_INTERRUPTS();
	if (h->deadline != 0 &&
		!(rc & (WL_SOCKET_READABLE | WL_SOCKET_WRITEABLE)) &&
		GetCurrentTimestamp() >= h->deadline)
		os_deadline_passed(h);

	if (rc & (WL_SOCKET_READABLE | WL_SOCKET_WRITEABLE))
		*quiet = 0;
	else if (++(*quiet) >= OS_MAX_QUIET_SLICES)
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: timeout reading \"%s\"", h->url),
				 errdetail("The server sent nothing for %d seconds.",
						   OS_MAX_QUIET_SLICES * OS_WAIT_SLICE_MS / 1000)));
}

/* ---------------------------------------------------------------- connect */

/*
 * The endpoint allow-list (#393, owner decision 2026-08-13). Empty, the
 * default, refuses every remote endpoint: pg_read_server_files must not be an
 * SSRF primitive out of the box. Entries are host or host:port, matched
 * case-insensitively against the endpoint the connection will use, BEFORE any
 * resolution: the list authorizes what the operator wrote. The GUC is defined
 * by the preloaded library (SUSET there, which is load-bearing) and read here
 * by name, so no cross-library symbol exists.
 */
static void
os_check_endpoint_allowed(PgColumnarObjHandle *h)
{
	const char *list =
		GetConfigOption("pgcolumnar.objstore_allowed_endpoints", true, false);
	/* the operator authorizes the endpoint; under virtual-host addressing the
	 * connect host carries the bucket, so match the endpoint authority (#621) */
	const char *checkhost = (h->auth_host != NULL) ? h->auth_host : h->host;
	bool		ok = false;

	if (list != NULL && list[0] != '\0')
	{
		char	   *copy = pstrdup(list);
		char	   *save = NULL;
		char	   *tok;

		for (tok = strtok_r(copy, ",", &save); tok != NULL;
			 tok = strtok_r(NULL, ",", &save))
		{
			char	   *entry = tok;
			char	   *end;
			char	   *colon;

			while (*entry == ' ' || *entry == '\t')
				entry++;
			end = entry + strlen(entry);
			while (end > entry && (end[-1] == ' ' || end[-1] == '\t'))
				*--end = '\0';
			if (entry[0] == '\0')
				continue;

			/*
			 * An entry may pin a port. M1 accepts no IPv6 literals in URLs,
			 * so a colon in the entry can only introduce a port.
			 */
			colon = strrchr(entry, ':');
			if (colon != NULL && colon[1] != '\0' &&
				strspn(colon + 1, "0123456789") == strlen(colon + 1))
			{
				if (atoi(colon + 1) != h->port)
					continue;
				*colon = '\0';
			}
			if (pg_strcasecmp(entry, checkhost) == 0)
			{
				ok = true;
				break;
			}
		}
		pfree(copy);
	}

	if (!ok)
	{
		/*
		 * The hint names the entry the message names, host:port, appended to
		 * the current list (#1796). It suggested the bare host as the whole
		 * list: an entry with no port matches every port on the host, and
		 * ALTER SYSTEM SET replaces the list, so the advice as written opened
		 * every port and dropped every other entry. The list goes inside a SQL
		 * literal, so any quote in it is doubled.
		 */
		StringInfoData want;
		const char *c;

		initStringInfo(&want);
		for (c = (list != NULL) ? list : ""; *c != '\0'; c++)
		{
			if (*c == '\'')
				appendStringInfoChar(&want, '\'');
			appendStringInfoChar(&want, *c);
		}
		if (want.len > 0)
			appendStringInfoChar(&want, ',');
		appendStringInfo(&want, "%s:%d", checkhost, h->port);
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("columnar: endpoint \"%s:%d\" is not in "
						"pgcolumnar.objstore_allowed_endpoints",
						checkhost, h->port),
				 errhint("A superuser can add it: ALTER SYSTEM SET "
						 "pgcolumnar.objstore_allowed_endpoints = '%s'; "
						 "SELECT pg_reload_conf();", want.data)));
	}
}

/*
 * An IPv4 address, in host byte order, that is a metadata address: 169.254/16,
 * or 100.100.100.200, Alibaba Cloud's instance-metadata address (#1848). Only
 * that one address: the rest of 100.64.0.0/10 is carrier-grade NAT.
 */
static bool
os_v4_is_metadata(uint32 a)
{
	return (a & 0xFFFF0000U) == 0xA9FE0000U ||	/* 169.254/16 */
		a == 0x646464C8U;		/* 100.100.100.200 */
}

/* The four bytes at b[i], b[j], b[k], b[l] as an IPv4 address in host order */
static uint32
os_v4_at(const uint8_t *b, int i, int j, int k, int l)
{
	return ((uint32) b[i] << 24) | ((uint32) b[j] << 16) |
		((uint32) b[k] << 8) | (uint32) b[l];
}

/*
 * Metadata-address refusal, UNCONDITIONAL (#393): 169.254.0.0/16 and fe80::/10 are
 * the cloud-metadata credential-theft surface (IMDS lives at
 * 169.254.169.254) and have no legitimate object-storage use. The same
 * surface has IPv6 forms (#1808): AWS's IPv6 IMDS in fd00:ec2::/32, Google
 * Compute Engine's at fd20:ce::254, and
 * 169.254/16 carried inside an IPv6 address -- IPv4-mapped ::ffff:0:0/96,
 * IPv4-compatible ::/96, and the NAT64 prefixes 64:ff9b::/96 and
 * 64:ff9b:1::/48 (RFC 6052, RFC 8215). Alibaba Cloud's metadata address
 * 100.100.100.200 is refused too, as IPv4 and inside each of those IPv6
 * forms (#1848). Checked after
 * resolution so a name pinned to a link-local address is caught, and the
 * whole connection is refused rather than the address skipped, so a resolver
 * returning a mixed set cannot steer the choice.
 */
static bool
os_addr_is_metadata(const struct addrinfo *ai)
{
	if (ai->ai_family == AF_INET)
		return os_v4_is_metadata(ntohl(((const struct sockaddr_in *) ai->ai_addr)->sin_addr.s_addr));
	if (ai->ai_family == AF_INET6)
	{
		const struct in6_addr *a6 =
			&((const struct sockaddr_in6 *) ai->ai_addr)->sin6_addr;

		const uint8_t *b = a6->s6_addr;
		static const uint8_t zero10[10] = {0};
		static const uint8_t nat64wkp[12] = {0x00, 0x64, 0xff, 0x9b};
		static const uint8_t gceimds6[16] = {0xfd, 0x20, 0x00, 0xce,
		0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x02, 0x54};
		bool		v4low = os_v4_is_metadata(os_v4_at(b, 12, 13, 14, 15));

		if (IN6_IS_ADDR_LINKLOCAL(a6))
			return true;
		/* fd00:ec2::/32, AWS IPv6 instance metadata (fd00:ec2::254) */
		if (b[0] == 0xfd && b[1] == 0x00 && b[2] == 0x0e && b[3] == 0xc2)
			return true;
		/* fd20:ce::254, Google Compute Engine IPv6 instance metadata */
		if (memcmp(b, gceimds6, 16) == 0)
			return true;
		/* ::ffff:0:0/96 (mapped) and ::/96 (compatible) carrying a metadata v4 */
		if (memcmp(b, zero10, 10) == 0 &&
			((b[10] == 0xff && b[11] == 0xff) || (b[10] == 0 && b[11] == 0)))
			return v4low;
		/* 64:ff9b::/96, the NAT64 well-known prefix */
		if (memcmp(b, nat64wkp, 12) == 0)
			return v4low;
		/*
		 * 64:ff9b:1::/48, the NAT64 local-use prefix. Its operator picks the
		 * prefix length, so the IPv4 address may sit at any RFC 6052 position:
		 * /48 (bytes 6,7,9,10), /56 (7,9,10,11), /64 (9-12) or /96 (12-15). The guard
		 * cannot tell which, so it checks all four and fails closed. That
		 * also refuses some ordinary addresses whose bytes land on another
		 * layout's position, e.g. 10.169.254.5 behind a /48 prefix; the
		 * docs say so.
		 */
		if (b[0] == 0x00 && b[1] == 0x64 && b[2] == 0xff && b[3] == 0x9b &&
			b[4] == 0x00 && b[5] == 0x01)
			return os_v4_is_metadata(os_v4_at(b, 6, 7, 9, 10)) ||	/* /48 */
				os_v4_is_metadata(os_v4_at(b, 7, 9, 10, 11)) ||	/* /56 */
				os_v4_is_metadata(os_v4_at(b, 9, 10, 11, 12)) ||	/* /64 */
				v4low;			/* /96 */
	}
	return false;
}

/*
 * Request-line splitting guard. The request-target and host go verbatim into
 * "%s %s HTTP/1.1\r\nHost: %s:%d\r\n" in every request builder, so a CR or LF in
 * either (from a user-controlled URL or catalog_uri) would inject a second
 * request line or header onto the connection. This is the same defense the
 * caller-supplied header lines already carry. It lives in os_connect, the one
 * path every request builder (GET, write, list, and the ABI http_request) goes
 * through, so no emit site can be missed.
 */
static void
os_reject_target_ctl(PgColumnarObjHandle *h)
{
	if (h->abspath != NULL && strpbrk(h->abspath, "\r\n") != NULL)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_EXCEPTION),
				 errmsg("columnar: an HTTP request path may not contain CR or LF")));
	if (h->host != NULL && strpbrk(h->host, "\r\n") != NULL)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_EXCEPTION),
				 errmsg("columnar: an HTTP request host may not contain CR or LF")));
}

static void
os_connect(PgColumnarObjHandle *h)
{
	struct addrinfo hints;
	struct addrinfo *addrs = NULL;
	struct addrinfo *ai;
	char		portstr[16];
	int			rc;
	int			quiet = 0;

	Assert(h->fd < 0);

	os_check_endpoint_allowed(h);
	os_reject_target_ctl(h);

	if (!h->fd_reserved)
	{
		if (!AcquireExternalFD())
			ereport(ERROR,
					(errcode(ERRCODE_INSUFFICIENT_RESOURCES),
					 errmsg("columnar: no file descriptor available to read \"%s\"",
							h->url)));
		h->fd_reserved = true;
	}

	/*
	 * Name resolution is synchronous in M1. The fixture and the ordinary
	 * endpoint form are IP literals, which never block; a DNS name against a
	 * slow resolver blocks here and is a known, documented M1 limitation
	 * rather than a socket-wait violation.
	 */
	memset(&hints, 0, sizeof(hints));
	hints.ai_family = AF_UNSPEC;
	hints.ai_socktype = SOCK_STREAM;
	snprintf(portstr, sizeof(portstr), "%d", h->port);
	rc = getaddrinfo(h->host, portstr, &hints, &addrs);
	if (rc != 0)
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: could not resolve \"%s\": %s",
						h->host, gai_strerror(rc))));

	for (ai = addrs; ai != NULL; ai = ai->ai_next)
		if (os_addr_is_metadata(ai))
		{
			freeaddrinfo(addrs);
			ereport(ERROR,
					(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
					 errmsg("columnar: \"%s\" resolves into a link-local range "
							"or an instance-metadata address, which object "
							"storage never legitimately uses",
							h->host),
					 errdetail("Link-local and instance-metadata addresses "
							   "(169.254.0.0/16, fe80::/10, fd00:ec2::/32, "
							   "fd20:ce::254, Alibaba Cloud's 100.100.100.200, and "
							   "169.254.0.0/16 or 100.100.100.200 inside an "
							   "IPv4-mapped, IPv4-compatible or NAT64 IPv6 "
							   "address) are refused "
							   "unconditionally: they are the cloud "
							   "instance-metadata surface.")));
		}

	for (ai = addrs; ai != NULL; ai = ai->ai_next)
	{
		h->fd = socket(ai->ai_family, SOCK_STREAM, 0);
		if (h->fd < 0)
			continue;
		if (fcntl(h->fd, F_SETFL, O_NONBLOCK) < 0)
		{
			close(h->fd);
			h->fd = -1;
			continue;
		}

		if (connect(h->fd, ai->ai_addr, ai->ai_addrlen) == 0)
			break;
		if (errno == EINPROGRESS)
		{
			socklen_t	errlen = sizeof(rc);

			do
			{
				os_wait(h, WL_SOCKET_WRITEABLE, &quiet);
				rc = -1;
				if (getsockopt(h->fd, SOL_SOCKET, SO_ERROR, &rc, &errlen) < 0)
					rc = errno;
			} while (rc == EINPROGRESS || rc == EALREADY);
			if (rc == 0)
				break;
			errno = rc;
		}
		close(h->fd);
		h->fd = -1;
	}
	freeaddrinfo(addrs);

	if (h->fd < 0)
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: could not connect to \"%s\": %m", h->url)));

	rc = 1;
	(void) setsockopt(h->fd, IPPROTO_TCP, TCP_NODELAY, &rc, sizeof(rc));
	h->served = 0;
	h->rb_off = h->rb_len = 0;

	if (h->tls)
		os_tls_handshake(h);
}

#ifdef HAVE_OBJSTORE_OPENSSL
/*
 * TLS handshake on the already-connected nonblocking fd (#393 M3). OpenSSL
 * verifies; we configure: SSL_VERIFY_PEER against the default verify paths
 * (which honour SSL_CERT_FILE/SSL_CERT_DIR), hostname binding through
 * X509_VERIFY_PARAM with partial wildcards refused, set1_ip_asc for IP
 * literals (X509_check_host alone silently misses them, and an IP endpoint is
 * the ordinary MinIO/Garage form), SNI only for names, TLS 1.2 minimum.
 */
static void
os_tls_handshake(PgColumnarObjHandle *h)
{
	X509_VERIFY_PARAM *param;
	unsigned char ipbuf[16];
	bool		isIp;
	int			quiet = 0;

	ERR_clear_error();
	h->sctx = SSL_CTX_new(TLS_client_method());
	if (h->sctx == NULL ||
		SSL_CTX_set_min_proto_version(h->sctx, TLS1_2_VERSION) != 1 ||
		SSL_CTX_set_default_verify_paths(h->sctx) != 1)
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: could not initialize TLS for \"%s\"",
						h->url)));
	SSL_CTX_set_verify(h->sctx, SSL_VERIFY_PEER, NULL);

	h->ssl = SSL_new(h->sctx);
	if (h->ssl == NULL || SSL_set_fd(h->ssl, h->fd) != 1)
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: could not initialize TLS for \"%s\"",
						h->url)));

	param = SSL_get0_param(h->ssl);
	X509_VERIFY_PARAM_set_hostflags(param, X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS);
	isIp = (inet_pton(AF_INET, h->host, ipbuf) == 1 ||
			inet_pton(AF_INET6, h->host, ipbuf) == 1);
	if (isIp)
	{
		/* IP literal: bind the IP, and no SNI (RFC 6066 forbids it) */
		if (X509_VERIFY_PARAM_set1_ip_asc(param, h->host) != 1)
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: could not bind the endpoint address for \"%s\"",
							h->url)));
	}
	else
	{
		if (X509_VERIFY_PARAM_set1_host(param, h->host, 0) != 1 ||
			SSL_set_tlsext_host_name(h->ssl, h->host) != 1)
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: could not bind the endpoint name for \"%s\"",
							h->url)));
	}

	for (;;)
	{
		int			rc;
		int			serr;

		ERR_clear_error();
		rc = SSL_connect(h->ssl);
		if (rc == 1)
			return;

		serr = SSL_get_error(h->ssl, rc);
		if (serr == SSL_ERROR_WANT_READ)
		{
			os_wait(h, WL_SOCKET_READABLE, &quiet);
			continue;
		}
		if (serr == SSL_ERROR_WANT_WRITE)
		{
			os_wait(h, WL_SOCKET_WRITEABLE, &quiet);
			continue;
		}

		/*
		 * Failed. A verification failure carries the reason every operator
		 * question starts with; anything else reports OpenSSL's own reason.
		 */
		{
			long		vres = SSL_get_verify_result(h->ssl);

			if (vres != X509_V_OK)
				ereport(ERROR,
						(errcode(ERRCODE_CONNECTION_FAILURE),
						 errmsg("columnar: could not establish TLS to \"%s\"",
								h->url),
						 errdetail("Certificate verification failed: %s.",
								   X509_verify_cert_error_string(vres))));
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: could not establish TLS to \"%s\"",
							h->url),
					 errdetail("%s.",
							   ERR_reason_error_string(ERR_peek_last_error()) ?
							   ERR_reason_error_string(ERR_peek_last_error()) :
							   "TLS handshake failed")));
		}
	}
}
#else
static void
os_tls_handshake(PgColumnarObjHandle *h)
{
	/* unreachable: h->tls is never set on a build without OpenSSL */
	elog(ERROR, "columnar: TLS requested but this build has no TLS support");
}
#endif

/* ------------------------------------------------------------------- send */

/*
 * Send the whole request. Returns false on a connection-level failure so the
 * caller can retry once on a stale keep-alive connection; raises for anything
 * that a fresh connection cannot fix.
 */
static bool
os_send_all(PgColumnarObjHandle *h, const char *buf, size_t len)
{
	int			quiet = 0;

#ifdef HAVE_OBJSTORE_OPENSSL
	if (h->tls)
	{
		while (len > 0)
		{
			int			rc;
			int			serr;

			ERR_clear_error();
			rc = SSL_write(h->ssl, buf, (int) len);
			if (rc > 0)
			{
				buf += rc;
				len -= (size_t) rc;
				continue;
			}
			serr = SSL_get_error(h->ssl, rc);
			if (serr == SSL_ERROR_WANT_READ)
				os_wait(h, WL_SOCKET_READABLE, &quiet);
			else if (serr == SSL_ERROR_WANT_WRITE)
				os_wait(h, WL_SOCKET_WRITEABLE, &quiet);
			else if (serr == SSL_ERROR_SYSCALL && errno == EINTR)
				continue;
			else
				return false;	/* peer went away: maybe stale keep-alive */
		}
		return true;
	}
#endif

	while (len > 0)
	{
		ssize_t		sent = send(h->fd, buf, len, MSG_NOSIGNAL);

		if (sent > 0)
		{
			buf += sent;
			len -= (size_t) sent;
			continue;
		}
		if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
		{
			os_wait(h, WL_SOCKET_WRITEABLE, &quiet);
			continue;
		}
		if (sent < 0 && errno == EINTR)
			continue;
		return false;			/* EPIPE, ECONNRESET, ... : maybe stale */
	}
	return true;
}

/* ------------------------------------------------------------------- recv */

/* Pull more bytes into the staging buffer. Returns false on orderly EOF. */
static bool
os_fill(PgColumnarObjHandle *h)
{
	int			quiet = 0;

	if (h->rb_off == h->rb_len)
		h->rb_off = h->rb_len = 0;
	else if (h->rb_len == OS_RBUF)
	{
		memmove(h->rb, h->rb + h->rb_off, h->rb_len - h->rb_off);
		h->rb_len -= h->rb_off;
		h->rb_off = 0;
	}

#ifdef HAVE_OBJSTORE_OPENSSL
	if (h->tls)
	{
		for (;;)
		{
			int			rc;
			int			serr;

			ERR_clear_error();
			rc = SSL_read(h->ssl, h->rb + h->rb_len, OS_RBUF - h->rb_len);
			if (rc > 0)
			{
				h->rb_len += rc;
				return true;
			}
			serr = SSL_get_error(h->ssl, rc);
			if (serr == SSL_ERROR_ZERO_RETURN)
				return false;	/* orderly close_notify */
			if (serr == SSL_ERROR_WANT_READ)
			{
				os_wait(h, WL_SOCKET_READABLE, &quiet);
				continue;
			}
			if (serr == SSL_ERROR_WANT_WRITE)
			{
				os_wait(h, WL_SOCKET_WRITEABLE, &quiet);
				continue;
			}
			if (serr == SSL_ERROR_SYSCALL && errno == EINTR)
				continue;
			if (serr == SSL_ERROR_SYSCALL && errno == 0)
				return false;	/* EOF without close_notify: stale keep-alive */
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: TLS read from \"%s\" failed", h->url)));
		}
	}
#endif

	for (;;)
	{
		ssize_t		got = recv(h->fd, h->rb + h->rb_len,
							   OS_RBUF - h->rb_len, 0);

		if (got > 0)
		{
			h->rb_len += (int) got;
			return true;
		}
		if (got == 0)
			return false;
		if (errno == EAGAIN || errno == EWOULDBLOCK)
		{
			os_wait(h, WL_SOCKET_READABLE, &quiet);
			continue;
		}
		if (errno == EINTR)
			continue;
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: could not read from \"%s\": %m", h->url)));
	}
}

/* --------------------------------------------------------------- response */

/*
 * True when the header line [line, eol) contains `token`, in any case. The
 * Connection header's tokens are case-insensitive (RFC 9110 section 7.6.1).
 */
static bool
os_line_has_token(const char *line, const char *eol, const char *token)
{
	size_t		tlen = strlen(token);
	const char *p;

	for (p = line; p + tlen <= eol; p++)
		if (pg_strncasecmp(p, token, tlen) == 0)
			return true;
	return false;
}

/*
 * Read and parse one response head. Returns false when the connection died
 * before a single status byte arrived (the stale keep-alive case); raises on
 * everything else.
 */
static bool
os_read_head(PgColumnarObjHandle *h, OsResponse *resp)
{
	int			headEnd = -1;
	bool		keepAlive = false;

	memset(resp, 0, sizeof(*resp));
	resp->content_length = -1;
	resp->range_total = -1;

	for (;;)
	{
		int			avail = h->rb_len - h->rb_off;
		const char *p = (const char *) h->rb + h->rb_off;
		int			i;

		for (i = 0; i + 3 < avail; i++)
			if (memcmp(p + i, "\r\n\r\n", 4) == 0)
			{
				headEnd = i + 4;
				break;
			}
		if (headEnd >= 0)
			break;
		if (avail >= OS_MAX_HEAD)
			ereport(ERROR,
					(errcode(ERRCODE_PROTOCOL_VIOLATION),
					 errmsg("columnar: oversized HTTP response header from \"%s\"",
							h->url)));
		if (!os_fill(h))
		{
			if (h->rb_len - h->rb_off == 0)
				return false;	/* clean EOF before any byte: stale */
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: connection to \"%s\" closed mid-response",
							h->url)));
		}
	}

	{
		const char *head;
		const char *line;
		const char *end;

		/*
		 * NUL-terminate the head (over its final LF) so the strstr calls
		 * below cannot run past the staging buffer: rb is not a C string, and
		 * an unbounded strstr reads whatever follows the allocation until it
		 * happens upon a zero byte.
		 */
		h->rb[h->rb_off + headEnd - 1] = '\0';
		head = (const char *) h->rb + h->rb_off;
		line = head;
		end = head + headEnd;

		if (headEnd < 12 || strncmp(head, "HTTP/1.", 7) != 0)
			ereport(ERROR,
					(errcode(ERRCODE_PROTOCOL_VIOLATION),
					 errmsg("columnar: \"%s\" did not answer HTTP", h->url)));
		resp->status = atoi(head + 9);

		while (line < end)
		{
			const char *eol = memchr(line, '\n', end - line);

			if (eol == NULL)
				break;
			if (pg_strncasecmp(line, "Content-Length:", 15) == 0)
				resp->content_length = strtoll(line + 15, NULL, 10);
			else if (pg_strncasecmp(line, "Transfer-Encoding:", 18) == 0 &&
					 strstr(line, "chunked") != NULL && strstr(line, "chunked") < eol)
				resp->chunked = true;
			else if (pg_strncasecmp(line, "Connection:", 11) == 0)
			{
				if (os_line_has_token(line + 11, eol, "close"))
					resp->conn_close = true;
				if (os_line_has_token(line + 11, eol, "keep-alive"))
					keepAlive = true;
			}
			else if (pg_strncasecmp(line, "ETag:", 5) == 0)
			{
				const char *p = line + 5;
				int			i = 0;

				while (p < eol && (*p == ' ' || *p == '\t'))
					p++;
				while (p < eol && *p != '\r' && *p != '\n' &&
					   i < (int) sizeof(resp->etag) - 1)
					resp->etag[i++] = *p++;
				resp->etag[i] = '\0';
				/* A cut-down ETag would match nothing: keep none instead. */
				if (p < eol && *p != '\r' && *p != '\n')
					resp->etag[0] = '\0';
			}
			else if (pg_strncasecmp(line, "x-amz-version-id:", 17) == 0)
			{
				const char *p = line + 17;
				int			i = 0;

				while (p < eol && (*p == ' ' || *p == '\t'))
					p++;
				while (p < eol && *p != '\r' && *p != '\n' &&
					   i < (int) sizeof(resp->version_id) - 1)
					resp->version_id[i++] = *p++;
				resp->version_id[i] = '\0';
				/* A cut-down id would name no version: keep none instead. */
				if (p < eol && *p != '\r' && *p != '\n')
					resp->version_id[0] = '\0';
			}
			else if (pg_strncasecmp(line, "Content-Range:", 14) == 0)
			{
				/* bytes FIRST-LAST/TOTAL (a 416 puts "*" before the slash); TOTAL may be "*" */
				const char *slash = memchr(line, '/', eol - line);

				if (slash != NULL && slash[1] >= '0' && slash[1] <= '9')
					resp->range_total = strtoll(slash + 1, NULL, 10);
			}
			line = eol + 1;
		}

		/*
		 * An HTTP/1.0 connection does not persist unless the reply says
		 * keep-alive (RFC 9112 section 9.3). Reusing it sent the next request
		 * into a connection the server was closing (#1830).
		 */
		if (head[7] == '0' && !keepAlive)
			resp->conn_close = true;
	}

	h->rb_off += headEnd;
	return true;
}

/* Take min(n, buffered) bytes from staging into dst (or discard). */
static int64
os_take(PgColumnarObjHandle *h, uint8 *dst, int64 n)
{
	int64		avail = h->rb_len - h->rb_off;
	int64		take = Min(n, avail);

	if (take > 0 && dst != NULL)
		memcpy(dst, h->rb + h->rb_off, take);
	h->rb_off += (int) take;
	return take;
}

/* Read exactly n body bytes into dst (NULL discards). */
static void
os_read_exact(PgColumnarObjHandle *h, uint8 *dst, int64 n)
{
	while (n > 0)
	{
		int64		got = os_take(h, dst, n);

		if (dst != NULL)
			dst += got;
		n -= got;
		if (n > 0 && !os_fill(h))
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: connection to \"%s\" closed mid-body",
							h->url)));
	}
}

/* Read one CRLF-terminated line (chunk framing) into buf; bounded. */
static void
os_read_line(PgColumnarObjHandle *h, char *buf, size_t cap)
{
	size_t		used = 0;

	for (;;)
	{
		while (h->rb_off < h->rb_len)
		{
			char		c = (char) h->rb[h->rb_off++];

			if (c == '\n')
			{
				buf[used] = '\0';
				return;
			}
			if (c != '\r')
			{
				if (used + 1 >= cap)
					ereport(ERROR,
							(errcode(ERRCODE_PROTOCOL_VIOLATION),
							 errmsg("columnar: oversized chunk header from \"%s\"",
									h->url)));
				buf[used++] = c;
			}
		}
		if (!os_fill(h))
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: connection to \"%s\" closed mid-body",
							h->url)));
	}
}

/*
 * Consume a response body. dst != NULL requires the body to be exactly `want`
 * bytes; dst == NULL discards a body of any length (error pages, HEAD has
 * none and must not call this).
 */
static void
os_read_body(PgColumnarObjHandle *h, const OsResponse *resp,
			 uint8 *dst, int64 want)
{
	if (resp->chunked)
	{
		char		line[64];
		int64		total = 0;

		for (;;)
		{
			int64		sz;

			os_read_line(h, line, sizeof(line));
			sz = strtoll(line, NULL, 16);
			if (sz < 0)
				ereport(ERROR,
						(errcode(ERRCODE_PROTOCOL_VIOLATION),
						 errmsg("columnar: bad chunk size from \"%s\"", h->url)));
			if (sz == 0)
			{
				/* trailers until the empty line */
				do
				{
					os_read_line(h, line, sizeof(line));
				} while (line[0] != '\0');
				break;
			}
			if (dst != NULL && total + sz > want)
				ereport(ERROR,
						(errcode(ERRCODE_PROTOCOL_VIOLATION),
						 errmsg("columnar: \"%s\" sent more bytes than requested",
								h->url)));
			os_read_exact(h, dst ? dst + total : NULL, sz);
			total += sz;
			os_read_line(h, line, sizeof(line));	/* chunk-terminating CRLF */
		}
		if (dst != NULL && total != want)
			ereport(ERROR,
					(errcode(ERRCODE_PROTOCOL_VIOLATION),
					 errmsg("columnar: \"%s\" sent %lld of %lld requested bytes",
							h->url, (long long) total, (long long) want)));
	}
	else
	{
		int64		cl = resp->content_length;

		if (cl < 0)
			ereport(ERROR,
					(errcode(ERRCODE_PROTOCOL_VIOLATION),
					 errmsg("columnar: \"%s\" sent a body with no length", h->url)));
		if (dst != NULL && cl != want)
			ereport(ERROR,
					(errcode(ERRCODE_PROTOCOL_VIOLATION),
					 errmsg("columnar: \"%s\" sent %lld of %lld requested bytes",
							h->url, (long long) cl, (long long) want)));
		os_read_exact(h, dst, cl);
	}
}

static char *os_slurp_body(PgColumnarObjHandle *h, const OsResponse *resp,
						   int64 cap, int64 *outlen);

/* ---------------------------------------------------------------- signing */

static void
os_sha256(const uint8 *data, size_t len, uint8 out[PG_SHA256_DIGEST_LENGTH])
{
	pg_cryptohash_ctx *ctx = pg_cryptohash_create(PG_SHA256);

	if (ctx == NULL ||
		pg_cryptohash_init(ctx) < 0 ||
		pg_cryptohash_update(ctx, data, len) < 0 ||
		pg_cryptohash_final(ctx, out, PG_SHA256_DIGEST_LENGTH) < 0)
		elog(ERROR, "columnar: SHA-256 computation failed");
	pg_cryptohash_free(ctx);
}

static void
os_hmac256(const uint8 *key, size_t klen,
		   const uint8 *data, size_t dlen,
		   uint8 out[PG_SHA256_DIGEST_LENGTH])
{
	pg_hmac_ctx *ctx = pg_hmac_create(PG_SHA256);

	if (ctx == NULL ||
		pg_hmac_init(ctx, key, klen) < 0 ||
		pg_hmac_update(ctx, data, dlen) < 0 ||
		pg_hmac_final(ctx, out, PG_SHA256_DIGEST_LENGTH) < 0)
		elog(ERROR, "columnar: HMAC-SHA-256 computation failed");
	pg_hmac_free(ctx);
}

static void
os_hex(const uint8 *in, size_t n, char *out)	/* out: 2n+1 bytes */
{
	static const char digits[] = "0123456789abcdef";
	size_t		i;

	for (i = 0; i < n; i++)
	{
		out[2 * i] = digits[in[i] >> 4];
		out[2 * i + 1] = digits[in[i] & 0xf];
	}
	out[2 * n] = '\0';
}

/*
 * AWS UriEncode for the path: unreserved bytes and '/' pass, everything else
 * is %XX with UPPERCASE hex. Hand-written per AWS's own recommendation:
 * platform encoders disagree on exactly the bytes that break signatures
 * (space, '+', '=', '~').
 */
static void
os_uriencode_path(StringInfo out, const char *s)
{
	for (; *s != '\0'; s++)
	{
		unsigned char c = (unsigned char) *s;

		if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
			(c >= '0' && c <= '9') ||
			c == '-' || c == '.' || c == '_' || c == '~' || c == '/')
			appendStringInfoChar(out, (char) c);
		else
			appendStringInfo(out, "%%%02X", c);
	}
}

/*
 * The query-string variant: '/' is NOT exempt (SigV4 canonical-query encoding
 * percent-encodes every reserved byte). Used for a multipart UploadId value,
 * which S3 returns opaque and AWS ids can carry reserved characters; encoding
 * it here means the query the client signs matches AWS's canonicalization
 * regardless of what the server chose (#394 review). Garage/MinIO ids are
 * unreserved, so this is a no-op there and byte-identical to before.
 */
static char *
os_uriencode_query(const char *s)
{
	StringInfoData out;

	initStringInfo(&out);
	for (; *s != '\0'; s++)
	{
		unsigned char c = (unsigned char) *s;

		if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
			(c >= '0' && c <= '9') ||
			c == '-' || c == '.' || c == '_' || c == '~')
			appendStringInfoChar(&out, (char) c);
		else
			appendStringInfo(&out, "%%%02X", c);
	}
	return out.data;
}

/*
 * Derive (or reuse) the day's signing key and return the SigV4 headers for one
 * request: x-amz-content-sha256, x-amz-date, optional x-amz-security-token,
 * and Authorization. `rangeVal` is the exact Range header value the request
 * will carry, or NULL for HEAD; the Range header is signed, which leaves no
 * unsigned header a middlebox could rewrite.
 */
static void
os_sign_request(PgColumnarObjHandle *h, const char *method,
				const char *query, const char *rangeVal, StringInfo headers)
{
	char		amzdate[20];	/* YYYYMMDDTHHMMSSZ */
	char		datestamp[9];	/* YYYYMMDD */
	pg_time_t	now = (pg_time_t) time(NULL);
	StringInfoData creq;
	StringInfoData signedlist;
	StringInfoData sts;
	uint8		digest[PG_SHA256_DIGEST_LENGTH];
	char		hexdigest[PG_SHA256_DIGEST_LENGTH * 2 + 1];
	char		signature[PG_SHA256_DIGEST_LENGTH * 2 + 1];

	pg_strftime(amzdate, sizeof(amzdate), "%Y%m%dT%H%M%SZ", pg_gmtime(&now));
	memcpy(datestamp, amzdate, 8);
	datestamp[8] = '\0';

	/* the four chained HMACs, cached per UTC day */
	if (strcmp(h->sigDate, datestamp) != 0)
	{
		StringInfoData seed;
		uint8		k[PG_SHA256_DIGEST_LENGTH];

		initStringInfo(&seed);
		appendStringInfo(&seed, "AWS4%s", h->secret);
		os_hmac256((uint8 *) seed.data, seed.len,
				   (uint8 *) datestamp, 8, k);
		os_hmac256(k, sizeof(k), (uint8 *) h->region, strlen(h->region), k);
		os_hmac256(k, sizeof(k), (uint8 *) "s3", 2, k);
		os_hmac256(k, sizeof(k), (uint8 *) "aws4_request", 12, k);
		memcpy(h->sigKey, k, sizeof(k));
		strlcpy(h->sigDate, datestamp, sizeof(h->sigDate));
		pfree(seed.data);
	}

	/* signed-headers list, alphabetical by construction */
	initStringInfo(&signedlist);
	appendStringInfoString(&signedlist, "host");
	if (rangeVal != NULL)
		appendStringInfoString(&signedlist, ";range");
	appendStringInfoString(&signedlist, ";x-amz-content-sha256;x-amz-date");
	if (h->token != NULL)
		appendStringInfoString(&signedlist, ";x-amz-security-token");

	/* canonical request */
	initStringInfo(&creq);
	appendStringInfo(&creq, "%s\n%s\n%s\n", method, h->abspath,
					 query != NULL ? query : "");
	appendStringInfo(&creq, "host:%s:%d\n", h->host, h->port);
	if (rangeVal != NULL)
		appendStringInfo(&creq, "range:%s\n", rangeVal);
	appendStringInfo(&creq, "x-amz-content-sha256:%s\n", OS_EMPTY_SHA256);
	appendStringInfo(&creq, "x-amz-date:%s\n", amzdate);
	if (h->token != NULL)
		appendStringInfo(&creq, "x-amz-security-token:%s\n", h->token);
	appendStringInfo(&creq, "\n%s\n%s", signedlist.data, OS_EMPTY_SHA256);

	os_sha256((uint8 *) creq.data, creq.len, digest);
	os_hex(digest, sizeof(digest), hexdigest);

	/* string to sign, then the signature */
	initStringInfo(&sts);
	appendStringInfo(&sts, "AWS4-HMAC-SHA256\n%s\n%s/%s/s3/aws4_request\n%s",
					 amzdate, datestamp, h->region, hexdigest);
	os_hmac256(h->sigKey, sizeof(h->sigKey),
			   (uint8 *) sts.data, sts.len, digest);
	os_hex(digest, sizeof(digest), signature);

	appendStringInfo(headers, "x-amz-content-sha256: %s\r\n", OS_EMPTY_SHA256);
	appendStringInfo(headers, "x-amz-date: %s\r\n", amzdate);
	if (h->token != NULL)
		appendStringInfo(headers, "x-amz-security-token: %s\r\n", h->token);
	appendStringInfo(headers,
					 "Authorization: AWS4-HMAC-SHA256 Credential=%s/%s/%s/s3/aws4_request, "
					 "SignedHeaders=%s, Signature=%s\r\n",
					 h->akid, datestamp, h->region, signedlist.data, signature);

	pfree(creq.data);
	pfree(signedlist.data);
	pfree(sts.data);
}

/* ---------------------------------------------------------------- request */

/*
 * The character that joins a request's own query to the path. An http(s)://
 * path keeps the URL's query string (#1785), so a second parameter joins it
 * with '&', never a second '?' (#1904). An S3 path is percent-encoded and
 * never holds a '?'.
 */
static const char *
os_query_sep(const PgColumnarObjHandle *h)
{
	return strchr(h->abspath, '?') != NULL ? "&" : "?";
}

/*
 * One request/response cycle with a single reconnect on a stale keep-alive
 * connection. `off`/`n` describe the Range for GET; HEAD sends no Range.
 */
static void
os_request(PgColumnarObjHandle *h, const char *method,
		   int64 off, int64 n, OsResponse *resp)
{
	StringInfoData req;
	bool		isGet = (strcmp(method, "GET") == 0);
	/* A ranged GET asks for the version open saw (#1867); the HEAD never. */
	const char *query = isGet ? h->pin_query : NULL;
	int			attempt;

	initStringInfo(&req);
	appendStringInfo(&req, "%s %s%s%s HTTP/1.1\r\nHost: %s:%d\r\n",
					 method, h->abspath, query != NULL ? os_query_sep(h) : "",
					 query != NULL ? query : "", h->host, h->port);
	if (isGet)
		appendStringInfo(&req, "Range: bytes=%lld-%lld\r\n",
						 (long long) off, (long long) (off + n - 1));
	/* Pin the read to the version open saw (#1805); S3 answers 412 if not. */
	if (isGet && h->pin_etag[0] != '\0')
		appendStringInfo(&req, "If-Match: %s\r\n", h->pin_etag);
	if (h->sign)
	{
		char		rangeVal[64];

		if (isGet)
			snprintf(rangeVal, sizeof(rangeVal), "bytes=%lld-%lld",
					 (long long) off, (long long) (off + n - 1));
		os_sign_request(h, method, query, isGet ? rangeVal : NULL, &req);
	}
	appendStringInfoString(&req, "User-Agent: pgcolumnar-objstore/1\r\n\r\n");

	for (attempt = 0;; attempt++)
	{
		bool		fresh = (h->fd < 0);

		if (h->fd < 0)
			os_connect(h);

		if (os_send_all(h, req.data, req.len) && os_read_head(h, resp))
			break;

		/*
		 * The connection failed before a status byte. On a connection that
		 * already served a request this is the ordinary keep-alive race and is
		 * retried once on a fresh connection; on a fresh connection it is the
		 * server's answer and is an error.
		 */
		os_disconnect(h);
		if (fresh || attempt > 0)
			ereport(ERROR,
					(errcode(ERRCODE_CONNECTION_FAILURE),
					 errmsg("columnar: connection to \"%s\" failed before a response",
							h->url)));
	}

	pfree(req.data);
}

/*
 * A 403 means the server rejected our authentication or signature. Read out
 * the XML <Code> element when the body is small and simply framed, so the
 * error names the server's reason; the credential itself never appears
 * anywhere. Raises; does not return.
 */
static void
os_reject_403(PgColumnarObjHandle *h, const OsResponse *resp, bool hasBody)
{
	char		code[64] = "";

	/*
	 * hasBody is false for HEAD: its response advertises a Content-Length but
	 * carries no body bytes, and reading them would wait on a transfer that
	 * never comes.
	 */
	if (hasBody && !resp->chunked && resp->content_length > 0 &&
		resp->content_length < 8192)
	{
		char	   *body = palloc((Size) resp->content_length + 1);
		char	   *codeStart;

		os_read_exact(h, (uint8 *) body, resp->content_length);
		body[resp->content_length] = '\0';
		codeStart = strstr(body, "<Code>");
		if (codeStart != NULL)
		{
			char	   *codeEnd = strstr(codeStart, "</Code>");

			if (codeEnd != NULL &&
				codeEnd - (codeStart + 6) < (ptrdiff_t) sizeof(code))
			{
				memcpy(code, codeStart + 6, codeEnd - (codeStart + 6));
				code[codeEnd - (codeStart + 6)] = '\0';
			}
		}
		pfree(body);
	}

	/*
	 * Only an s3:// or gs:// handle signs (h->sign). An http(s):// request
	 * carries no credential, so pointing its reader at the AWS_* environment
	 * would send them to settings the request never used (#1779).
	 */
	if (!h->sign)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("columnar: access to \"%s\" was denied (HTTP 403%s%s)",
						h->url, code[0] ? ": " : "", code),
				 errhint("Requests to http:// and https:// URLs are never "
						 "signed. Use an s3:// or gs:// URL for a store that "
						 "needs credentials.")));

	/* the hint names where the refused key was configured (#1796) */
	ereport(ERROR,
			(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
			 errmsg("columnar: access to \"%s\" was denied (HTTP 403%s%s)",
					h->url, code[0] ? ": " : "", code),
			 h->credsrc == OS_CRED_MAPPING ?
			 errhint("Check access_key_id and secret_access_key in the user "
					 "mapping the current role uses for this server.") :
			 h->credsrc == OS_CRED_VENDED ?
			 errhint("The storage credentials the catalog vended for this "
					 "table were refused.") :
			 errhint("Check the AWS_* credential environment of the server "
					 "process.")));
}

/* -------------------------------------------------------------------- ABI */

static bool
objstore_handles_url(const char *url)
{
	/*
	 * http:// (M1), s3:// (M2), and https:// when this module was built with
	 * OpenSSL (M3). Without OpenSSL, https:// stays unhandled so it reports
	 * "no such URL scheme" rather than silently downgrading to cleartext.
	 */
#ifdef HAVE_OBJSTORE_OPENSSL
	if (pg_strncasecmp(url, "https://", 8) == 0)
		return true;
#endif
	return pg_strncasecmp(url, "http://", 7) == 0 ||
		pg_strncasecmp(url, "s3://", 5) == 0 ||
		pg_strncasecmp(url, "gs://", 5) == 0;	/* GCS via the interop XML API (#621) */
}

/* A required credential-environment variable, or the 28000 the design owes. */
static const char *
os_require_env(const char *name, const char *url)
{
	const char *v = getenv(name);

	if (v == NULL || v[0] == '\0')
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("columnar: reading \"%s\" requires %s in the server "
						"environment", url, name),
				 errhint("Ambient credentials are read from the postmaster's "
						 "environment. Set %s and restart, or use a local "
						 "path.", name)));
	return v;
}

/*
 * Resolve an s3://bucket/key URL: endpoint and region from the catalog config
 * when present, else the environment; credentials from the config's mapping
 * triple when present, else the environment IF ambient use is allowed
 * (superuser, credentials_required=false, or a function-path caller), else a
 * 28000 refusal. Path-style request target with the key percent-encoded
 * exactly as it is signed.
 *
 * Its messages name h->url, not `url`. The two are the same except for a
 * bucket-level request, which resolves from a dummy key, "s3://bucket/_", and
 * names the URL the caller gave in h->url (os_write_handle_as). A refusal here
 * used to name that dummy key (#1869).
 */
static void
os_resolve_s3(PgColumnarObjHandle *h, const char *url,
			  const PgColumnarObjStoreConfig *cfg)
{
	const char *bucket = url + 5;	/* both "s3://" and "gs://" are 5 chars */
	const char *slash = strchr(bucket, '/');
	bool		isGs = (pg_strncasecmp(url, "gs://", 5) == 0);
	const char *ep;
	const char *ephost;
	const char *epslash;
	const char *epcolon;
	const char *region;
	const char *token;
	const char *addr;
	char	   *authhost;
	bool		virtualHost;
	bool		ambientOk = (cfg == NULL || cfg->allow_ambient);
	StringInfoData path;
	MemoryContext oldcxt;

	if (slash == NULL || slash == bucket || slash[1] == '\0')
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" is not %s://bucket/key", h->url,
						isGs ? "gs" : "s3")));

	/*
	 * Userinfo is refused here, the same as os_open and http_request do for
	 * http(s) (#706). This path never did, and the failure was quiet rather than
	 * open: the '@' is not special to a bucket name, so `s3://u:p@bucket/key`
	 * parsed, the userinfo became part of the BUCKET, and the caller was told the
	 * object does not exist.
	 *
	 * Measured on main with an endpoint configured (#995):
	 *
	 *   path style     HEAD /u%3Ap%40pgc-bucket/vh.parquet, Host untouched
	 *                  -> "does not exist (HTTP 404)"
	 *   virtual host   the bucket is the leftmost Host label, so the whole string
	 *                  goes to the resolver -> "could not resolve
	 *                  \"u:p@pgc-bucket.s3.local\""
	 *   write          export_parquet raised NO ERROR and PUT to
	 *                  /u%3Ap%40pgc-bucket/ui.parquet -- a bucket the caller
	 *                  never named
	 *
	 * So this is a wrong-reason defect and not an SSRF: nothing here splits the
	 * authority at '@', and the userinfo never becomes basic auth or moves the
	 * host. The write row is why it is worth refusing rather than documenting --
	 * acceptance there sends bytes somewhere nobody asked for.
	 *
	 * BEFORE THE ENDPOINT IS RESOLVED, deliberately. Placed after it, the refusal
	 * would be unreachable whenever no endpoint is configured, because the s3
	 * branch demands one first (28000) -- and the arms for this in
	 * test/objstore_userinfo.sh would then need an object-store fixture to say
	 * anything at all.
	 */
	if (memchr(bucket, '@', slash - bucket) != NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: userinfo in \"%s\" is not supported", h->url)));

	if (cfg != NULL && cfg->endpoint != NULL)
		ep = cfg->endpoint;
	else
	{
		ep = getenv("AWS_ENDPOINT_URL");
		if (ep == NULL || ep[0] == '\0')
		{
			/* GCS has a well-known interop endpoint; S3 requires an explicit
			 * one, since there is no single default S3 authority (#621). */
			if (isGs)
				ep = "https://storage.googleapis.com";
			else
				ep = os_require_env("AWS_ENDPOINT_URL", h->url);
		}
	}
	/*
	 * Userinfo in the ENDPOINT, which #997 left (#995). The bucket guard above
	 * cannot see it: the userinfo is in the endpoint the operator configured,
	 * not in the s3:// URL the caller wrote.
	 *
	 * HERE, AND NOT AT THE AUTHORITY PARSE BELOW. Placed there it sits behind the
	 * region demand, so an endpoint carrying userinfo with no region configured
	 * reports the region instead -- and an arm for it would need a region set for
	 * no reason connected to what it tests. MEASURED: all five endpoint arms in
	 * test/objstore_userinfo.sh returned "requires a region option" until this
	 * moved. That is the same reasoning the bucket guard carries for sitting
	 * before the endpoint is resolved at all, and the trap #995 named.
	 *
	 * The scheme is not yet checked here, so the scan is the whole endpoint
	 * rather than its authority. An '@' cannot occur in "http://" or "https://",
	 * and one in a PATH is still a malformed endpoint, so the wider scan refuses
	 * the same set and costs nothing.
	 *
	 * TWO SHAPES, AND ONLY ONE WAS EVER CAUGHT -- measured on the parse itself:
	 *
	 *   http://u:p@host:30829   -> host "u",          port 0       <- refused
	 *   http://user@host:30829  -> host "user@host",  port 30829   <- port VALID
	 *
	 * The first has a colon INSIDE the userinfo, so the authority split lands
	 * there and the port becomes atoi("p@host:30829") = 0. The second has no such
	 * colon, the real port survives, and the invalid-port refusal never fires at
	 * all. #995 measured the first and concluded "refused as invalid host or
	 * port" -- true of that shape, not of the code.
	 *
	 * The second shape is why this is a guard and not a message change. Its
	 * refusal came from the allow-list, naming the host it could not match, and
	 * the hint then told the operator:
	 *
	 *     ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = 'user@host'
	 *
	 * A diagnostic that invites widening a security boundary to accommodate a
	 * parse bug is worse than a wrong error code.
	 *
	 * The message names the ENDPOINT rather than `url`, because that is the
	 * string carrying the userinfo.
	 */
	if (strchr(ep, '@') != NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: userinfo in object-store endpoint \"%s\" is "
						"not supported", ep),
				 errhint("Remove the user:password@ from the endpoint and supply "
						 "credentials through AWS_ACCESS_KEY_ID and "
						 "AWS_SECRET_ACCESS_KEY.")));

	if (pg_strncasecmp(ep, "https://", 8) == 0)
	{
#ifdef HAVE_OBJSTORE_OPENSSL
		h->tls = true;
#else
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: AWS_ENDPOINT_URL is https, but this "
						"object-store module was built without OpenSSL"),
				 errhint("Rebuild the module with OpenSSL available, or use "
						 "an explicitly configured http endpoint.")));
#endif
	}
	else if (pg_strncasecmp(ep, "http://", 7) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: AWS_ENDPOINT_URL must be an http:// or "
						"https:// URL")));

	if (cfg != NULL && cfg->region != NULL)
		region = cfg->region;
	else
	{
		region = getenv("AWS_REGION");
		if (region == NULL || region[0] == '\0')
			region = getenv("AWS_DEFAULT_REGION");
		if (region == NULL || region[0] == '\0')
		{
			/* GCS interop accepts "auto"; S3 needs a real region for the
			 * signature scope (#621). */
			if (isGs)
				region = "auto";
			else
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
						 errmsg("columnar: reading \"%s\" requires a region option "
								"on the server, or AWS_REGION in the server "
								"environment", h->url)));
		}
	}

	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	if (cfg != NULL && cfg->akid != NULL)
	{
		/* the mapping's credential triple; the validator required the pair */
		h->credsrc = cfg->vended ? OS_CRED_VENDED : OS_CRED_MAPPING;
		h->akid = pstrdup(cfg->akid);
		h->secret = pstrdup(cfg->secret);
		h->token = (cfg->token != NULL && cfg->token[0] != '\0')
			? pstrdup(cfg->token) : NULL;
	}
	else if (ambientOk)
	{
		h->credsrc = OS_CRED_ENV;
		h->akid = pstrdup(os_require_env("AWS_ACCESS_KEY_ID", h->url));
		h->secret = pstrdup(os_require_env("AWS_SECRET_ACCESS_KEY", h->url));
		token = getenv("AWS_SESSION_TOKEN");
		h->token = (token != NULL && token[0] != '\0') ? pstrdup(token) : NULL;
	}
	else
	{
		MemoryContextSwitchTo(oldcxt);
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("columnar: no credentials for \"%s\"", h->url),
				 errdetail("No user mapping for this server carries "
						   "access_key_id and secret_access_key for the "
						   "current role."),
				 errhint("Create a user mapping with credentials, or have a "
						 "superuser set credentials_required 'false' on the "
						 "mapping to permit the server environment's ambient "
						 "identity.")));
	}
	h->region = pstrdup(region);

	/* endpoint authority */
	ephost = ep + (h->tls ? 8 : 7);
	epslash = strchr(ephost, '/');
	if (epslash == NULL)
		epslash = ephost + strlen(ephost);
	epcolon = memchr(ephost, ':', epslash - ephost);
	if (epcolon != NULL)
	{
		authhost = pnstrdup(ephost, epcolon - ephost);
		h->port = atoi(epcolon + 1);
	}
	else
	{
		authhost = pnstrdup(ephost, epslash - ephost);
		h->port = h->tls ? 443 : 80;
	}

	/*
	 * Addressing style (#621). Path-style (the default, byte-identical to
	 * before) sends every request to the endpoint authority with the bucket as
	 * the first path segment. Virtual-host addressing puts the bucket in the
	 * host (bucket.s3.region.amazonaws.com) and the key alone in the path, which
	 * is what AWS now prefers and what the cert is verified against. The
	 * endpoint allow-list still authorizes the endpoint authority, so auth_host
	 * carries it under virtual-host.
	 */
	addr = GetConfigOption("pgcolumnar.objstore_s3_addressing", true, false);
	virtualHost = (addr != NULL && pg_strcasecmp(addr, "virtual") == 0);

	initStringInfo(&path);
	appendStringInfoChar(&path, '/');
	if (virtualHost)
	{
		char	   *bname = pnstrdup(bucket, slash - bucket);

		h->host = psprintf("%s.%s", bname, authhost);
		h->auth_host = authhost;
		os_uriencode_path(&path, slash + 1);	/* the key alone */
	}
	else
	{
		h->host = authhost;
		h->auth_host = NULL;
		os_uriencode_path(&path, bucket);	/* bucket/key together; '/' passes */
	}
	h->abspath = path.data;
	MemoryContextSwitchTo(oldcxt);

	h->sign = true;
}

static PgColumnarObjHandle *
objstore_open(const char *url, const PgColumnarObjStoreConfig *cfg, int64 *len)
{
	PgColumnarObjHandle *h;
	OsResponse	resp;
	bool		isS3 = (pg_strncasecmp(url, "s3://", 5) == 0 ||
						pg_strncasecmp(url, "gs://", 5) == 0);	/* #621: GCS interop */
	MemoryContext oldcxt;

	if (!os_callback_registered)
	{
		RegisterResourceReleaseCallback(os_resource_release, NULL);
		os_callback_registered = true;
	}

	/*
	 * The handle and its buffers live in TopMemoryContext and are freed
	 * explicitly (close, or the release callback on abort): the per-scan
	 * context this open runs in dies during an abort BEFORE the callback
	 * walks the list, and a handle in that context would be freed memory by
	 * the time the callback closes its socket.
	 */
	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	h = (PgColumnarObjHandle *) palloc0(sizeof(PgColumnarObjHandle));
	h->fd = -1;
	h->url = pstrdup(url);
	h->rb = (uint8 *) palloc(OS_RBUF);
	MemoryContextSwitchTo(oldcxt);
	dlist_push_head(&os_open_handles, &h->node);

	if (isS3)
		os_resolve_s3(h, url, cfg);
	else
	{
		bool		isHttps = (pg_strncasecmp(url, "https://", 8) == 0);
		const char *authority = url + (isHttps ? 8 : 7);
		const char *slash = strchr(authority, '/');
		const char *colon;

		if (slash == NULL || slash == authority)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("columnar: \"%s\" has no object path", url)));
		if (memchr(authority, '@', slash - authority) != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("columnar: userinfo in \"%s\" is not supported", url)));

		oldcxt = MemoryContextSwitchTo(TopMemoryContext);
		h->abspath = pstrdup(slash);
		colon = memchr(authority, ':', slash - authority);
		if (colon != NULL)
		{
			h->host = pnstrdup(authority, colon - authority);
			h->port = atoi(colon + 1);
		}
		else
		{
			h->host = pnstrdup(authority, slash - authority);
			h->port = isHttps ? 443 : 80;
		}
		MemoryContextSwitchTo(oldcxt);
		h->tls = isHttps;
	}

	if (h->port <= 0 || h->port > 65535 ||
		h->host == NULL || h->host[0] == '\0')
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" has an invalid host or port", url)));

	os_request(h, "HEAD", 0, 0, &resp);
	if (resp.status == 403)
		os_reject_403(h, &resp, false);
	if (resp.status == 404)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_FILE),
				 errmsg("columnar: \"%s\" does not exist (HTTP 404)", url)));
	if (resp.status != 200)
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("columnar: unexpected HTTP status %d for \"%s\"",
						resp.status, url)));
	if (resp.content_length < 0)
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("columnar: \"%s\" reported no length", url)));
	/* HEAD carries no body, whatever its headers say about one. */
	if (resp.conn_close)
		os_disconnect(h);
	else
		h->served++;

	h->len = resp.content_length;
	*len = h->len;

	/*
	 * pgcolumnar.objstore_pin_reads = off leaves the read unpinned (#1866),
	 * for a server whose ETag changes from one request to the next.
	 */
	{
		const char *pin = GetConfigOption("pgcolumnar.objstore_pin_reads",
										  true, false);

		if (pin != NULL && strcmp(pin, "off") == 0)
			return h;
	}

	/*
	 * If-Match compares strongly (RFC 9110 13.1.1), so a weak ETag would make
	 * every ranged GET fail. Such a server's reads stay unpinned.
	 */
	if (strncmp(resp.etag, "W/", 2) != 0)
		strlcpy(h->pin_etag, resp.etag, sizeof(h->pin_etag));

	/*
	 * A versioned store names the version (#1867): ask for it on every ranged
	 * GET, so an overwrite mid-read leaves this read on the version it
	 * opened. "null" is the id S3 gives an object written while versioning
	 * was off; that version is overwritten in place, so it pins nothing that
	 * If-Match does not.
	 */
	if (resp.version_id[0] != '\0' && strcmp(resp.version_id, "null") != 0)
	{
		char	   *enc = os_uriencode_query(resp.version_id);

		h->pin_query = MemoryContextAlloc(TopMemoryContext,
										  strlen(enc) + sizeof("versionId="));
		sprintf(h->pin_query, "versionId=%s", enc);
		pfree(enc);
	}
	return h;
}

static void
objstore_read(PgColumnarObjHandle *h, int64 off, void *buf, size_t n)
{
	OsResponse	resp;

	Assert(h != NULL && n > 0);

	os_request(h, "GET", off, (int64) n, &resp);

	if (resp.status == 200)
	{
		/*
		 * The server ignored the Range header. Refuse rather than read: the
		 * caller asked for n bytes and a whole-object body silently desyncs
		 * every later read on this connection.
		 */
		os_disconnect(h);
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("columnar: \"%s\" ignored a Range request", h->url),
				 errdetail("The server answered 200 with the whole object "
						   "instead of 206 with the requested bytes.")));
	}
	/*
	 * A GET that names a version needs s3:GetObjectVersion, and a policy that
	 * grants only s3:GetObject answers it 403. Drop the version for the rest
	 * of this read and ask again: If-Match still pins it, as before #1867. A
	 * 403 for any other reason comes back on the retry and is reported then.
	 */
	if (resp.status == 403 && h->pin_query != NULL)
	{
		os_disconnect(h);
		pfree(h->pin_query);
		h->pin_query = NULL;
		ereport(DEBUG1,
				(errmsg_internal("columnar: \"%s\" refused a GET that names a version (HTTP 403); "
								 "reading without the version", h->url)));
		objstore_read(h, off, buf, n);
		return;
	}
	if (resp.status == 403)
		os_reject_403(h, &resp, true);
	if (resp.status == 404)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_FILE),
				 errmsg("columnar: \"%s\" does not exist (HTTP 404)", h->url)));
	if (resp.status == 412 && h->pin_etag[0] != '\0')
	{
		/* The body is the server's, not ours; drop the connection with it. */
		os_disconnect(h);
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("columnar: \"%s\" changed while it was being read", h->url),
				 errdetail("The object no longer has ETag %s, which it had when "
						   "the read began, so the server refused the rest of "
						   "the read (HTTP 412).", h->pin_etag),
				 errhint("Run the statement again to read the new version.")));
	}
	/*
	 * The read asks only for bytes inside the length the HEAD gave. A 416
	 * says the object no longer has them: it shrank under the read.
	 */
	if (resp.status == 416 && off + (int64) n <= h->len)
	{
		os_disconnect(h);
		if (resp.range_total >= 0)
			ereport(ERROR,
					(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
					 errmsg("columnar: \"%s\" changed while it was being read", h->url),
					 errdetail("The object is now %lld bytes long; it was %lld bytes "
							   "when the read began (HTTP 416 Content-Range).",
							   (long long) resp.range_total, (long long) h->len),
					 errhint("Run the statement again to read the new version.")));
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("columnar: \"%s\" changed while it was being read", h->url),
				 errdetail("The server has no bytes %lld-%lld, which were inside the "
						   "%lld bytes the object had when the read began (HTTP 416).",
						   (long long) off, (long long) (off + (int64) n - 1),
						   (long long) h->len),
				 errhint("Run the statement again to read the new version.")));
	}
	if (resp.status != 206)
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("columnar: unexpected HTTP status %d for \"%s\"",
						resp.status, h->url)));

	/*
	 * The 206 states the object's whole length (#1867). A length other than
	 * the HEAD's means another writer replaced the object, even on a server
	 * with no ETag to pin: refuse the rest of the read.
	 */
	if (resp.range_total >= 0 && resp.range_total != h->len)
	{
		os_disconnect(h);
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("columnar: \"%s\" changed while it was being read", h->url),
				 errdetail("The object is now %lld bytes long; it was %lld bytes "
						   "when the read began (HTTP 206 Content-Range).",
						   (long long) resp.range_total, (long long) h->len),
				 errhint("Run the statement again to read the new version.")));
	}

	os_read_body(h, &resp, (uint8 *) buf, (int64) n);

	if (resp.conn_close)
		os_disconnect(h);
	else
		h->served++;
}

static void
objstore_close(PgColumnarObjHandle *h)
{
	if (h != NULL)
		os_free_handle(h);
}

/* ------------------------------------------------------------- write side */

/* One staged part is 8 MiB, comfortably above S3's 5 MiB non-final minimum.
 * A dev GUC lowers it so a small fixture exercises the multipart path without
 * generating tens of MiB (the same shape as pgcolumnar.sink_fail_after). */
#define OS_PART_SIZE_DEFAULT	(8 * 1024 * 1024)

static int
os_part_size(void)
{
	const char *v =
		GetConfigOption("pgcolumnar.objstore_part_size", true, false);
	int			n = (v != NULL && v[0] != '\0') ? atoi(v) : 0;

	return n > 0 ? n : OS_PART_SIZE_DEFAULT;
}

struct PgColumnarObjSink
{
	PgColumnarObjHandle *h;		/* the resolved connection + credentials */
	StringInfoData buf;			/* bytes not yet flushed as a part */
	char	   *uploadId;		/* NULL until a multipart upload begins */
	bool		createSent;		/* POST ?uploads was sent (#1810) */
	PgColumnarObjStoreConfig *cfg;	/* a copy of create's cfg, or NULL */
	StringInfoData completeXml; /* <Part> list built as parts complete */
	int			partNo;			/* next part number (1-based) */
	int			partSize;		/* frozen at create from the GUC */
	int64		total;			/* bytes handed to sink_write */
};

static PgColumnarObjHandle *os_write_handle_as(const char *url, const char *name,
											   const PgColumnarObjStoreConfig *cfg);

/*
 * Resolve a write target into a connection handle exactly as the read path
 * does (endpoint, credentials, path-style key, allow-list and link-local
 * enforced at connect), minus the HEAD. Shares os_resolve_s3 for s3:// and
 * the plain authority parse for http(s)://.
 */
static PgColumnarObjHandle *
os_write_handle(const char *url, const PgColumnarObjStoreConfig *cfg)
{
	return os_write_handle_as(url, url, cfg);
}

/*
 * os_write_handle, resolved from `url` and named `name` in every message. A
 * bucket-level request (a LIST, the endpoint check) resolves from a dummy key
 * in the bucket and names the URL the caller gave, so no error, from the
 * resolve or from any later request, names "s3://bucket/_" (#1781, #1869).
 * h->url is read only by messages: signing, host and path come from `url`.
 */
static PgColumnarObjHandle *
os_write_handle_as(const char *url, const char *name,
				   const PgColumnarObjStoreConfig *cfg)
{
	PgColumnarObjHandle *h;
	bool		isS3 = (pg_strncasecmp(url, "s3://", 5) == 0 ||
						pg_strncasecmp(url, "gs://", 5) == 0);	/* #621: GCS interop */
	MemoryContext oldcxt;

	if (!os_callback_registered)
	{
		RegisterResourceReleaseCallback(os_resource_release, NULL);
		os_callback_registered = true;
	}

	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	h = (PgColumnarObjHandle *) palloc0(sizeof(PgColumnarObjHandle));
	h->fd = -1;
	h->url = pstrdup(name);
	h->rb = (uint8 *) palloc(OS_RBUF);
	MemoryContextSwitchTo(oldcxt);
	dlist_push_head(&os_open_handles, &h->node);

	if (isS3)
		os_resolve_s3(h, url, cfg);
	else
	{
		bool		isHttps = (pg_strncasecmp(url, "https://", 8) == 0);
		const char *authority = url + (isHttps ? 8 : 7);
		const char *slash = strchr(authority, '/');
		const char *colon;

		if (slash == NULL || slash == authority)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("columnar: \"%s\" has no object path", url)));

		/*
		 * Same userinfo refusal as os_open and http_request (#706). Without
		 * it a `user@host` write target parsed with the '@' embedded in the
		 * host (or, with `user:pass@`, split at the userinfo colon into a
		 * garbage port). Both shapes still failed closed downstream -- the
		 * allow-list cannot match an '@'-carrying host, and port 0 is
		 * refused -- but by accident and with the wrong diagnostic, and the
		 * userinfo bytes rode along in the handle's url.
		 */
		if (memchr(authority, '@', slash - authority) != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("columnar: userinfo in \"%s\" is not supported", url)));

		oldcxt = MemoryContextSwitchTo(TopMemoryContext);
		h->abspath = pstrdup(slash);
		colon = memchr(authority, ':', slash - authority);
		if (colon != NULL)
		{
			h->host = pnstrdup(authority, colon - authority);
			h->port = atoi(colon + 1);
		}
		else
		{
			h->host = pnstrdup(authority, slash - authority);
			h->port = isHttps ? 443 : 80;
		}
		MemoryContextSwitchTo(oldcxt);
		h->tls = isHttps;
	}
	if (h->port <= 0 || h->port > 65535 || h->host == NULL || h->host[0] == '\0')
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" has an invalid host or port", url)));
	return h;
}

/*
 * Sign a write request. Unlike the read signer this carries a canonical QUERY
 * string (multipart uses ?uploads, ?partNumber=&uploadId=, ?uploadId=) and a
 * REAL payload hash (the SHA-256 of the body), both of which SigV4 folds into
 * the canonical request. Caller passes the already-canonicalized query (sorted,
 * UriEncoded) and the payload hash hex.
 */
static void
os_sign_write(PgColumnarObjHandle *h, const char *method, const char *canonPath,
			  const char *canonQuery, const char *payloadHex, StringInfo headers)
{
	char		amzdate[20];
	char		datestamp[9];
	pg_time_t	now = (pg_time_t) time(NULL);
	StringInfoData creq;
	StringInfoData sts;
	StringInfoData signedlist;
	uint8		digest[PG_SHA256_DIGEST_LENGTH];
	char		hexdigest[PG_SHA256_DIGEST_LENGTH * 2 + 1];
	char		signature[PG_SHA256_DIGEST_LENGTH * 2 + 1];

	pg_strftime(amzdate, sizeof(amzdate), "%Y%m%dT%H%M%SZ", pg_gmtime(&now));
	memcpy(datestamp, amzdate, 8);
	datestamp[8] = '\0';

	if (strcmp(h->sigDate, datestamp) != 0)
	{
		StringInfoData seed;
		uint8		k[PG_SHA256_DIGEST_LENGTH];

		initStringInfo(&seed);
		appendStringInfo(&seed, "AWS4%s", h->secret);
		os_hmac256((uint8 *) seed.data, seed.len, (uint8 *) datestamp, 8, k);
		os_hmac256(k, sizeof(k), (uint8 *) h->region, strlen(h->region), k);
		os_hmac256(k, sizeof(k), (uint8 *) "s3", 2, k);
		os_hmac256(k, sizeof(k), (uint8 *) "aws4_request", 12, k);
		memcpy(h->sigKey, k, sizeof(k));
		strlcpy(h->sigDate, datestamp, sizeof(h->sigDate));
		pfree(seed.data);
	}

	initStringInfo(&signedlist);
	appendStringInfoString(&signedlist,
						   "host;x-amz-content-sha256;x-amz-date");
	if (h->token != NULL)
		appendStringInfoString(&signedlist, ";x-amz-security-token");

	initStringInfo(&creq);
	appendStringInfo(&creq, "%s\n%s\n%s\n", method, canonPath, canonQuery);
	appendStringInfo(&creq, "host:%s:%d\n", h->host, h->port);
	appendStringInfo(&creq, "x-amz-content-sha256:%s\n", payloadHex);
	appendStringInfo(&creq, "x-amz-date:%s\n", amzdate);
	if (h->token != NULL)
		appendStringInfo(&creq, "x-amz-security-token:%s\n", h->token);
	appendStringInfo(&creq, "\n%s\n%s", signedlist.data, payloadHex);

	os_sha256((uint8 *) creq.data, creq.len, digest);
	os_hex(digest, sizeof(digest), hexdigest);

	initStringInfo(&sts);
	appendStringInfo(&sts, "AWS4-HMAC-SHA256\n%s\n%s/%s/s3/aws4_request\n%s",
					 amzdate, datestamp, h->region, hexdigest);
	os_hmac256(h->sigKey, sizeof(h->sigKey),
			   (uint8 *) sts.data, sts.len, digest);
	os_hex(digest, sizeof(digest), signature);

	appendStringInfo(headers, "x-amz-content-sha256: %s\r\n", payloadHex);
	appendStringInfo(headers, "x-amz-date: %s\r\n", amzdate);
	if (h->token != NULL)
		appendStringInfo(headers, "x-amz-security-token: %s\r\n", h->token);
	appendStringInfo(headers,
					 "Authorization: AWS4-HMAC-SHA256 Credential=%s/%s/%s/s3/aws4_request, "
					 "SignedHeaders=%s, Signature=%s\r\n",
					 h->akid, datestamp, h->region, signedlist.data, signature);
	pfree(creq.data);
	pfree(sts.data);
	pfree(signedlist.data);
}

/*
 * Did the server answer a request we failed to finish sending (#1796 D)? A
 * store may refuse a PUT from its headers alone, answer 403 or 404, and close
 * with the body unread. A body larger than the socket buffers then fails to
 * send (EPIPE or ECONNRESET) although the answer has arrived, and treating the
 * failed send as no response lost the status. The answer is already in the
 * receive queue when the send fails, so this only peeks, never waits: an empty
 * queue keeps the stale keep-alive handling as it was. The store closes the
 * connection it answered early, so a later request on this handle fails its
 * send and takes the one reconnect a stale keep-alive takes.
 *
 * Plain sockets only. On TLS a failed SSL_write leaves the session unusable
 * for SSL_read, so a TLS store's early answer is still reported as no response.
 */
static bool
os_answered_early(PgColumnarObjHandle *h)
{
	char		c;

	if (h->rb_len > h->rb_off)
		return true;
	if (h->tls || h->fd < 0)
		return false;
	return recv(h->fd, &c, 1, MSG_PEEK | MSG_DONTWAIT) > 0;
}

/*
 * One write request (PUT/POST/DELETE), signed when the handle is an S3 one,
 * with an optional body and a single reconnect on a stale keep-alive.
 * `rawQuery` is the query as it goes on the wire AND the canonical query the
 * signature covers: the caller builds it with keys already sorted (partNumber
 * < uploadId; single-key queries are trivially sorted) and any UploadId value
 * already percent-encoded through os_uriencode_query, so wire and canonical
 * are byte-identical. The response body, if any, is captured into *outBody
 * (palloc'd) for the caller.
 */
#define OS_WRITE_MAX_REPLY	(1024 * 1024)

static void
os_write_request(PgColumnarObjHandle *h, const char *method, const char *rawQuery,
				 const uint8 *body, int64 bodylen, OsResponse *resp,
				 char **outBody)
{
	uint8		phash[PG_SHA256_DIGEST_LENGTH];
	char		payloadHex[PG_SHA256_DIGEST_LENGTH * 2 + 1];
	int			attempt;
	bool		held = false;

	os_sha256(body, (size_t) bodylen, phash);
	os_hex(phash, sizeof(phash), payloadHex);

	for (attempt = 0;; attempt++)
	{
		StringInfoData req;
		bool		fresh;
		bool		sent;
		bool		early;

		if (h->fd < 0)
			os_connect(h);
		fresh = (attempt == 0 && h->served == 0);

		initStringInfo(&req);
		appendStringInfo(&req, "%s %s%s%s HTTP/1.1\r\nHost: %s:%d\r\n",
						 method, h->abspath, rawQuery[0] ? os_query_sep(h) : "", rawQuery,
						 h->host, h->port);
		appendStringInfo(&req, "Content-Length: %lld\r\n", (long long) bodylen);

		/*
		 * Sign only an S3 handle, as os_request does (#1779). An http(s)://
		 * target has no region or credentials, so signing it read a NULL
		 * region and crashed the backend; its write goes unsigned, like its
		 * read.
		 */
		if (h->sign)
			os_sign_write(h, method, h->abspath, rawQuery, payloadHex, &req);
		appendStringInfoString(&req, "User-Agent: pgcolumnar-objstore/1\r\n\r\n");

		sent = os_send_all(h, req.data, req.len) &&
			(bodylen == 0 || os_send_all(h, (const char *) body, bodylen));
		early = !sent && os_answered_early(h);
		if (sent || early)
		{
			/*
			 * The store has the whole request and may apply it. From here a
			 * cancel waits for the answer, so the caller learns the outcome
			 * (#1813). The wait stays bounded by the quiet-slice timeout and
			 * any deadline, which os_wait checks with interrupts held.
			 */
			if (sent && h->holdForAnswer && !held)
			{
				HOLD_INTERRUPTS();
				held = true;
			}
			if (os_read_head(h, resp))
			{
				pfree(req.data);
				break;
			}
		}
		pfree(req.data);
		os_disconnect(h);

		/*
		 * The store had the whole commit request when the connection closed,
		 * so it may have applied it (#1846). With a cancel or terminate
		 * pending, do not send it again: the retry would let the interrupt
		 * end the statement with no word of the write. Say instead that the
		 * object may be written.
		 */
		if (held)
		{
			bool		interrupted = (QueryCancelPending || ProcDiePending);

			RESUME_INTERRUPTS();
			held = false;
			if (interrupted || fresh || attempt > 0)
				ereport(ERROR,
						(errcode(ERRCODE_CONNECTION_FAILURE),
						 errmsg("columnar: lost the connection to \"%s\" after sending the request that commits the object",
								h->url),
						 errdetail("The store may have written the object at that key.")));
		}
		if (!fresh && attempt == 0)
			continue;			/* stale keep-alive: one reconnect */
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: %s to \"%s\" failed before a response",
						method, h->url)));
	}

	if (resp->status == 403)
		os_reject_403(h, resp, true);

	/*
	 * Read the reply body with the same reader as the listing (#1830). It
	 * handles a Content-Length, chunked framing, and a body that ends when the
	 * server closes the connection, which HTTP/1.1 allows. The old code read
	 * only the first two, so it dropped a close-delimited UploadId and left
	 * the body's bytes on the handle for the next request to read as its
	 * reply. A 1xx, 204 or 304 reply has no body whatever its headers say
	 * (RFC 9112 section 6.3). Garage answers a DELETE with a 204 that has no
	 * Content-Length and keeps the connection open, so reading that reply
	 * until close would wait until Garage drops the idle connection, about
	 * 60 s. A body with a length of 1 MiB or more is an error page nobody
	 * reads, so it is drained.
	 *
	 * Any other reply with neither a length nor chunked framing ends at
	 * connection close (RFC 9112 section 6.3). A store that sends such a
	 * reply and keeps the connection open makes this read wait for the
	 * quiet-slice timeout. A chunked or close-delimited body over 1 MiB
	 * raises os_slurp_body's size error, even on a 200 to
	 * CompleteMultipartUpload, after the store has written the object. A
	 * real S3 reply is far smaller; docs/sql-reference.md states the limit.
	 */
	{
		char	   *b = NULL;
		bool		untilClose = false;

		if ((resp->status >= 100 && resp->status < 200) ||
			resp->status == 204 || resp->status == 304)
			;
		else if (!resp->chunked && resp->content_length >= OS_WRITE_MAX_REPLY)
			os_read_body(h, resp, NULL, 0);		/* drain */
		else if (resp->chunked || resp->content_length != 0)
		{
			untilClose = (!resp->chunked && resp->content_length < 0);
			b = os_slurp_body(h, resp, OS_WRITE_MAX_REPLY, NULL);
		}
		if (outBody != NULL)
			*outBody = b;
		else if (b != NULL)
			pfree(b);

		/* a close-delimited body leaves the connection at end of stream */
		if (untilClose)
			resp->conn_close = true;
	}

	if (resp->conn_close)
		os_disconnect(h);
	else
		h->served++;
	if (held)
		RESUME_INTERRUPTS();
}

/*
 * S3 multipart limits (#1857). An upload holds parts 1 to 10000, and AWS S3
 * and GCS refuse a part under 5 MiB unless it is the last. The store checks
 * the part size only when the upload completes, after every part went up.
 */
#define OS_MAX_PARTS		10000
#define OS_MIN_PART_SIZE	(5 * 1024 * 1024)

/* True when `host` is `domain` or a name under it, in any case. */
static bool
os_host_in_domain(const char *host, const char *domain)
{
	size_t		hl = strlen(host);
	size_t		dl = strlen(domain);

	if (hl > 0 && host[hl - 1] == '.')
		hl--;					/* a fully qualified name's trailing dot */
	if (hl == dl)
		return pg_strncasecmp(host, domain, dl) == 0;
	return hl > dl && host[hl - dl - 1] == '.' &&
		pg_strncasecmp(host + hl - dl, domain, dl) == 0;
}

/*
 * True when the write goes to AWS S3, Google Cloud Storage or Cloudflare R2,
 * the stores known to refuse a part under 5 MiB. Other stores are not checked:
 * some accept smaller parts, and the test servers rely on that.
 */
static bool
os_store_needs_part_minimum(const PgColumnarObjHandle *h)
{
	const char *host = h->auth_host != NULL ? h->auth_host : h->host;

	if (!h->sign || host == NULL)
		return false;
	return os_host_in_domain(host, "amazonaws.com") ||
		os_host_in_domain(host, "amazonaws.com.cn") ||
		os_host_in_domain(host, "storage.googleapis.com") ||
		os_host_in_domain(host, "r2.cloudflarestorage.com");
}

/* Upload the buffered bytes as the next multipart part; record its ETag. */
static void
os_flush_part(PgColumnarObjSink *s)
{
	OsResponse	resp;
	char	   *body = NULL;
	char	   *encId;
	char	   *query;
	const char *etag;

	/* refuse part 10001 before sending it; the abort then ends the upload */
	if (s->partNo > OS_MAX_PARTS)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("columnar: \"%s\" needs more than %d parts at a part size of %d bytes",
						s->h->url, OS_MAX_PARTS, s->partSize),
				 errdetail("A multipart upload holds at most %d parts. The export had produced %lld bytes.",
						   OS_MAX_PARTS, (long long) s->total),
				 errhint("Raise pgcolumnar.objstore_part_size so the object fits in %d parts.",
						 OS_MAX_PARTS)));
	encId = os_uriencode_query(s->uploadId);
	query = psprintf("partNumber=%d&uploadId=%s", s->partNo, encId);

	os_write_request(s->h, "PUT", query, (uint8 *) s->buf.data, s->buf.len,
					 &resp, &body);
	pfree(query);
	pfree(encId);
	if (resp.status != 200)
		ereport(ERROR,
				(errcode(ERRCODE_IO_ERROR),
				 errmsg("columnar: uploading a part of \"%s\" failed (HTTP %d)",
						s->h->url, resp.status)));
	/* The part's ETag is the server's word for its bytes; CompleteMultipart
	 * validates the list against it on a real S3, so echo it verbatim (already
	 * quoted by the server). The fixture checks it the same way and assembles
	 * the listed parts, so both are satisfied. */
	etag = resp.etag[0] ? resp.etag : "\"\"";
	appendStringInfo(&s->completeXml,
					 "<Part><PartNumber>%d</PartNumber><ETag>%s</ETag></Part>",
					 s->partNo, etag);
	if (body != NULL)
		pfree(body);
	resetStringInfo(&s->buf);
	s->partNo++;
}

static void
os_begin_multipart(PgColumnarObjSink *s)
{
	OsResponse	resp;
	char	   *body = NULL;
	char	   *idStart;
	char	   *idEnd;
	MemoryContext oldcxt;

	/*
	 * A part under 5 MiB that is not the last makes AWS S3 and GCS refuse the
	 * complete step (#1857). The upload starts when the buffer holds a whole
	 * part, so the object will almost always need more than one part. Refuse
	 * now, before the store holds anything. An object of exactly one part is
	 * refused too, although the store would accept it; that edge is rare.
	 */
	if (s->partSize < OS_MIN_PART_SIZE && os_store_needs_part_minimum(s->h))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: a part size of %d bytes is below the 5 MiB minimum that \"%s\" requires",
						s->partSize, s->h->url),
				 errdetail("AWS S3 and Google Cloud Storage refuse a multipart upload whose parts, except the last, are under 5 MiB (%d bytes).",
						   OS_MIN_PART_SIZE),
				 errhint("Set pgcolumnar.objstore_part_size to %d or more, or to 0 for the 8 MiB default.",
						 OS_MIN_PART_SIZE)));

	/* from here the store may hold an upload this sink has no id for */
	s->createSent = true;
	os_write_request(s->h, "POST", "uploads=", NULL, 0, &resp, &body);
	if (resp.status != 200 || body == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_IO_ERROR),
				 errmsg("columnar: could not start a multipart upload for \"%s\" (HTTP %d)",
						s->h->url, resp.status)));
	idStart = strstr(body, "<UploadId>");
	idEnd = idStart ? strstr(idStart, "</UploadId>") : NULL;
	if (idStart == NULL || idEnd == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_IO_ERROR),
				 errmsg("columnar: multipart upload for \"%s\" returned no UploadId",
						s->h->url)));
	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	s->uploadId = pnstrdup(idStart + 10, idEnd - (idStart + 10));
	MemoryContextSwitchTo(oldcxt);
	s->partNo = 1;
	pfree(body);
}

/*
 * Free the sink and its TopMemoryContext-resident buffers (#394 review): the
 * handle is freed by the caller (finish/abort) before this. Called at every
 * terminal point so a long-lived session running many exports does not
 * accumulate them.
 */
static void
os_sink_free(PgColumnarObjSink *s)
{
	if (s->buf.data != NULL)
		pfree(s->buf.data);
	if (s->completeXml.data != NULL)
		pfree(s->completeXml.data);
	if (s->uploadId != NULL)
		pfree(s->uploadId);
	if (s->cfg != NULL)
	{
		const char *strs[] = {s->cfg->endpoint, s->cfg->region, s->cfg->akid,
		s->cfg->secret, s->cfg->token};
		int			i;

		for (i = 0; i < lengthof(strs); i++)
			if (strs[i] != NULL)
				pfree((char *) strs[i]);
		pfree(s->cfg);
	}
	pfree(s);
}

static const char *
os_strdup_or_null(const char *v)
{
	return v != NULL ? pstrdup(v) : NULL;
}

static PgColumnarObjSink *
objstore_sink_create(const char *url, const PgColumnarObjStoreConfig *cfg)
{
	PgColumnarObjSink *s;
	MemoryContext oldcxt;

	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	s = (PgColumnarObjSink *) palloc0(sizeof(PgColumnarObjSink));
	initStringInfo(&s->buf);
	initStringInfo(&s->completeXml);

	/*
	 * Keep the cfg: an abort with no upload id lists and aborts with it, so
	 * it must use the same endpoint and credentials as the upload (#1846).
	 */
	if (cfg != NULL)
	{
		s->cfg = (PgColumnarObjStoreConfig *) palloc(sizeof(*cfg));
		*s->cfg = *cfg;
		s->cfg->endpoint = os_strdup_or_null(cfg->endpoint);
		s->cfg->region = os_strdup_or_null(cfg->region);
		s->cfg->akid = os_strdup_or_null(cfg->akid);
		s->cfg->secret = os_strdup_or_null(cfg->secret);
		s->cfg->token = os_strdup_or_null(cfg->token);
	}
	MemoryContextSwitchTo(oldcxt);
	s->partSize = os_part_size();
	s->h = os_write_handle(url, cfg);
	return s;
}

/*
 * Stage the chunk a part at a time (#1812). The whole chunk used to be
 * appended first, so the buffer held up to one part plus one chunk, and a
 * writer's chunk is a whole Parquet page or Arrow batch: 500 MB staged plus a
 * 600 MB page passed the 1 GiB allocation cap. Now the buffer is filled to the
 * part size, uploaded, and filled again, so it never holds more than one part.
 */
static void
objstore_sink_write(PgColumnarObjSink *s, const void *buf, size_t n)
{
	const char *p = (const char *) buf;

	s->total += (int64) n;
	while (n > 0)
	{
		size_t		room = (size_t) (s->partSize - s->buf.len);
		size_t		take = Min(n, room);
		MemoryContext oldcxt = MemoryContextSwitchTo(TopMemoryContext);

		appendBinaryStringInfo(&s->buf, p, (int) take);
		MemoryContextSwitchTo(oldcxt);
		p += take;
		n -= take;

		if (s->buf.len >= s->partSize)
		{
			if (s->uploadId == NULL)
				os_begin_multipart(s);
			os_flush_part(s);	/* uploads the part and empties the buffer */
		}
	}
}

/*
 * After the commit request succeeded: a cancel or terminate that arrived while
 * its answer was awaited is still pending, and it ends the statement at the
 * next interrupt check. Say first that the object is written, so the error
 * that follows does not read as "nothing was written" (#1813). A committed
 * object cannot be taken back: it may have replaced an earlier one. The detail
 * does not say the object stays: a parallel_export_parquet worker runs this
 * too, and its dispatcher deletes the worker's object when the export fails.
 */
static void
os_sink_report_late_interrupt(PgColumnarObjSink *s)
{
	if (QueryCancelPending || ProcDiePending)
		ereport(WARNING,
				(errmsg("columnar: the export wrote \"%s\" before the interrupt took effect",
						s->h->url),
				 errdetail("The store accepted the object before the cancel or terminate took effect.")));
}

static void
objstore_sink_finish(PgColumnarObjSink *s)
{
	OsResponse	resp;

	/* the commit request: the PUT or the CompleteMultipartUpload (#1813) */
	if (s->uploadId == NULL)
	{
		/* small object: a single PUT is the whole story */
		s->h->holdForAnswer = true;
		os_write_request(s->h, "PUT", "", (uint8 *) s->buf.data, s->buf.len,
						 &resp, NULL);
		s->h->holdForAnswer = false;
		if (resp.status != 200)
			ereport(ERROR,
					(errcode(ERRCODE_IO_ERROR),
					 errmsg("columnar: writing \"%s\" failed (HTTP %d)",
							s->h->url, resp.status)));
	}
	else
	{
		StringInfoData xml;
		char	   *encId = os_uriencode_query(s->uploadId);
		char	   *query = psprintf("uploadId=%s", encId);

		if (s->buf.len > 0)			/* the final (short) part */
			os_flush_part(s);
		initStringInfo(&xml);
		appendStringInfo(&xml, "<CompleteMultipartUpload>%s</CompleteMultipartUpload>",
						 s->completeXml.data);
		s->h->holdForAnswer = true;
		os_write_request(s->h, "POST", query, (uint8 *) xml.data, xml.len,
						 &resp, NULL);
		s->h->holdForAnswer = false;
		pfree(xml.data);
		pfree(query);
		pfree(encId);
		if (resp.status != 200)
			ereport(ERROR,
					(errcode(ERRCODE_IO_ERROR),
					 errmsg("columnar: completing the upload of \"%s\" failed (HTTP %d)",
							s->h->url, resp.status)));
	}
	os_sink_report_late_interrupt(s);
	os_free_handle(s->h);
	s->h = NULL;
	os_sink_free(s);
}

/*
 * How long the abort of an upload whose id never arrived may take, from the
 * listing's start to the last abort (#1810): the same bound the parallel
 * dispatcher uses for its own cleanup (#1743).
 */
#define OS_SINK_CLEANUP_MS	10000

static char **objstore_list_uploads(const char *url, const PgColumnarObjStoreConfig *cfg,
									int timeout_ms, char ***upload_ids, int *nuploads);
static int	objstore_abort_upload(const char *url, const char *upload_id,
								  const PgColumnarObjStoreConfig *cfg, int timeout_ms);

/*
 * The sink sent POST ?uploads but has no UploadId (#1810): a cancel or
 * terminate landed while it waited for the answer, or the answer was lost.
 * The store may hold the upload anyway. List the uploads for this exact key
 * and abort each one. A listing by the key as a prefix also returns longer
 * keys (key.old); those belong to someone else and stay open. An upload that
 * another writer started at this same key cannot be told apart, and is
 * aborted too.
 *
 * This runs for s3:// and gs:// (#1854): the GCS XML API lists multipart
 * uploads the same way. The listing and the aborts use the cfg the sink was
 * created with, so they reach the same endpoint with the same credentials as
 * the upload. Raises on failure; the caller holds interrupts and catches.
 */
static void
os_sink_abort_unanswered(PgColumnarObjSink *s)
{
	const char *url = s->h->url;
	TimestampTz start = GetCurrentTimestamp();
	char	  **keys;
	char	  **ids = NULL;
	int			n = 0;
	int			i;

	if (pg_strncasecmp(url, "s3://", 5) != 0 &&
		pg_strncasecmp(url, "gs://", 5) != 0)
		return;
	keys = objstore_list_uploads(url, s->cfg, OS_SINK_CLEANUP_MS, &ids, &n);
	for (i = 0; i < n; i++)
	{
		long		left;

		/* the listing names keys s3:// or gs://bucket/key; compare past it */
		if (strlen(keys[i]) < 5 || strcmp(keys[i] + 5, url + 5) != 0)
			continue;
		left = OS_SINK_CLEANUP_MS -
			TimestampDifferenceMilliseconds(start, GetCurrentTimestamp());
		if (left <= 0)
			break;
		(void) objstore_abort_upload(url, ids[i], s->cfg, (int) left);
	}
}

static void
objstore_sink_abort(PgColumnarObjSink *s)
{
	MemoryContext cxt;
	volatile uint32 held = 0;
	volatile uint32 cancelHeld = 0;

	if (s == NULL)
		return;
	if (s->h == NULL)			/* finish already ran; just reclaim */
	{
		os_sink_free(s);
		return;
	}
	cxt = CurrentMemoryContext;

	/*
	 * A commit request that raised leaves holdForAnswer set (finish clears it
	 * only on success). The DELETE below is not a commit request.
	 */
	s->h->holdForAnswer = false;
	/* best effort, never raises: an ABORT that itself failed must not mask
	 * the error that triggered it */
	PG_TRY();
	{
		if (s->uploadId != NULL)
		{
			OsResponse	resp;
			char	   *encId = os_uriencode_query(s->uploadId);
			char	   *query = psprintf("uploadId=%s", encId);

			/*
			 * The abort can land mid-request (a cancel or a SIGTERM inside
			 * os_wait), with a part's body still on this connection: the
			 * DELETE would be read as more of that body, or its answer
			 * confused with the part's. A fresh connection makes it a request
			 * of its own (#1722).
			 */
			os_disconnect(s->h);
			os_write_request(s->h, "DELETE", query, NULL, 0, &resp, NULL);
			pfree(query);
			pfree(encId);
		}
		else if (s->createSent)
		{
			/*
			 * Interrupts held, so a second cancel cannot cut the cleanup
			 * short; every wait is bounded by OS_SINK_CLEANUP_MS instead.
			 */
			HOLD_INTERRUPTS();
			held = InterruptHoldoffCount;
			cancelHeld = QueryCancelHoldoffCount;
			os_sink_abort_unanswered(s);
			RESUME_INTERRUPTS();
			held = 0;
		}
	}
	PG_CATCH();
	{
		/*
		 * errfinish zeroed the holdoff counts before the longjmp. Put back
		 * what the branch above held, then balance it.
		 */
		if (held != 0)
		{
			InterruptHoldoffCount = held;
			QueryCancelHoldoffCount = cancelHeld;
			RESUME_INTERRUPTS();
		}
		MemoryContextSwitchTo(cxt);
		FlushErrorState();
	}
	PG_END_TRY();
	os_free_handle(s->h);
	s->h = NULL;
	os_sink_free(s);
}

/* -------------------------------------------------------- ListObjectsV2 (#619)
 *
 * A ListObjectsV2 response is XML from the endpoint. The endpoint is allow-listed
 * (the operator authorized it), but an authorized endpoint can still be hostile,
 * so the response is attacker-influenceable input and the parser wears the
 * columnar_thrift.c discipline: a {buf, len, pos} scan, every bound written as
 * subtract-not-add so a crafted length cannot wrap, and anything unrecognized is
 * end-of-input, never a read past the buffer. It is a targeted extractor for the
 * three elements a listing carries that we use, not a general XML parser.
 */

/* Caps so a hostile endpoint cannot exhaust memory or spin the backend forever. */
#define OS_LIST_MAX_BODY	(16 * 1024 * 1024)
#define OS_LIST_MAX_KEYS	1000000
#define OS_LIST_MAX_PAGES	100000

/*
 * Byte offset just past the next occurrence of `needle` at or after `from` in
 * [buf, buf+len). Returns -1 when absent. The loop bound `i <= len - nlen` is
 * the overflow-safe form (never `i + nlen <= len`, which wraps).
 */
static int64
os_xml_find(const char *buf, int64 len, int64 from, const char *needle)
{
	int64		nlen = (int64) strlen(needle);
	int64		i;

	if (nlen == 0 || from < 0 || nlen > len)
		return -1;
	for (i = from; i <= len - nlen; i++)
		if (memcmp(buf + i, needle, (size_t) nlen) == 0)
			return i + nlen;
	return -1;
}

/*
 * Append the XML-decoded content of [buf+start, buf+end) to `out`, resolving the
 * five predefined entities and single-byte numeric character references. An
 * unrecognized ampersand sequence is copied literally. Every scan is bounded by
 * `end`, and a numeric reference is bounded to a short window.
 */
static void
os_xml_decode_append(StringInfo out, const char *buf, int64 start, int64 end)
{
	int64		i = start;

	while (i < end)
	{
		int64		semi = -1;
		int64		j;

		if (buf[i] != '&')
		{
			appendStringInfoChar(out, buf[i]);
			i++;
			continue;
		}
		for (j = i + 1; j < end && j < i + 12; j++)
			if (buf[j] == ';')
			{
				semi = j;
				break;
			}
		if (semi < 0)
		{
			appendStringInfoChar(out, '&');
			i++;
			continue;
		}
		if (semi - (i + 1) >= 2 && buf[i + 1] == '#')
		{
			char		numbuf[12];
			int64		nlen = semi - (i + 2);
			long		code;

			if (nlen <= 0 || nlen >= (int64) sizeof(numbuf))
			{
				appendStringInfoChar(out, '&');
				i++;
				continue;
			}
			memcpy(numbuf, buf + i + 2, (size_t) nlen);
			numbuf[nlen] = '\0';
			code = (numbuf[0] == 'x' || numbuf[0] == 'X')
				? strtol(numbuf + 1, NULL, 16) : strtol(numbuf, NULL, 10);
			if (code > 0 && code < 128)
			{
				appendStringInfoChar(out, (char) code);
				i = semi + 1;
				continue;
			}
			appendStringInfoChar(out, '&');
			i++;
		}
		else
		{
			int64		nlen = semi - (i + 1);
			const char *e = buf + i + 1;

			if (nlen == 3 && memcmp(e, "amp", 3) == 0)
				appendStringInfoChar(out, '&');
			else if (nlen == 2 && memcmp(e, "lt", 2) == 0)
				appendStringInfoChar(out, '<');
			else if (nlen == 2 && memcmp(e, "gt", 2) == 0)
				appendStringInfoChar(out, '>');
			else if (nlen == 4 && memcmp(e, "quot", 4) == 0)
				appendStringInfoChar(out, '"');
			else if (nlen == 4 && memcmp(e, "apos", 4) == 0)
				appendStringInfoChar(out, '\'');
			else
			{
				appendStringInfoChar(out, '&');
				i++;
				continue;
			}
			i = semi + 1;
		}
	}
}

/* The literal content span of a single <tag>...</tag>, XML-decoded into `out`.
 * Returns true when both tags were found in order. Bounded. */
static bool
os_xml_element(const char *buf, int64 len, const char *open, const char *close,
			   StringInfo out)
{
	int64		s = os_xml_find(buf, len, 0, open);
	int64		e;

	if (s < 0)
		return false;
	e = os_xml_find(buf, len, s, close);
	if (e < 0)
		return false;
	os_xml_decode_append(out, buf, s, e - (int64) strlen(close));
	return true;
}

/*
 * Parse one ListObjectsV2 page: append each <Key> (XML-decoded, as a full
 * s3://bucket/<key> URL) to `keys`, and report truncation and the next token.
 * No delimiter is sent, so there are no CommonPrefixes and every <Key> is an
 * object key.
 */
static void
os_list_parse_page(const char *body, int64 blen, const char *bucket,
				   List **keys, bool *truncated, char **nextToken)
{
	StringInfoData v;
	StringInfoData kbuf;
	int64		pos = 0;

	*truncated = false;
	*nextToken = NULL;

	initStringInfo(&v);
	if (os_xml_element(body, blen, "<IsTruncated>", "</IsTruncated>", &v))
		*truncated = (v.len == 4 && pg_strncasecmp(v.data, "true", 4) == 0);

	resetStringInfo(&v);
	if (os_xml_element(body, blen, "<NextContinuationToken>",
					   "</NextContinuationToken>", &v) && v.len > 0)
		*nextToken = pstrdup(v.data);
	pfree(v.data);

	initStringInfo(&kbuf);
	for (;;)
	{
		int64		ks = os_xml_find(body, blen, pos, "<Key>");
		int64		ke;

		if (ks < 0)
			break;
		ke = os_xml_find(body, blen, ks, "</Key>");
		if (ke < 0)
			break;
		resetStringInfo(&kbuf);
		appendStringInfo(&kbuf, "s3://%s/", bucket);
		os_xml_decode_append(&kbuf, body, ks, ke - (int64) strlen("</Key>"));
		*keys = lappend(*keys, pstrdup(kbuf.data));
		pos = ke;
		if (list_length(*keys) > OS_LIST_MAX_KEYS)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("columnar: object listing exceeded %d keys",
							OS_LIST_MAX_KEYS)));
	}
	pfree(kbuf.data);
}

/* Read the whole response body into a palloc'd NUL-terminated buffer, bounded by
 * cap; handles chunked, content-length and close-delimited framing. */
static char *
os_slurp_body(PgColumnarObjHandle *h, const OsResponse *resp, int64 cap,
			  int64 *outlen)
{
	StringInfoData s;

	initStringInfo(&s);
	if (resp->chunked)
	{
		char		line[64];

		for (;;)
		{
			int64		sz;

			os_read_line(h, line, sizeof(line));
			sz = strtoll(line, NULL, 16);
			if (sz < 0)
				ereport(ERROR,
						(errcode(ERRCODE_PROTOCOL_VIOLATION),
						 errmsg("columnar: bad chunk size from \"%s\"", h->url)));
			if (sz == 0)
			{
				do
				{
					os_read_line(h, line, sizeof(line));
				} while (line[0] != '\0');
				break;
			}
			if ((int64) s.len + sz > cap)
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("columnar: response from \"%s\" exceeded %lld bytes",
								h->url, (long long) cap)));
			enlargeStringInfo(&s, (int) sz);
			os_read_exact(h, (uint8 *) s.data + s.len, sz);
			s.len += (int) sz;
			s.data[s.len] = '\0';
			os_read_line(h, line, sizeof(line));	/* chunk-terminating CRLF */
		}
	}
	else if (resp->content_length < 0)
	{
		/*
		 * Neither chunked nor a length: the body ends when the server closes
		 * the connection (RFC 9112 section 6.3, rule 8). Every request on the
		 * generic HTTP path sends Connection: close, and the Apache Iceberg
		 * REST catalog answers a reply over about 32 KB this way (#1787).
		 * Over plain TCP, os_fill returns false only on an orderly close (FIN)
		 * and raises on a read error, so a reset connection is refused rather
		 * than read as a short body. Over TLS, os_fill also returns false on
		 * EOF without close_notify, so a body cut off that way is accepted as
		 * complete; that is existing behaviour and is not changed here.
		 */
		for (;;)
		{
			int64		avail = h->rb_len - h->rb_off;

			if ((int64) s.len + avail > cap)
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("columnar: response from \"%s\" exceeded %lld bytes",
								h->url, (long long) cap)));
			if (avail > 0)
			{
				enlargeStringInfo(&s, (int) avail);
				s.len += (int) os_take(h, (uint8 *) s.data + s.len, avail);
				s.data[s.len] = '\0';
			}
			if (!os_fill(h))
				break;
		}
	}
	else
	{
		int64		cl = resp->content_length;

		if (cl > cap)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("columnar: response from \"%s\" exceeded %lld bytes",
							h->url, (long long) cap)));
		enlargeStringInfo(&s, (int) cl);
		os_read_exact(h, (uint8 *) s.data, cl);
		s.len = (int) cl;
		s.data[s.len] = '\0';
	}
	if (outlen != NULL)
		*outlen = s.len;
	return s.data;
}

/* Issue a signed GET for `rawQuery` and return the full response body. rawQuery
 * is wire and canonical both, keys already sorted and values already encoded. */
static char *
os_list_request(PgColumnarObjHandle *h, const char *rawQuery, int64 *outlen)
{
	/* SHA-256 of the empty payload, the GET body */
	static const char emptyHash[] =
		"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	OsResponse	resp;
	int			attempt;

	for (attempt = 0;; attempt++)
	{
		StringInfoData req;
		bool		fresh;

		if (h->fd < 0)
			os_connect(h);
		fresh = (attempt == 0 && h->served == 0);

		initStringInfo(&req);
		appendStringInfo(&req, "GET %s%s%s HTTP/1.1\r\nHost: %s:%d\r\n",
						 h->abspath, rawQuery[0] ? "?" : "", rawQuery,
						 h->host, h->port);
		os_sign_write(h, "GET", h->abspath, rawQuery, emptyHash, &req);
		appendStringInfoString(&req, "User-Agent: pgcolumnar-objstore/1\r\n\r\n");

		if (os_send_all(h, req.data, req.len) && os_read_head(h, &resp))
		{
			pfree(req.data);
			break;
		}
		pfree(req.data);
		os_disconnect(h);
		if (!fresh && attempt == 0)
			continue;			/* stale keep-alive: one reconnect */
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: LIST to \"%s\" failed before a response",
						h->url)));
	}

	if (resp.status == 403)
		os_reject_403(h, &resp, true);
	if (resp.status < 200 || resp.status >= 300)
	{
		if (resp.content_length > 0 || resp.chunked)
			os_read_body(h, &resp, NULL, 0);
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: LIST to \"%s\" returned HTTP %d",
						h->url, resp.status)));
	}

	{
		char	   *body = os_slurp_body(h, &resp, OS_LIST_MAX_BODY, outlen);

		/* a close-delimited body leaves the connection at end of stream */
		if (resp.conn_close || (!resp.chunked && resp.content_length < 0))
			os_disconnect(h);
		else
			h->served++;
		return body;
	}
}

/*
 * os_target_bucket_root
 *		Point a handle resolved from a key in `bucket` at the bucket itself, the
 *		target of a bucket-level request such as a LIST. Path-style addressing
 *		names the bucket in the path, "/bucket". Virtual-host addressing has
 *		already put the bucket in the host name (os_resolve_s3 sets auth_host),
 *		so the path is the bucket root, "/": "/bucket" there would name the key
 *		"bucket", and S3 answers that as a GetObject of it (#1783).
 */
static void
os_target_bucket_root(PgColumnarObjHandle *h, const char *bucket)
{
	MemoryContext oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	StringInfoData bp;

	initStringInfo(&bp);
	appendStringInfoChar(&bp, '/');
	if (h->auth_host == NULL)
		os_uriencode_path(&bp, bucket);
	if (h->abspath != NULL)
		pfree(h->abspath);
	h->abspath = bp.data;
	MemoryContextSwitchTo(oldcxt);
}

static char **
objstore_list_objects(const char *url, const PgColumnarObjStoreConfig *cfg,
					  int *nkeys)
{
	const char *bstart;
	const char *slash;
	char	   *bucket;
	char	   *prefix;
	char	   *encPrefix;
	char	   *dummyUrl;
	char	   *token = NULL;
	PgColumnarObjHandle *h;
	List	   *keys = NIL;
	char	  **out;
	ListCell   *lc;
	int			pages = 0;
	int			i;

	if (pg_strncasecmp(url, "s3://", 5) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: listing a prefix is only supported for s3:// URLs, not \"%s\"",
						url)));
	bstart = url + 5;
	slash = strchr(bstart, '/');
	if (slash == NULL || slash == bstart)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" is not s3://bucket/prefix", url)));
	bucket = pnstrdup(bstart, slash - bstart);
	prefix = pstrdup(slash + 1);	/* may be "" (list the whole bucket) */

	/*
	 * Resolve host/region/credentials from a dummy key, then point the request
	 * target at the bucket root and carry the prefix in the signed query. The
	 * handle is named for the prefix being listed, so a refusal, of the
	 * listing or of the resolve, names it and not "s3://bucket/_" (#1781,
	 * #1869).
	 */
	dummyUrl = psprintf("s3://%s/_", bucket);
	h = os_write_handle_as(dummyUrl, url, cfg);
	os_target_bucket_root(h, bucket);
	encPrefix = os_uriencode_query(prefix);

	PG_TRY();
	{
		for (;;)
		{
			StringInfoData q;
			char	   *body;
			int64		blen = 0;
			bool		truncated = false;
			char	   *next = NULL;
			int			before = list_length(keys);

			if (++pages > OS_LIST_MAX_PAGES)
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("columnar: object listing of \"%s\" exceeded %d pages",
								url, OS_LIST_MAX_PAGES)));

			/* canonical query: keys sorted, continuation-token < list-type < prefix */
			initStringInfo(&q);
			if (token != NULL)
			{
				char	   *encTok = os_uriencode_query(token);

				appendStringInfo(&q, "continuation-token=%s&", encTok);
				pfree(encTok);
			}
			appendStringInfo(&q, "list-type=2&prefix=%s", encPrefix);

			body = os_list_request(h, q.data, &blen);
			pfree(q.data);

			os_list_parse_page(body, blen, bucket, &keys, &truncated, &next);
			pfree(body);

			if (!truncated || next == NULL)
			{
				if (next != NULL)
					pfree(next);
				break;
			}
			/*
			 * A truncated page must make progress. ListObjectsV2 returns at least
			 * one key on every truncated page (there is more to come, so this page
			 * was full), so a truncated page that added nothing is a broken or
			 * hostile endpoint. Refusing here bounds the paging loop for ANY token
			 * pattern, including a short cycle of alternating tokens that the
			 * next-token check below cannot catch: a cycle that returns no new
			 * keys is stopped on its first empty page rather than running to the
			 * OS_LIST_MAX_PAGES backstop. The fuzzer (fuzz_listing.sh) finds it.
			 */
			if (list_length(keys) == before)
			{
				pfree(next);
				ereport(ERROR,
						(errcode(ERRCODE_PROTOCOL_VIOLATION),
						 errmsg("columnar: listing of \"%s\" returned a truncated page with no keys",
								url)));
			}
			/*
			 * A continuation token that repeats the one we just sent is the other
			 * non-advancing shape (the endpoint replays a page that does carry
			 * keys); refuse it too rather than accumulate duplicates toward the
			 * OS_LIST_MAX_KEYS backstop.
			 */
			if (token != NULL && strcmp(next, token) == 0)
			{
				pfree(next);
				ereport(ERROR,
						(errcode(ERRCODE_PROTOCOL_VIOLATION),
						 errmsg("columnar: listing of \"%s\" returned a non-advancing continuation token",
								url)));
			}
			if (token != NULL)
				pfree(token);
			token = next;
		}
	}
	PG_CATCH();
	{
		os_free_handle(h);
		PG_RE_THROW();
	}
	PG_END_TRY();
	os_free_handle(h);

	*nkeys = list_length(keys);
	out = (char **) palloc(sizeof(char *) * Max(*nkeys, 1));
	i = 0;
	foreach(lc, keys)
		out[i++] = (char *) lfirst(lc);
	return out;
}

/* --------------------------------------------- ListMultipartUploads (#1743)
 *
 * The response is endpoint XML like a ListObjectsV2 page, read with the same
 * bounded extractor. Each <Upload> element carries a <Key> and an <UploadId>;
 * a truncated page names the next page by NextKeyMarker and
 * NextUploadIdMarker.
 */
static void
os_uploads_parse_page(const char *body, int64 blen, const char *scheme,
					  const char *bucket, List **keys, List **ids,
					  bool *truncated, char **nextKey, char **nextId)
{
	StringInfoData v;
	StringInfoData kbuf;
	int64		pos = 0;

	*truncated = false;
	*nextKey = NULL;
	*nextId = NULL;

	initStringInfo(&v);
	if (os_xml_element(body, blen, "<IsTruncated>", "</IsTruncated>", &v))
		*truncated = (v.len == 4 && pg_strncasecmp(v.data, "true", 4) == 0);
	resetStringInfo(&v);
	if (os_xml_element(body, blen, "<NextKeyMarker>", "</NextKeyMarker>", &v) &&
		v.len > 0)
		*nextKey = pstrdup(v.data);
	resetStringInfo(&v);
	if (os_xml_element(body, blen, "<NextUploadIdMarker>",
					   "</NextUploadIdMarker>", &v))
		*nextId = pstrdup(v.data);

	initStringInfo(&kbuf);
	for (;;)
	{
		int64		us = os_xml_find(body, blen, pos, "<Upload>");
		int64		ue;

		if (us < 0)
			break;
		ue = os_xml_find(body, blen, us, "</Upload>");
		if (ue < 0)
			break;
		resetStringInfo(&kbuf);
		appendStringInfo(&kbuf, "%s://%s/", scheme, bucket);
		resetStringInfo(&v);
		if (os_xml_element(body + us, ue - us, "<Key>", "</Key>", &kbuf) &&
			os_xml_element(body + us, ue - us, "<UploadId>", "</UploadId>", &v) &&
			v.len > 0)
		{
			*keys = lappend(*keys, pstrdup(kbuf.data));
			*ids = lappend(*ids, pstrdup(v.data));
		}
		pos = ue;
		if (list_length(*keys) > OS_LIST_MAX_KEYS)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("columnar: upload listing exceeded %d uploads",
							OS_LIST_MAX_KEYS)));
	}
	pfree(kbuf.data);
	pfree(v.data);
}

static void
os_set_deadline(PgColumnarObjHandle *h, int timeout_ms)
{
	if (timeout_ms > 0)
	{
		h->deadline = TimestampTzPlusMilliseconds(GetCurrentTimestamp(),
												  timeout_ms);
		h->deadline_ms = timeout_ms;
	}
}

static char **
objstore_list_uploads(const char *url, const PgColumnarObjStoreConfig *cfg,
					  int timeout_ms, char ***upload_ids, int *nuploads)
{
	const char *bstart;
	const char *slash;
	char	   *bucket;
	char	   *encPrefix;
	char	   *keyMarker = NULL;
	char	   *idMarker = NULL;
	PgColumnarObjHandle *h;
	List	   *keys = NIL;
	List	   *ids = NIL;
	char	  **outKeys;
	char	  **outIds;
	int			pages = 0;
	int			i;
	const char *scheme;

	/*
	 * s3:// and, through the GCS XML API, gs:// (#1854). Keys come back in
	 * the URL's scheme, lowercase.
	 */
	if (pg_strncasecmp(url, "s3://", 5) == 0)
		scheme = "s3";
	else if (pg_strncasecmp(url, "gs://", 5) == 0)
		scheme = "gs";
	else
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: listing uploads is only supported for s3:// and gs:// URLs, not \"%s\"",
						url)));
	bstart = url + 5;
	slash = strchr(bstart, '/');
	if (slash == NULL || slash == bstart)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" is not %s://bucket/prefix", url, scheme)));
	bucket = pnstrdup(bstart, slash - bstart);
	encPrefix = os_uriencode_query(slash + 1);

	/*
	 * As objstore_list_objects: resolve from a dummy key, request the bucket,
	 * and name the prefix being listed in every message (#1869).
	 */
	h = os_write_handle_as(psprintf("%s://%s/_", scheme, bucket), url, cfg);
	os_set_deadline(h, timeout_ms);
	os_target_bucket_root(h, bucket);

	PG_TRY();
	{
		for (;;)
		{
			StringInfoData q;
			char	   *body;
			int64		blen = 0;
			bool		truncated = false;
			char	   *nextKey = NULL;
			char	   *nextId = NULL;
			int			before = list_length(keys);

			if (++pages > OS_LIST_MAX_PAGES)
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("columnar: upload listing of \"%s\" exceeded %d pages",
								url, OS_LIST_MAX_PAGES)));

			/* canonical query: key-marker < prefix < upload-id-marker < uploads */
			initStringInfo(&q);
			if (keyMarker != NULL)
			{
				char	   *enc = os_uriencode_query(keyMarker);

				appendStringInfo(&q, "key-marker=%s&", enc);
				pfree(enc);
			}
			appendStringInfo(&q, "prefix=%s&", encPrefix);
			if (keyMarker != NULL && idMarker != NULL)
			{
				char	   *enc = os_uriencode_query(idMarker);

				appendStringInfo(&q, "upload-id-marker=%s&", enc);
				pfree(enc);
			}
			appendStringInfoString(&q, "uploads=");

			body = os_list_request(h, q.data, &blen);
			pfree(q.data);
			os_uploads_parse_page(body, blen, scheme, bucket, &keys, &ids,
								  &truncated, &nextKey, &nextId);
			pfree(body);

			if (!truncated || nextKey == NULL)
				break;

			/*
			 * A truncated page must add uploads and move the markers on, or a
			 * broken or hostile endpoint could keep the loop going (the same
			 * two guards as objstore_list_objects).
			 */
			if (list_length(keys) == before ||
				(keyMarker != NULL && strcmp(nextKey, keyMarker) == 0 &&
				 strcmp(nextId ? nextId : "", idMarker ? idMarker : "") == 0))
				ereport(ERROR,
						(errcode(ERRCODE_PROTOCOL_VIOLATION),
						 errmsg("columnar: upload listing of \"%s\" did not advance",
								url)));
			keyMarker = nextKey;
			idMarker = nextId;
		}
	}
	PG_CATCH();
	{
		os_free_handle(h);
		PG_RE_THROW();
	}
	PG_END_TRY();
	os_free_handle(h);

	*nuploads = list_length(keys);
	outKeys = (char **) palloc(sizeof(char *) * Max(*nuploads, 1));
	outIds = (char **) palloc(sizeof(char *) * Max(*nuploads, 1));
	for (i = 0; i < *nuploads; i++)
	{
		outKeys[i] = (char *) list_nth(keys, i);
		outIds[i] = (char *) list_nth(ids, i);
	}
	*upload_ids = outIds;
	return outKeys;
}

static int
objstore_abort_upload(const char *url, const char *upload_id,
					  const PgColumnarObjStoreConfig *cfg, int timeout_ms)
{
	PgColumnarObjHandle *h = os_write_handle(url, cfg);
	OsResponse	resp;
	char	   *encId = os_uriencode_query(upload_id);
	char	   *query = psprintf("uploadId=%s", encId);

	os_set_deadline(h, timeout_ms);
	PG_TRY();
	{
		os_write_request(h, "DELETE", query, NULL, 0, &resp, NULL);
	}
	PG_CATCH();
	{
		os_free_handle(h);
		PG_RE_THROW();
	}
	PG_END_TRY();
	os_free_handle(h);
	pfree(query);
	pfree(encId);
	return resp.status;
}

static void
objstore_delete_object(const char *url, const PgColumnarObjStoreConfig *cfg)
{
	PgColumnarObjHandle *h = os_write_handle(url, cfg);
	OsResponse	resp;

	PG_TRY();
	{
		os_write_request(h, "DELETE", "", NULL, 0, &resp, NULL);
	}
	PG_CATCH();
	{
		FlushErrorState();		/* best effort, like the local unlink */
	}
	PG_END_TRY();
	os_free_handle(h);
}

/*
 * A one-shot HTTP(S) request (#388 phase 7). See columnar_objstore.h. The
 * handle lives in TopMemoryContext and is freed here on the normal path (by the
 * resource-release callback on abort), like every other handle; the response
 * body is returned in the caller's context. No signing: authentication is
 * whatever the caller placed in header_lines.
 */
static PgColumnarHttpResult
objstore_http_request(const char *url, const char *method,
					  const char *const *header_lines, int nheaders,
					  const char *body, int64 body_len, int64 max_response)
{
	PgColumnarObjHandle *h;
	OsResponse	resp;
	PgColumnarHttpResult result;
	StringInfoData req;
	MemoryContext oldcxt;
	bool		isHttps = (pg_strncasecmp(url, "https://", 8) == 0);
	const char *authority;
	const char *slash;
	const char *colon;
	int			i;

	if (!isHttps && pg_strncasecmp(url, "http://", 7) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: HTTP request URL \"%s\" is not http(s)://", url)));
#ifndef HAVE_OBJSTORE_OPENSSL
	if (isHttps)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: https requires the object-store module built with OpenSSL")));
#endif

	/* request-splitting guard: a header line may not carry CR or LF */
	for (i = 0; i < nheaders; i++)
		if (strpbrk(header_lines[i], "\r\n") != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("columnar: an HTTP header line may not contain CR or LF")));

	if (!os_callback_registered)
	{
		RegisterResourceReleaseCallback(os_resource_release, NULL);
		os_callback_registered = true;
	}

	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	h = (PgColumnarObjHandle *) palloc0(sizeof(PgColumnarObjHandle));
	h->fd = -1;
	h->url = pstrdup(url);
	h->rb = (uint8 *) palloc(OS_RBUF);
	MemoryContextSwitchTo(oldcxt);
	dlist_push_head(&os_open_handles, &h->node);	/* before any raise */

	authority = url + (isHttps ? 8 : 7);
	slash = strchr(authority, '/');
	if (slash == NULL || slash == authority)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" has no request path", url)));
	if (memchr(authority, '@', slash - authority) != NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: userinfo in \"%s\" is not supported", url)));

	oldcxt = MemoryContextSwitchTo(TopMemoryContext);
	h->abspath = pstrdup(slash);
	colon = memchr(authority, ':', slash - authority);
	if (colon != NULL)
	{
		h->host = pnstrdup(authority, colon - authority);
		h->port = atoi(colon + 1);
	}
	else
	{
		h->host = pnstrdup(authority, slash - authority);
		h->port = isHttps ? 443 : 80;
	}
	h->tls = isHttps;
	MemoryContextSwitchTo(oldcxt);

	if (h->port <= 0 || h->port > 65535 || h->host[0] == '\0')
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("columnar: \"%s\" has an invalid host or port", url)));

	initStringInfo(&req);
	appendStringInfo(&req, "%s %s HTTP/1.1\r\n", method, h->abspath);
	appendStringInfo(&req, "Host: %s:%d\r\n", h->host, h->port);
	appendStringInfoString(&req, "User-Agent: pgcolumnar-objstore/1\r\n");
	appendStringInfoString(&req, "Connection: close\r\n");
	for (i = 0; i < nheaders; i++)
		appendStringInfo(&req, "%s\r\n", header_lines[i]);
	if (body != NULL && body_len > 0)
		appendStringInfo(&req, "Content-Length: %lld\r\n", (long long) body_len);
	appendStringInfoString(&req, "\r\n");
	if (body != NULL && body_len > 0)
		appendBinaryStringInfo(&req, body, (int) body_len);

	os_connect(h);				/* allow-list + link-local + TLS handshake */
	if (!os_send_all(h, req.data, req.len) || !os_read_head(h, &resp))
	{
		os_disconnect(h);
		ereport(ERROR,
				(errcode(ERRCODE_CONNECTION_FAILURE),
				 errmsg("columnar: connection to \"%s\" failed before a response",
						url)));
	}
	pfree(req.data);

	result.status = resp.status;
	if (strcmp(method, "HEAD") == 0)
	{
		result.body = NULL;
		result.body_len = 0;
	}
	else
		result.body = os_slurp_body(h, &resp, max_response, &result.body_len);

	os_free_handle(h);
	return result;
}

/*
 * The allow-list check with no request (#1780). See columnar_objstore.h. An
 * s3:// or gs:// URL resolves from a dummy key in its bucket, as
 * objstore_list_objects does, so a bare or empty prefix resolves too.
 */
static void
objstore_check_endpoint(const char *url, const PgColumnarObjStoreConfig *cfg)
{
	PgColumnarObjHandle *h;
	char	   *target = (char *) url;

	if (pg_strncasecmp(url, "s3://", 5) == 0 || pg_strncasecmp(url, "gs://", 5) == 0)
	{
		const char *bstart = url + 5;
		const char *slash = strchr(bstart, '/');
		int			blen = (slash != NULL) ? (int) (slash - bstart) : (int) strlen(bstart);

		if (blen == 0)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("columnar: \"%s\" names no bucket", url)));
		target = psprintf("%.5s%.*s/_", url, blen, bstart);
	}
	h = os_write_handle_as(target, url, cfg);	/* messages name `url` (#1869) */
	os_check_endpoint_allowed(h);
	os_free_handle(h);
	if (target != url)
		pfree(target);
}

static const PgColumnarObjStoreApi objstore_api = {
	.abi_version = PGCOLUMNAR_OBJSTORE_ABI,
	.handles_url = objstore_handles_url,
	.open = objstore_open,
	.read = objstore_read,
	.close = objstore_close,
	.sink_create = objstore_sink_create,
	.sink_write = objstore_sink_write,
	.sink_finish = objstore_sink_finish,
	.sink_abort = objstore_sink_abort,
	.delete_object = objstore_delete_object,
	.list_objects = objstore_list_objects,
	.http_request = objstore_http_request,
	.list_uploads = objstore_list_uploads,
	.abort_upload = objstore_abort_upload,
	.check_endpoint = objstore_check_endpoint,
};

const PgColumnarObjStoreApi *
pgcolumnar_objstore_init(void)
{
	return &objstore_api;
}

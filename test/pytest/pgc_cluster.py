"""pgColumnar pytest harness: the cluster a worker owns, and the connection to it.

Design notes that are load-bearing, from design/ISSUE_432_PYTEST_HARNESS.md:

- ONE CLUSTER PER XDIST WORKER, not one shared and not one per test. Two workers
  installing the extension into a single pkglibdir race with each other, and a
  shared cluster lets one test see another's tables. A cluster per test would cost
  an initdb each.
- THE PORT IS DERIVED FROM THE WORKER ID, not picked at random, and sits below the
  ephemeral floor. The bash harness took a random port and then needed a retry,
  because two suites on one box start on the same port and the loser reports a wall
  of errors with no named check failing.
- initdb AND pg_ctl ARE BINARIES, so they are subprocesses. That is the whole
  psql-exception list for this file. Everything after the server is up goes over a
  libpq connection: no psql, no text parsing.
- THE SERVER RUNS AS postgres WHEN WE ARE ROOT. initdb and postgres refuse to run
  as root, so the harness uses runuser, exactly as lib.sh does.
"""

import fcntl
import importlib.util
import hashlib
import os
import re
import pathlib
import shutil
import socket
import signal
import subprocess
import tempfile
import time

# THE OLD CONSTANT HERE WAS 54600, AND BOTH HALVES OF ITS COMMENT WERE FALSE
# (@jdatcmd, #897 review). It claimed to sit below the ephemeral floor and to
# match the bash harness. Measured: /proc/sys/net/ipv4/ip_local_port_range is
# `32768 60999`, so 54600 is INSIDE the ephemeral range, and test/portlib.sh's
# bands are MAIN [10000, 29568) and AUX [29768, 31768) -- nowhere near. The
# kernel handed out 54600, 54602, 54604 and 54606 during a 6000-connection
# probe, 54600 being the master worker's port, while the bash MAIN band took 0
# of 6000. It cost me cluster start failures in this very session before the
# review arrived.
#
# The floor is READ, not assumed, and the band arithmetic is portlib.sh's own so
# the two harnesses cannot drift apart again.
DEFAULT_EPHEMERAL_FLOOR = 32768
MIN_FLOOR = 20000


def ephemeral_floor(range_text):
    """The kernel's lowest ephemeral port, from ip_local_port_range's contents.

    Falls back to the documented default rather than guessing when the file is
    unreadable or malformed, and never returns something so low that the bands
    below it collapse.
    """
    try:
        low = int(str(range_text).split()[0])
    except (ValueError, IndexError, AttributeError):
        return DEFAULT_EPHEMERAL_FLOOR
    return low if low >= MIN_FLOOR else DEFAULT_EPHEMERAL_FLOOR


def read_ephemeral_floor(path="/proc/sys/net/ipv4/ip_local_port_range"):
    try:
        return ephemeral_floor(pathlib.Path(path).read_text())
    except OSError:
        return DEFAULT_EPHEMERAL_FLOOR


def aux_band(floor):
    """test/portlib.sh's AUX band, by its own arithmetic.

        PGC_AUX_PORT_HI = floor - 1000
        PGC_AUX_PORT_LO = AUX_HI - 2000

    AUX rather than MAIN because AUX is for "extra clusters a single suite
    stands up beyond its own", which is what an xdist worker is, and because the
    matrix walks MAIN.
    """
    hi = floor - 1000
    return hi - 2000, hi


def port_is_free(port, host="127.0.0.1"):
    """Bind-test. A band is an argument about probability; a bind is a fact."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


# THE PORT DEPENDS ON THE WORKER AS WELL AS THE SLOT (#1742). A module fixture that
# starts its own server (an S3 fixture, a private cluster) asks for a fixed slot,
# and under `-n` a module's tests are split across workers, so every worker runs
# that fixture. When the start depended on the slot only, two workers probed the
# same free port at the same moment, and the second bind failed with "Address
# already in use" (#1635, #1714, #1721). Each worker now starts in its own block of
# WORKER_STRIDE ports, and a slot is an offset inside that block. Slot 0 is the
# worker's own cluster (make_cluster); fixtures use 1 and up.
#
# 96, not 64 (#1749 review): main's twins already ask for slots 32 to 35 and the
# stack up to 41, and a block of 64 ports holds slots 0 to 31. 96 holds 0 to 47, and
# the AUX band's 2000 ports still give 20 workers, more than any -n here. The guard
# test_every_literal_fixture_slot_fits_a_workers_block keeps the two from drifting.
WORKER_STRIDE = 96


def worker_slot(worker_id=None):
    """The xdist worker's number: 3 for `gw3`, and 0 outside xdist.

    Read from PYTEST_XDIST_WORKER when no id is given, which xdist sets in every
    worker, so a fixture calling pick_port needs no argument to be worker-aware.
    """
    if worker_id is None:
        worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id in (None, "", "master"):
        return 0
    digits = str(worker_id).lstrip("gw")
    return int(digits) if digits.isdigit() else 0


def pick_port(slot, floor=None, is_free=port_is_free, worker_id=None):
    """A free port in the AUX band for this slot of this worker.

    Walks on collision instead of trusting the band, because the band only makes
    a collision unlikely and this harness has already been broken once by a port
    that something else was holding.
    """
    if not 0 <= slot * 2 < WORKER_STRIDE:
        raise ValueError(f"slot {slot} does not fit in a worker's block of "
                         f"{WORKER_STRIDE} ports")
    lo, hi = aux_band(floor if floor is not None else read_ephemeral_floor())
    span = hi - lo
    start = worker_slot(worker_id) * WORKER_STRIDE + slot * 2
    for step in range(span):
        port = lo + ((start + step) % span)
        if is_free(port):
            return port
    raise RuntimeError(
        f"no free port in the AUX band [{lo}, {hi}) for slot {slot} of worker "
        f"{worker_slot(worker_id)}")


class Cluster:
    """A throwaway cluster owned by one xdist worker."""

    def __init__(self, pg_config, worker_id, datadir, port):
        self.pg_config = pg_config
        self.worker_id = worker_id
        self.datadir = datadir
        self.port = port
        self.bindir = _pg_config(pg_config, "--bindir")
        self.version = _pg_config(pg_config, "--version")
        self.libdir = _pg_config(pg_config, "--pkglibdir")
        self.sharedir = _pg_config(pg_config, "--sharedir")
        self._started = False

    def server_binary_verdict(self, so_mtime, postmaster_epoch):
        """fresh | predates | unknown, from two epochs.

        Pure, so it is driven without a server. `predates` means the postmaster
        started BEFORE the library on disk was written, so the backends are
        executing older code than the file -- shared_preload_libraries maps the
        library at start and a reinstall does not reload it.
        """
        try:
            so, pm = float(so_mtime), float(postmaster_epoch)
        except (TypeError, ValueError):
            return "unknown"
        return "predates" if so > pm else "fresh"

    def require_server_loaded_this_binary(self):
        """Refuse if the running server predates the installed library."""
        try:
            so_mtime = os.stat(self.so_path).st_mtime
        except OSError:
            so_mtime = None
        pm = None
        try:
            import psycopg
            with psycopg.connect(self.dsn(), autocommit=True) as conn:
                row = conn.execute(
                    "SELECT extract(epoch from pg_postmaster_start_time())"
                ).fetchone()
                pm = row[0] if row else None
        except Exception:
            pm = None
        verdict = self.server_binary_verdict(so_mtime, pm)
        if verdict == "predates":
            raise RuntimeError(
                f"this server started before {self.so_path} was installed, so its "
                f"backends are running older code than the file on disk. "
                f"shared_preload_libraries maps the library at start; a reinstall "
                f"does not reload it."
            )
        return verdict

    # -- is the installed extension the one this checkout describes? -------
    @property
    def extension_dir(self):
        return os.path.join(self.sharedir, "extension")

    def _read(self, path):
        try:
            return pathlib.Path(path).read_text()
        except OSError:
            return None

    def install_freshness(self):
        """(verdict, source version, installed version) for the prefix in use.

        The harness does not build. It runs against whatever is already in the
        prefix, which is what makes not-rebuilding-per-test possible and is also
        how a corpus ends up measuring another branch's extension.
        """
        srcdir = pathlib.Path(__file__).resolve().parents[2]
        src_ctl = self._read(srcdir / "pgcolumnar.control")
        inst_ctl = self._read(os.path.join(self.extension_dir, "pgcolumnar.control"))
        src_v = control_default_version(src_ctl)
        inst_v = control_default_version(inst_ctl)

        src_sql = self._read(srcdir / f"pgcolumnar--{src_v}.sql") if src_v else None
        inst_sql = (self._read(os.path.join(self.extension_dir,
                                            f"pgcolumnar--{inst_v}.sql"))
                    if inst_v else None)
        return install_verdict(src_v, inst_v, src_sql, inst_sql), src_v, inst_v

    def require_fresh_install(self):
        """Refuse to run against an extension this checkout did not produce.

        Raises rather than skips. A skip here would green the whole corpus, and
        "the extension was from another branch" is the one thing a skip must
        never be allowed to say quietly.

        `unknown` does NOT raise: someone who installed by hand has no readable
        pair to compare, and refusing would break a documented workflow. It says
        which question went unanswered instead of printing nothing.
        """
        verdict, src_v, inst_v = self.install_freshness()
        if verdict == "stale":
            raise RuntimeError(
                "the installed pgcolumnar was not built from this source: "
                f"this checkout declares {src_v!r}, {self.extension_dir} holds "
                f"{inst_v!r}"
                + (" (same version, different base script)"
                   if src_v == inst_v else "")
                + ". Rebuild and reinstall before running the corpus; every "
                  "test below would otherwise report on code this tree does "
                  "not contain."
            )
        if verdict == "unknown":
            print(f"-- install freshness UNVERIFIED (source {src_v!r}, "
                  f"installed {inst_v!r})")
        return verdict

    # -- the dsn every test connects through -------------------------------
    def dsn(self, dbname="postgres"):
        return f"host=127.0.0.1 port={self.port} user=postgres dbname={dbname}"

    @property
    def so_path(self):
        return os.path.join(self.libdir, _SO_NAME)

    def so_md5(self):
        """Fingerprint the library under test.

        lib.sh prints this on every run because a suite once reported a full pass
        against a previously installed library. The Python harness keeps it for the
        same reason.
        """
        return _md5_of(self.so_path)

    # -- lifecycle, the only place a binary is invoked ---------------------
    def initdb(self):
        _asroot(["initdb", "-D", str(self.datadir), "-A", "trust", "-U", "postgres"],
                self.bindir, self.datadir)
        conf = self.datadir / "postgresql.conf"
        with open(conf, "a") as fh:
            fh.write(
                "\n".join(
                    [
                        "",
                        f"port={self.port}",
                        "listen_addresses='127.0.0.1'",
                        # THE SOCKET DIRECTORY, pinned to this cluster's own datadir.
                        #
                        # A PACKAGED POSTGRES DEFAULTS IT SOMEWHERE THIS USER CANNOT WRITE,
                        # and that is why the CI job could not start a cluster at all.
                        # Measured, same box, same major:
                        #
                        #   /usr/lib/postgresql/18  (PGDG, --runstatedir=/run)
                        #       #unix_socket_directories = '/var/run/postgresql'
                        #   /usr/local/pg18a        (source build, no such flag)
                        #       #unix_socket_directories = '/tmp'
                        #
                        # and /var/run/postgresql is drwxrwsr-x postgres postgres. So the
                        # postmaster cannot create its lock file and FATALs, which reaches
                        # the caller as nothing more than `pg_ctl: could not start server`.
                        #
                        # lib.sh does not pin this and does not need to: the suites job runs
                        # under sudo and lib.sh drops to `runuser -u postgres`, which CAN
                        # write that directory. This harness must be non-root throughout --
                        # initdb refuses root -- and is not postgres either, so the default
                        # is wrong for it on any packaged build.
                        #
                        # The datadir rather than /tmp: it already exists, it is already
                        # this cluster's, it goes away with it, and two xdist workers cannot
                        # collide in it.
                        f"unix_socket_directories='{self.datadir}'",
                        "shared_preload_libraries='pgcolumnar'",
                        # Deterministic output so a hash oracle means the same thing
                        # on every machine. lib.sh sets the same three.
                        "extra_float_digits=3",
                        "timezone='UTC'",
                        "lc_messages='C'",
                        # A test that hangs should fail, not wedge the run.
                        "statement_timeout='120s'",
                        "log_min_messages=warning",
                        # ONE PREPARED TRANSACTION PER parallel_copy WORKER, and the
                        # setting cannot be raised without a restart -- so it is set
                        # here, where the postmaster is started, rather than by a test.
                        #
                        # THE DEFAULT IS 0, so it is not a matter of asking for fewer
                        # workers: any number of workers is one too many. `lib.sh` takes
                        # the same setting for the same suite through PGC_EXTRA_CONF, at
                        # the same value.
                        #
                        # It costs a fixed shared-memory array and changes nothing else:
                        # a prepared transaction exists only where something PREPAREs
                        # one, and nothing else in this corpus does. The cluster is
                        # session-scoped, so a per-test alternative would mean restarting
                        # it underneath every other file.
                        "max_prepared_transactions=8",
                        "",
                    ]
                )
            )

    def start(self):
        log = self.datadir / "server.log"
        try:
            _asroot(["pg_ctl", "-D", str(self.datadir), "-l", str(log), "-w", "start"],
                    self.bindir, self.datadir)
        except RuntimeError as e:
            # `pg_ctl` says "Examine the log output." and then nothing examined it, so a
            # cluster that would not start produced fifty identical errors naming the
            # command and not one naming the cause. Measured on a GitHub runner: fifty
            # errors, every one of them `pg_ctl: could not start server`, and the reason
            # was in a file nobody read.
            #
            # lib.sh has had pgc_start_log_report since #537 for exactly this, and the
            # two harnesses are meant to be parallel in FUNCTIONALITY. This is that
            # function's job on this side, written here rather than called across the
            # boundary.
            raise RuntimeError(f"{e}\n{_start_log_report(log)}") from e
        self._started = True

    def stop(self):
        if not self._started:
            return
        _asroot(["pg_ctl", "-D", str(self.datadir), "-m", "immediate", "-w", "stop"],
                self.bindir, self.datadir, check=False)
        self._started = False

    def is_ours(self):
        """Does the server on our port run from OUR datadir?

        lib.sh asks this because `pg_ctl -w` proves only that SOMETHING answers on
        the port. A foreign cluster answering would let every later check run
        against the wrong server.
        """
        import psycopg

        with psycopg.connect(self.dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW data_directory")
                live = pathlib.Path(cur.fetchone()[0]).resolve()
        return live == self.datadir.resolve()


_DEFAULT_VERSION_RE = re.compile(
    r"""^\s*default_version\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)


def control_default_version(text):
    """The default_version a .control file declares, or None.

    None rather than "" for absent, so a caller cannot confuse "no version
    here" with "a version that is the empty string".
    """
    if not text:
        return None
    m = _DEFAULT_VERSION_RE.search(text)
    return m.group(1) if m else None


def install_verdict(src_version, installed_version, src_sql, installed_sql):
    """Did the installed extension come from this source tree?

    A pure function of four strings, so it is driven directly by
    test_install_freshness.py without a cluster -- the same shape test/lib.sh
    uses for its own freshness verdicts.

    Exactly three values, and a caller's `if verdict == "stale"` depends on
    that: "fresh", "stale", "unknown".

    UNKNOWN IS NOT FRESH. If either side is unreadable the honest answer is
    that the question was not answered. Returning "fresh" there would certify
    every run the harness failed to check, which is the failure this guard
    exists to stop.

    The version alone is not enough. A release cycle is long and the base
    install script changes inside it, so two builds can share a version and
    differ. The script is compared as well, which is why the second arm of
    test_same_version_but_a_different_script_is_stale exists.
    """
    if not src_version or not installed_version:
        return "unknown"
    if src_version != installed_version:
        return "stale"
    if not src_sql or not installed_sql:
        return "unknown"
    return "fresh" if src_sql == installed_sql else "stale"


def build_and_install(srcdir, pg_config, major, runner=None):
    """Build and install the extension, or raise.

    Drives `pgc_build_and_install` out of test/lib.sh rather than carrying a
    second implementation. The bash harness has refused to report checks against
    a previously installed library since #536; this harness did not, and reported
    25 passed against source carrying `#error THIS SOURCE IS BROKEN AND CANNOT
    BUILD` (@jdatcmd, #897 review). Two implementations of "is the thing under
    test the thing in this tree" would drift, and that drift would be invisible
    in exactly the way that defect was.

    RAISES rather than skips. A skip greens the corpus, and "the source does not
    compile" is the last thing that may be said quietly.
    """
    srcdir = str(srcdir)
    # THE VALUES GO IN AS ARGUMENTS, NOT INTO THE SCRIPT TEXT (#1339). Spliced into
    # double quotes, a `$` in the path was expanded by bash: a tree under
    # "broken tree $HOME" sourced "broken tree /root/test/lib.sh". As positional
    # parameters nothing in them is ever parsed as shell.
    script = ('. "$1/test/lib.sh" || exit 1; '
              'pgc_build_and_install "$1" "$2" "$3"')
    run = runner or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True))
    proc = run(["bash", "-c", script, "bash", srcdir, str(pg_config), str(major)])
    if proc.returncode != 0:
        raise RuntimeError(
            f"pgcolumnar failed to build or install from {srcdir}; refusing to "
            f"report checks against whatever was installed before.\n"
            f"{(proc.stderr or '').strip()}\n{(proc.stdout or '').strip()}".strip()
        )


# BOTH OF THESE NOW DELEGATE TO test/pgc_fingerprint.py.
#
# They used to be an independent implementation of what test/lib.sh does, and the
# pair produced four defects in one day -- two here, two there, and not one found
# by whoever wrote that copy (#907):
#
#     objstore/*.c never walked            here     found by @linuxhikerpm
#     the bare NAME instead of the path    here     found while fixing the above
#     `xargs -0 cat | md5sum`, no bounds   lib.sh   found by @linuxhikerpm
#     each build dir's Makefile omitted    here     found while writing the twin
#
# The docstring here asserted "the same input set as pgc_source_fingerprint in
# test/lib.sh" through all four. It was false when written and stayed false
# through two rounds of fixing, which is the case against a prose claim of
# agreement: it is not a mechanism, and it is worse than silence because it is
# exactly what stops the next person checking.
#
# Loaded BY PATH rather than by package import: test/ is not a package, and this
# module is imported by pytest from test/pytest/ while lib.sh runs it as a script
# from test/. Neither should have to know about the other's layout.
_FP_PATH = pathlib.Path(__file__).resolve().parent.parent / "pgc_fingerprint.py"
_fp_spec = importlib.util.spec_from_file_location("pgc_fingerprint", _FP_PATH)
_fp = importlib.util.module_from_spec(_fp_spec)
_fp_spec.loader.exec_module(_fp)


def source_build_dirs(srcdir):
    """Every directory the build compiles in: src/, plus any with its own Makefile.

    DERIVED, NOT LISTED. Naming objstore/ would fix today and fail the next time a
    module is added; a directory carrying its own Makefile is what the top-level
    Makefile recurses into, so that is the property to read.
    """
    return _fp.build_dirs(srcdir)


def source_fingerprint(srcdir):
    """A hash of everything a build reads, or None if it could not be computed.

    None rather than "" because that is the contract this harness already had, and
    build_once distinguishes "no fingerprint" from a real one. The module returns
    "" for the same condition; the mapping happens here rather than there so the
    shell and Python callers each get the shape they already expect.
    """
    return _fp.fingerprint(srcdir) or None


def source_manifest(srcdir):
    """Every file the fingerprint hashes, one "relpath digest" per line.

    THE SAME TEXT test/lib.sh's `pgc_source_manifest` prints, because both are the
    same call into test/pgc_fingerprint.py. A caller comparing the two is therefore
    comparing one implementation with itself rather than two that can drift.

    None, not "", when a file could not be read -- the module's contract, and
    deliberate: a digest that FAILED must not look like one that succeeded. lib.sh's
    wrapper substitutes a sentinel line there instead, which is the WRAPPER's
    behaviour and not the module's, so a test of that belongs to the shell harness.

    I wrote this as a join over (relpath, digest) pairs first. `manifest()` returns the
    joined TEXT, and my probe had an `isinstance` fallback that quietly stringified it
    and then reported the two "identical" -- so the wrong assumption read as verified
    until the real call raised.
    """
    return _fp.manifest(srcdir)


# The library's filename, named ONCE. `so_path` and `installed_library` both need
# it, and this module's own comments argue against twin implementations of "is the
# thing under test the thing in this tree" -- that pair produced four defects in one
# day (#907).
_SO_NAME = "pgcolumnar.so"


def _md5_of(path):
    """Digest a FILE with md5sum, the way `so_md5` has always done it.

    Deliberately not `hashlib`: `test_this_module_keeps_no_private_fingerprint`
    forbids a private digest in this module, because the twin source-fingerprint
    implementations produced four defects in one day and the docstring claiming they
    agreed was false through two rounds of fixing (#907). That argument is about a
    SECOND WAY TO COMPUTE ONE THING, which is what a separate artifact digest would
    be, so `so_md5` and `installed_library` share this.

    Truncated to 12 like `so_md5`, so the value written into the build marker is the
    same string the run prints, and a reader can compare them by eye.
    """
    return _run(["md5sum", str(path)]).split()[0][:12]


def installed_library(pg_config):
    """What the prefix holds RIGHT NOW: an md5, "absent", or None if unreadable.

    This is a claim about THIS PREFIX OVER TIME, which is the property actually at
    stake, and deliberately not a claim about the source. The source cannot predict
    the artifact: the build path is compiled in, so one commit built in two
    directories produces two different libraries -- @jdatcmd measured 2c9559d087b0
    and 757591c69d32 from a8702031 with nothing but the directory differing. A
    stored per-source constant would therefore fail open on every legitimate
    rebuild-elsewhere, and this project builds from a fresh directory routinely.

    `None` is NOT "unchanged". The caller writes it into the marker as `unobserved`,
    so a degraded decision is readable rather than inferred from an absence.
    """
    try:
        libdir = _pg_config(pg_config, "--pkglibdir")
    except Exception:
        return None
    try:
        return _md5_of(pathlib.Path(libdir) / _SO_NAME)
    except Exception:
        # The prefix answered and the library could not be digested -- missing, or
        # unreadable. That is an observation, not an absence of one, and it must
        # rebuild rather than certify. Broad on purpose: every way of failing to
        # read the artifact means the same thing here, and the direction is closed.
        return "absent"


def library_change_report(so_path, recorded):
    """-> None when the library at `so_path` still has digest `recorded`, else why.

    On a shared box another session can install into this prefix while a run is
    under way (#1915): measured on PG18, /usr/local/pgsql/lib/pgcolumnar.so was
    replaced at 14:45:10 between one session's install and its test. The session
    records the digest it started with and asks this at the end. An empty
    `recorded` means nothing was recorded, so there is nothing to compare.
    """
    if not recorded:
        return None
    try:
        now = _md5_of(so_path)
    except Exception:
        now = "absent"
    if now == recorded:
        return None
    return (f"the installed library changed during this run: {recorded} at the "
            f"session start, {now} now ({so_path}). Another session installed into "
            f"this prefix, so the tests may have measured its code. Build, install "
            f"and test in one lock hold.")


def require_library_unchanged(so_path, recorded):
    """Raise when the library changed since `recorded`; the session then fails."""
    report = library_change_report(so_path, recorded)
    if report:
        raise RuntimeError(report)


def build_once(srcdir, pg_config, major, lock_path=None, runner=None):
    """build_and_install, but at most once across xdist workers.

    pgc_cluster.py:6 gives the pkglibdir race as the reason this harness does not
    install. That is a reason to serialise the install, not to skip it: the
    workers share one prefix, so one of them builds under a lock and the rest
    wait and then find the marker.

    The marker records the pg_config and major, so a second run against a
    DIFFERENT prefix still builds -- keying it on "did anyone build" alone would
    reintroduce the defect for anyone who runs the corpus twice against two
    majors.
    """
    # A PER-USER DIRECTORY, because a fixed path in /tmp is not a collision, it is a
    # permanent denial. `fs.protected_regular = 2` (default on this kernel) forbids opening
    # a regular file for write in a world-writable STICKY directory when the file's owner is
    # neither the directory's owner nor the caller -- so once one user creates
    # /tmp/pgc-pytest-build.lock, every other user on the box is locked out of the corpus
    # FOREVER, and so is root:
    #
    #     running as: root uid=0
    #     lock: -rw-r--r-- 1 ciuser ciuser 0 /tmp/pgc-pytest-build.lock
    #     PermissionError: [Errno 13] Permission denied
    #
    # Measured after running the corpus as one user and then as another; it cost two runs
    # before I read the sysctl. CAP_DAC_OVERRIDE does not help, which is what makes it
    # surprising.
    #
    # The DIRECTORY carries the uid, not the filename: a per-user directory is owned by that
    # user and is not world-writable, so protected_regular does not apply inside it at all.
    # A per-user FILENAME in /tmp would still be a file in a sticky shared directory.
    #
    # WHAT THIS GIVES UP, said out loud: the lock no longer serialises two DIFFERENT users
    # installing into one shared prefix. That is already covered, and better, by the marker
    # key -- it includes `installed_library(pg_config)` (#956), so another user's install
    # invalidates this user's marker and forces a rebuild rather than being silently
    # accepted. The lock's job is the xdist-worker race within one run, and workers share a
    # uid.
    if lock_path is None:
        lock_dir = os.path.join(tempfile.gettempdir(), f"pgc-pytest-{os.getuid()}")
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        lock_path = os.path.join(lock_dir, "build.lock")
    marker = lock_path + ".done"
    # THE FINGERPRINT IS PART OF THE KEY. Keying on pg_config and major alone
    # would skip the build after a source edit, which is the staleness this
    # whole guard exists to stop -- reintroduced by the optimisation meant to
    # make the guard cheap. A tree we cannot fingerprint gets a key that never
    # matches, so it always rebuilds.
    fp = source_fingerprint(srcdir)

    def key(lib):
        # THE INSTALLED LIBRARY IS PART OF THE KEY (#956). The source fingerprint
        # answers "did this layer last build this source". It was read as "does the
        # prefix hold that build", and those are different claims. The gap cost two
        # debugging sessions in one day: a perf run installed a pre-#945 library
        # into the shared prefix, and the corpus then reported ten failures here and
        # nineteen on @jdatcmd's box with the code entirely innocent. The source had
        # not changed, so the old key matched and the build was skipped.
        #
        # Anything may write this prefix -- the shell harness, a measurement run, a
        # manual install, another worktree -- and stopping them is not the fix. The
        # fix is for this decision to notice.
        return f"{pg_config}\n{major}\n{fp}\n{lib or 'unobserved'}\n"

    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                if fp and pathlib.Path(marker).read_text() == key(
                        installed_library(pg_config)):
                    return "already-built"
            except OSError:
                pass
            build_and_install(srcdir, pg_config, major, runner=runner)
            lib = installed_library(pg_config)
            # AN INSTALL THAT LEAVES NO LIBRARY IS A BROKEN INSTALL (#1339). It used to
            # be written into the marker as "absent" and certified, so the next call
            # skipped the build over a prefix holding nothing. Raised, not skipped, for
            # the reason build_and_install raises.
            if lib == "absent":
                raise RuntimeError(
                    f"pgcolumnar's install from {srcdir} reported success but left no "
                    f"library in {pg_config}'s pkglibdir; refusing to certify it.")
            # AFTER the install, because the install is what writes the library: a
            # fingerprint taken before it would record the previous one and certify
            # exactly the state this guard exists to refuse.
            #
            # AND NO MARKER AT ALL WHEN THE PREFIX COULD NOT BE OBSERVED (#956
            # review, @jdatcmd). Writing `unobserved` made two consecutive
            # unobservable calls match each other and skip -- a fail-open inside a
            # change about a fail-open. It was only reachable with an injected stub
            # runner, because `build_and_install` shells `make PG_CONFIG=<that>` and
            # raises when it fails, but that argument depends on build_and_install
            # staying unable to succeed without a usable pg_config and nothing
            # enforces it. Writing nothing costs one condition and removes the
            # argument: the next call finds no marker and builds.
            if fp and lib is not None:
                pathlib.Path(marker).write_text(key(lib))
            return "built"
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _pg_config(pg_config, flag):
    return _run([pg_config, flag]).strip()


def _run(argv, check=True):
    proc = subprocess.run(argv, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{argv!r} failed rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


# What the server log says about a cluster that would not start.
#
# FATAL lines first, with their line numbers, then a tail, and it SAYS SO when it found
# neither: silence here reads as "there was nothing to say", which was the whole complaint
# in #537. lib.sh's pgc_start_log_report is the same function on the other side; neither
# calls the other, because the harnesses stay independent.
_START_FATAL = re.compile(r"FATAL|PANIC|could not|No space|Permission denied", re.I)


def _start_log_report(log, fatal_lines=5, tail_lines=20):
    try:
        text = pathlib.Path(log).read_text(errors="replace")
    except OSError as e:
        return f"---- server log unreadable at {log}: {e} ----"
    if not text.strip():
        return f"---- server log absent or empty at {log} ----"
    lines = text.splitlines()
    fatal = [f"  {n}: {l}" for n, l in enumerate(lines, 1) if _START_FATAL.search(l)]
    out = []
    if fatal:
        out.append("---- why the cluster would not start ----")
        out.extend(fatal[:fatal_lines])
    else:
        out.append("---- no FATAL in the server log; its tail follows ----")
    out.append(f"---- server log tail ({log}) ----")
    out.extend(f"  {l}" for l in lines[-tail_lines:])
    return "\n".join(out)


def _asroot(argv, bindir, datadir, check=True):
    """Run a server binary, dropping to postgres when we are root.

    initdb, pg_ctl and postgres all refuse to run as root. lib.sh uses runuser for
    the same reason, and the datadir has to be owned by postgres before they run.
    """
    exe = os.path.join(bindir, argv[0])
    cmd = [exe] + argv[1:]
    if os.geteuid() == 0:
        shutil.chown(datadir, user="postgres")
        for path in datadir.rglob("*"):
            shutil.chown(path, user="postgres")
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return _run(cmd, check=check)


def _abandoned_postmaster(datadir):
    """-> the pid of a postmaster still holding `datadir`, or None (#1170).

    `Cluster.stop()` returns immediately when `self._started` is False, which it
    is if the interruption arrived DURING `start()`. The postmaster is then up
    and nothing stops it, so removing the tree leaves a live server whose
    datadir is gone -- a leak with no name tag anywhere on the filesystem, which
    is worse than the one it replaces. @OffgridwithJD measured exactly that:
    `/proc/<pid>/cwd -> /tmp/pgc-pytest-0-oxj_b1xd/data (deleted)`.

    LINE 1 OF postmaster.pid IS NEGATIVE WHEN THE WRITER IS NOT A POSTMASTER --
    initdb's standalone backend writes one too -- so a negative pid is not a
    server and must not be signalled. Their detector found that one the hard way.

    AND THE PID IS CHECKED AGAINST ITS OWN COMMAND LINE, because a pid can be
    recycled between the write and the read. A process is only ours if it is a
    postgres binary whose arguments name this datadir.
    """
    try:
        first = (pathlib.Path(datadir) / "postmaster.pid").read_text().split("\n")[0]
    except OSError:
        return None
    try:
        pid = int(first.strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    try:
        cmdline = pathlib.Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    if not cmdline or not cmdline[0].endswith(b"postgres"):
        return None
    return pid if str(datadir).encode() in b" ".join(cmdline) else None


def _stop_abandoned(datadir):
    """SIGQUIT a postmaster the teardown could not stop, and wait for it.

    SIGQUIT is what `pg_ctl -m immediate` sends, which is the right mode for a
    server nobody is going to talk to again. Returns True when nothing is left
    holding the directory, so the caller can refuse to remove it otherwise.
    """
    pid = _abandoned_postmaster(datadir)
    if pid is None:
        return True
    try:
        os.kill(pid, signal.SIGQUIT)
    except OSError:
        return _abandoned_postmaster(datadir) is None
    for _ in range(100):
        if _abandoned_postmaster(datadir) is None:
            return True
        time.sleep(0.05)
    return False


def make_cluster(pg_config, worker_id):
    """Create and start a cluster for one worker. The caller stops it.

    THIS FUNCTION OWNS THE TREE UNTIL IT SUCCESSFULLY RETURNS. It did not, and
    the failure mode is the one that matters least when things work and most
    when they do not (@linuxhikerpm, #897 review): `root` came from `mkdtemp`
    and then `Cluster()`, `initdb()`, `start()` and `is_ours()` ran with no
    cleanup guard --

        make_cluster_error=RuntimeError
        new_roots=1 leaked=['/tmp/pgc-pytest-777-h3phhtxc']

    -- and conftest.py cannot clean up after it, because the tuple assignment
    `cluster, root = make_cluster(...)` never completes when the call raises.
    The handled is_ours() path leaked too: it stopped the cluster and left the
    directory.

    So every exit that is not a successful return stops whatever was started and
    removes the tree. A partially started cluster is stopped with `-m immediate`
    inside `stop()`; failing to stop it must not mask the original error, so the
    cleanup is itself guarded.
    """
    slot = worker_slot(worker_id)
    port = pick_port(0, worker_id=worker_id)
    root = pathlib.Path(tempfile.mkdtemp(prefix=f"pgc-pytest-{slot}-"))
    cluster = None
    try:
        os.chmod(root, 0o777)
        datadir = root / "data"
        datadir.mkdir()
        cluster = Cluster(pg_config, worker_id, datadir, port)
        cluster.initdb()
        cluster.start()
        if not cluster.is_ours():
            raise RuntimeError(
                f"the server on port {port} is not ours: its data_directory "
                f"differs from {datadir}. Refusing to test against a foreign "
                f"cluster."
            )
        return cluster, root
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt during initdb leaks
        # a datadir and a possibly-running postmaster exactly like an error does.
        if cluster is not None:
            try:
                cluster.stop()
            except Exception:
                pass
        # NEVER REMOVE A DIRECTORY A POSTMASTER STILL HOLDS. `stop()` is a no-op
        # when the interruption arrived during `start()`, and removing the tree
        # then leaves a live server with no datadir to find it by (#1170).
        if _stop_abandoned(root / "data"):
            shutil.rmtree(root, ignore_errors=True)
        raise

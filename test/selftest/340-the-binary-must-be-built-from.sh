# ---- the binary under test must be built from this source, and the server must
# ---- be running it (#432 follow-up, jd)
#
# TWO QUESTIONS, AND THE HARNESS COULD ANSWER NEITHER.
#
# selftest 110 compares the INSTALLED .so against the one built in this tree, so a
# missed install and a foreign overwrite are caught. Nothing derived anything from
# the SOURCE TEXT, so both copies could agree with each other while both were stale
# against edited source. PGC_SKIP_BUILD opens that hole widest, because not
# rebuilding is its entire purpose.
#
# And a cp is not enough. shared_preload_libraries='pgcolumnar' maps the library at
# postmaster start, so `make install` over a running instance changes the file and
# nothing else: every backend keeps executing the code it already mapped. A binary
# can match the source exactly while the server runs something older.
#
# THE CONTROLLER SHAPE. Whoever builds records a fingerprint of the build inputs.
# Every suite in the batch recomputes it and compares, so it is one build per batch
# and one hash per suite rather than a rebuild per suite. Then, once the cluster is
# up, the suite compares the binary's mtime against pg_postmaster_start_time().
#
# The verdict functions are pure, taking their inputs as arguments, for the same
# reason pgc_build_needs_clean is: they can be exercised here without a build.
#
# THE MEASURED COST OF NOT HAVING THIS. A probe run under PGC_SKIP_BUILD=1 asserted
# its fix was "present" by grepping the SOURCE while measuring a .so a different
# worktree had installed. Its control failed, and the failure read as a product
# defect for several minutes.
#
# AND ONE FROM WRITING IT. The first revision wrote the stamp in the skip-build
# branch, so every run recorded the source it was about to compare against and a
# suite measuring an edited tree reported "matches the binary under test". The arm
# below that requires `stale` is what caught it.

check "a fingerprint equal to the record is fresh" \
	"$(pgc_freshness_verdict abc123abc123 abc123abc123)" "fresh"
check "a fingerprint different from the record is stale" \
	"$(pgc_freshness_verdict abc123abc123 def456def456)" "stale"
check "no record at all is unknown, not fresh" \
	"$(pgc_freshness_verdict "" abc123abc123)" "unknown"
check "and an uncomputable current fingerprint is unknown, not stale" \
	"$(pgc_freshness_verdict abc123abc123 "")" "unknown"

check "a server started after the binary is fresh" \
	"$(pgc_running_binary_verdict 1000 2000)" "fresh"
check "a server started at the same second is fresh" \
	"$(pgc_running_binary_verdict 1000 1000)" "fresh"
check "a server older than the binary predates it" \
	"$(pgc_running_binary_verdict 2000 1000)" "predates"
check "a missing binary timestamp is unknown, not predates" \
	"$(pgc_running_binary_verdict "" 1000)" "unknown"
check "a missing postmaster timestamp is unknown, not predates" \
	"$(pgc_running_binary_verdict 1000 "")" "unknown"
check "and a non-numeric timestamp is unknown rather than compared as text" \
	"$(pgc_running_binary_verdict 1000 "not-a-time")" "unknown"

# ---- the stamp must record the BINARY, not only the source (#959) ------------
#
# `pgc_freshness_verdict` compares two SOURCE fingerprints and the caller printed
# "source X matches the binary under test" -- a claim about the BINARY from evidence
# about the SOURCE. When another process writes the shared prefix the claim is false,
# and it is false in the POSITIVE branch, which is the one that asserts something.
#
# MEASURED on two trees whose src/ differs by five files: tree B built and installed
# through the harness, tree A then installed its own library into the same prefix,
# and B ran #945's suite under PGC_SKIP_BUILD=1:
#
#     -- .so: d312a10c0cfb            <- A's build
#     -- source: a0e6afc3e13e matches the binary under test
#     FAIL  plan has runtime coordinator: got [0] want [1]      (nine in all)
#
# The stamp is keyed per SOURCE TREE -- /root/wv3/.pgc_source_stamp.18.<inst> and
# /root/wfpB/.pgc_source_stamp.18.<inst> are two files -- so a tree's stamp records
# only what THAT tree built and is structurally unable to see another tree overwrite
# the prefix. @jdatcmd reproduced the same sentence above two different libraries
# with opposite outcomes on PG 17.
#
# The verdict and the decision are pure, like the two above, so they are exercised
# here without a build.

check "a recorded library digest equal to the installed one is fresh" \
	"$(pgc_binary_identity_verdict aaa111aaa111 aaa111aaa111)" "fresh"
check "a recorded digest different from the installed one is replaced, not fresh" \
	"$(pgc_binary_identity_verdict aaa111aaa111 bbb222bbb222)" "replaced"
check "no recorded digest is unknown, not fresh" \
	"$(pgc_binary_identity_verdict "" aaa111aaa111)" "unknown"
check "and an unreadable installed library is unknown, not replaced" \
	"$(pgc_binary_identity_verdict aaa111aaa111 "")" "unknown"

# THE DECISION, so the caller cannot claim the binary on source evidence alone.
check "source fresh and binary fresh is the only state that earns the claim" \
	"$(pgc_freshness_claim fresh fresh)" "verified"
check "source fresh with an unverifiable binary does not earn it" \
	"$(pgc_freshness_claim fresh unknown)" "source-only"
check "source fresh with a replaced binary is refused" \
	"$(pgc_freshness_claim fresh replaced)" "refuse-binary"
check "a stale source is refused whatever the binary says" \
	"$(pgc_freshness_claim stale fresh)" "refuse-source"
check "and a stale source with a replaced binary is still refused for the source" \
	"$(pgc_freshness_claim stale replaced)" "refuse-source"
check "no source record is unverified, not verified" \
	"$(pgc_freshness_claim unknown fresh)" "unverified"

# THE OLD STAMP FORMAT MUST NOT BE MISREAD. A one-line stamp holds the source
# fingerprint only. Reading a library digest out of it must give nothing, or every
# pre-existing stamp would certify the source hash as a binary digest.
_bs="$PGC_WORKDIR/stamp959"; mkdir -p "$(dirname "$_bs")"
printf '%s\n' "a0e6afc3e13e" > "$_bs"
check "a one-line stamp yields its source fingerprint" \
	"$(pgc_read_source_stamp "$_bs")" "a0e6afc3e13e"
check "and yields NO library digest, rather than reusing the source hash" \
	"$(pgc_read_installed_stamp "$_bs")" ""
pgc_write_source_stamp "$_bs" "a0e6afc3e13e" "d312a10c0cfb"
check "a two-line stamp still yields the source fingerprint first" \
	"$(pgc_read_source_stamp "$_bs")" "a0e6afc3e13e"
check "and yields the library digest second" \
	"$(pgc_read_installed_stamp "$_bs")" "d312a10c0cfb"

# THE MIRROR OF THAT TRAP, in the reader this change did not touch (@jdatcmd swept
# for it). `pgc_read_source_stamp` stripped hex from the WHOLE file, which was right
# while a stamp was one line. With two, a source that cannot be fingerprinted writes
# an empty first line and the whole-file read returns the LIBRARY DIGEST as the
# source: verdict `stale` instead of `unknown`, so a documented UNVERIFIED became a
# FATAL naming a library digest as a source fingerprint. Introduced by the second
# line, so it is fixed here rather than left as a residual.
pgc_write_source_stamp "$_bs" "" "d312a10c0cfb"
check "an unfingerprintable source reads as empty, not as the library digest" \
	"$(pgc_read_source_stamp "$_bs")" ""
check "so its verdict is unknown rather than a spurious stale" \
	"$(pgc_freshness_verdict "$(pgc_read_source_stamp "$_bs")" a0e6afc3e13e)" "unknown"
printf 'abc\nd312a10c0cfb\n' > "$_bs"
check "and a short first line does not splice the second into it" \
	"$(pgc_read_source_stamp "$_bs")" "abc"
unset _bs

# The fingerprint has to MOVE when a build input moves and STAY when nothing does.
# A fingerprint that never changes reports fresh forever, which is the failure this
# whole file exists to prevent, one level down.
_fp_dir="$(mktemp -d)"
mkdir -p "$_fp_dir/src"
printf 'int x;\n' > "$_fp_dir/src/a.c"
printf 'PG_CONFIG = pg_config\n' > "$_fp_dir/Makefile"
_fp_one="$(pgc_source_fingerprint "$_fp_dir")"
check "a fingerprint is 12 hex characters" \
	"$(printf '%s' "$_fp_one" | grep -cE '^[0-9a-f]{12}$')" "1"
check "the same tree fingerprints the same twice" \
	"$(pgc_source_fingerprint "$_fp_dir")" "$_fp_one"
printf 'int x; int y;\n' > "$_fp_dir/src/a.c"
check "editing a source file moves the fingerprint" \
	"$([ "$(pgc_source_fingerprint "$_fp_dir")" != "$_fp_one" ] && echo moved || echo same)" \
	"moved"
_fp_two="$(pgc_source_fingerprint "$_fp_dir")"
printf 'int x;\n' > "$_fp_dir/src/a.c"
check "and restoring it restores the fingerprint" \
	"$(pgc_source_fingerprint "$_fp_dir")" "$_fp_one"
printf 'notes\n' > "$_fp_dir/README-not-a-build-input"
check "a file that is not a build input does not move it" \
	"$(pgc_source_fingerprint "$_fp_dir")" "$_fp_one"
printf 'int z;\n' > "$_fp_dir/src/b.c"
check "adding a source file moves it" \
	"$([ "$(pgc_source_fingerprint "$_fp_dir")" != "$_fp_one" ] && echo moved || echo same)" \
	"moved"
rm -rf "$_fp_dir"

# ---- every directory the build recurses into is in the fingerprint -----------
#
# THE PROPERTY, NOT A LIST. The first fingerprint read $dir/src only, so editing
# objstore/columnar_objstore_module.c -- a SEPARATE shared library the top-level
# Makefile builds and installs by recursion -- left it unchanged, and three
# objstore suites could measure a stale module while the run printed "matches
# the binary under test" (@jdatcmd, #898 review).
#
# Naming objstore/ here would fix today and fail the next time a module is added,
# so this arm reads the recursion out of the Makefile and requires every target
# directory to be covered. If someone adds `$(MAKE) -C $(FOO_DIR)` and FOO_DIR
# has no Makefile of its own, or is otherwise missed, this reddens.
_bd_root="$PGC_SRCDIR"
_bd_covered="$(pgc_source_build_dirs "$_bd_root" | sed "s|^$_bd_root/||" | sort)"

check "PREMISE the fingerprint covers at least src" \
	"$(printf '%s\n' "$_bd_covered" | grep -cx 'src')" "1"

# Every `$(MAKE) -C $(SOMETHING_DIR)` in the top-level Makefile, resolved to the
# directory name that variable ends with.
_bd_missing=""
_bd_seen=0
while IFS= read -r _bd_var; do
	[ -n "$_bd_var" ] || continue
	_bd_val="$(sed -n "s/^[[:space:]]*${_bd_var}[[:space:]]*=[[:space:]]*\(.*\)$/\1/p" \
		"$_bd_root/Makefile" | head -1)"
	_bd_name="${_bd_val##*/}"
	[ -n "$_bd_name" ] || continue
	_bd_seen=$(( _bd_seen + 1 ))
	# grep -c on a here-string, not `printf | grep -qx`; see selftest 080.
	[ "$(grep -cx "$_bd_name" <<<"$_bd_covered" || true)" -ne 0 ] || \
		_bd_missing="$_bd_missing $_bd_name"
done <<EOF
$(grep -oE '\$\(MAKE\) -C \$\([A-Z_]+\)' "$_bd_root/Makefile" \
	| sed -E 's/.*\$\(([A-Z_]+)\)$/\1/' | sort -u)
EOF

# A guard that found nothing to check has not passed, it has abstained. The
# Makefile recurses today; if this ever reads zero the parser broke, not the
# Makefile.
check "PREMISE the Makefile's recursion was actually parsed" \
	"$([ "$_bd_seen" -ge 1 ] && echo yes || echo no)" "yes"
check "every directory the Makefile builds from is in the fingerprint" \
	"$([ -z "$_bd_missing" ] && echo none || echo "missing:$_bd_missing")" "none"

# And the consequence, end to end: a change under a recursed directory must move
# the fingerprint. Against a tree with the REAL tree's SHAPE -- because the defect
# was that the real tree's objstore/ was not being read, and a hand-built fixture
# could not have caught it -- but NOT against the real tree itself.
#
# THIS AROSE FROM WRITING INTO THE LIVE SOURCE TREE, AND IT COST FOUR PULL
# REQUESTS. This arm used to write `objstore/.pgc_fingerprint_probe.c` into
# $PGC_SRCDIR. harness_selftest runs IN the matrix, so at PGC_JOBS=4 it created
# that file in the shared build directory while up to three sibling suites
# fingerprinted concurrently, and whichever sampled inside the window reported
#
#     FATAL: the binary under test was not built from this source
#            source now a735c673b129, binary built from 6d122a7158d5
#
# on a tree that was correct. The path is tree-RELATIVE and the content fixed, so
# the deviant value was IDENTICAL across majors, build directories and branches --
# which is what made it look deterministic enough to be a real staleness, and what
# sent two agents chasing a transient md5sum failure and then a path-spelling
# defect. @linuxhikerpm found it by reading, and it reproduces exactly:
#
#     clean tree                              6d122a7158d5
#     with objstore/.pgc_fingerprint_probe.c  a735c673b129   <- what CI reported
#
# A hardlinked copy costs no data and keeps the structure exact. Writing a NEW
# file into it cannot touch the original, because only existing files are shared.
# Entry by entry, and hardlink-first. `cp -al SRC DST` is NOT safe as a fallback
# pair: /tmp is a different filesystem from the tree here, so the hardlink copy
# failed AFTER creating DST, and `cp -a` then copied the tree INSIDE it -- the
# copy's build dirs came out as `bfix src` instead of `objstore src`. The PREMISE
# below caught that, which is the entire reason it is written as a premise rather
# than assumed. `*` also skips `.git`, which no part of this question needs.
_bd_copy="$(mktemp -d "${TMPDIR:-/tmp}/pgc-bdcopy.XXXXXX")/tree"
mkdir -p "$_bd_copy"
for _bd_entry in "$_bd_root"/*; do
	[ -e "$_bd_entry" ] || continue
	cp -al "$_bd_entry" "$_bd_copy/" 2>/dev/null || cp -a "$_bd_entry" "$_bd_copy/"
done
_bd_probe="$_bd_copy/objstore/.pgc_fingerprint_probe.c"

# THE ARM THAT WOULD HAVE CAUGHT THE ORIGINAL. A suite that runs beside others
# must not write into the tree they are reading.
check "the probe is written outside the live source tree" \
	"$(case "$_bd_probe" in "$_bd_root"/*) echo "INSIDE $_bd_root" ;; *) echo outside ;; esac)" \
	"outside"

# PREMISE: the copy is equivalent for the question being asked. If the copy did
# not carry objstore/, "a new file under objstore moves the fingerprint" would be
# measuring a directory the fingerprint never covered, and would pass for the
# wrong reason.
check "PREMISE the copy discovers the same build directories as the real tree" \
	"$(pgc_source_build_dirs "$_bd_copy" | sed "s|^$_bd_copy/||" | sort | tr '\n' ' ')" \
	"$(pgc_source_build_dirs "$_bd_root" | sed "s|^$_bd_root/||" | sort | tr '\n' ' ')"

_bd_before="$(pgc_source_fingerprint "$_bd_copy")"
printf 'int pgc_fingerprint_probe;\n' > "$_bd_probe"
check "a new source file under objstore moves the fingerprint" \
	"$([ "$(pgc_source_fingerprint "$_bd_copy")" != "$_bd_before" ] && echo moved || echo same)" \
	"moved"
rm -f "$_bd_probe"
check "and removing it restores the fingerprint" \
	"$(pgc_source_fingerprint "$_bd_copy")" "$_bd_before"
rm -rf "${_bd_copy%/tree}"

# ---- the postmaster arm must be able to FIRE, not just to compute -----------
#
# The arms above feed pgc_running_binary_verdict fixture values and prove its
# arithmetic. They do not prove anything calls it. Every suite initdb's a fresh
# cluster and starts it AFTER the install, so through any shipped path the
# postmaster is always newer than the .so: `predates` is unreachable, and the
# call site could have been deleted with every check still passing
# (@jdatcmd, #898 review).
#
# These arms drive the real call site against the REAL running cluster. The stat,
# the pg_postmaster_start_time() query, the verdict and the refusal all execute;
# only the path is redirected, so nothing has to touch the installed library to
# prove the guard fires.
_rb_dir="$(mktemp -d)"
_rb_so="$_rb_dir/pgcolumnar.so"

# A file written now is newer than a postmaster that started earlier: predates.
: > "$_rb_so"
touch -d "+1 hour" "$_rb_so"
_rb_out="$(pgc_check_running_binary "$_rb_so" 2>&1)"; _rb_rc=$?
check "a library newer than the running server is REFUSED" "$_rb_rc" "1"
check "and the refusal says the server must be restarted" \
	"$(printf '%s\n' "$_rb_out" | grep -c 'Restart the cluster')" "1"

# The control: same call, same server, a library older than the postmaster.
touch -d "-1 hour" "$_rb_so"
_rb_out2="$(pgc_check_running_binary "$_rb_so" 2>&1)"; _rb_rc2=$?
check "a library older than the running server is accepted" "$_rb_rc2" "0"
check "and it says so rather than staying silent" \
	"$(printf '%s\n' "$_rb_out2" | grep -c 'started after the binary was installed')" "1"

# A path that does not exist must read unknown, not fresh and not a crash.
rm -f "$_rb_so"
_rb_out3="$(pgc_check_running_binary "$_rb_so" 2>&1)"; _rb_rc3=$?
check "an unreadable library is not a failure" "$_rb_rc3" "0"
check "but it says which question went unanswered" \
	"$(printf '%s\n' "$_rb_out3" | grep -c 'could not compare')" "1"
rm -rf "$_rb_dir"

# ---- three false-freshness paths @linuxhikerpm reproduced (#898 review) ------
#
# All three are the same failure this file exists to prevent: the run reports
# FRESH while the binary is stale. Each was reproduced against the branch before
# it was fixed, and each arm below drives the REAL function rather than a copy.

# 1. THE WRITER COULD NOT REPORT FAILURE. `|| true` on the redirect meant both
#    controllers' `if (...)` warning branches were unreachable -- and both carry
#    a comment saying "NOT || true", so the comments argued for a guarantee the
#    code did not provide. Measured: write_rc=0 exists=no.
_fs_bad="/proc/pgc-source-stamp-selftest"   # a path that cannot be created
pgc_write_source_stamp "$_fs_bad" "deadbeef" 2>/dev/null
check "the stamp writer reports failure on an unwritable target" \
	"$?" "1"

check "premise: and the stamp really was not written, so the arm is not vacuous" \
	"$([ -f "$_fs_bad" ] && echo written || echo absent)" "absent"

_fs_ok="$PGC_WORKDIR/stamp.ok"
pgc_write_source_stamp "$_fs_ok" "cafebabe"
check "control: and it still succeeds on a writable one" "$?" "0"

check "control: writing the value it was given" "$(cat "$_fs_ok" 2>/dev/null)" "cafebabe"

# 2. THE DIGEST COULD NOT SEE A REPARTITION. `xargs -0 cat | md5sum` hashed the
#    concatenated stream, so moving bytes BETWEEN files left the hash unchanged
#    while the source stopped compiling:
#        before_hash=bfce474cc159 after_hash=bfce474cc159
#        initial_compile=0 repartitioned_compile=1  error: redefinition of 'x'
_fs_rp="$PGC_WORKDIR/repartition"; rm -rf "$_fs_rp"; mkdir -p "$_fs_rp/src"
printf 'all:\n\ttrue\n' > "$_fs_rp/Makefile"
printf 'static int x=1;\n' > "$_fs_rp/src/a.c"
printf 'static int x=2;\n' > "$_fs_rp/src/b.c"
_fs_before="$(pgc_source_fingerprint "$_fs_rp")"

check "premise: the partition fixture fingerprints at all" \
	"$([ -n "$_fs_before" ] && echo yes || echo no)" "yes"

# The same bytes, a different partition: a.c gains b.c's line and b.c is emptied.
printf 'static int x=1;\nstatic int x=2;\n' > "$_fs_rp/src/a.c"
: > "$_fs_rp/src/b.c"
check "moving bytes between files moves the fingerprint" \
	"$([ "$(pgc_source_fingerprint "$_fs_rp")" != "$_fs_before" ] && echo moved || echo SAME)" \
	"moved"

# And back, so the arm above is about the partition rather than about any edit.
printf 'static int x=1;\n' > "$_fs_rp/src/a.c"
printf 'static int x=2;\n' > "$_fs_rp/src/b.c"
check "and restoring the partition restores the fingerprint" \
	"$(pgc_source_fingerprint "$_fs_rp")" "$_fs_before"

# Renaming a file is also a repartition: same bytes, different translation unit.
mv "$_fs_rp/src/b.c" "$_fs_rp/src/c.c"
check "renaming a source file moves the fingerprint too" \
	"$([ "$(pgc_source_fingerprint "$_fs_rp")" != "$_fs_before" ] && echo moved || echo SAME)" \
	"moved"

# 3. "KEYED BY MAJOR" ALIASED DISTINCT INSTALLATIONS. Two PG18 prefixes with
#    different pkglibdirs both resolved to `.pgc_source_stamp.18`, so a build
#    into one certified the other. This box really has pg18a, pg18n and
#    pg18_san -- but the arm uses FAKE pg_configs so it does not depend on which
#    majors happen to be installed here.
_fs_cfg="$PGC_WORKDIR/cfgs"; rm -rf "$_fs_cfg"; mkdir -p "$_fs_cfg"
for _fs_n in a b; do
	{
		printf '#!/bin/sh\n'
		printf 'case "$1" in\n'
		printf '  --version) echo "PostgreSQL 18.4" ;;\n'
		printf '  --pkglibdir) echo "/usr/local/pg18%s/lib/postgresql" ;;\n' "$_fs_n"
		printf 'esac\n'
	} > "$_fs_cfg/pg_config.$_fs_n"
	chmod 755 "$_fs_cfg/pg_config.$_fs_n"
done

_fs_pa="$(pgc_source_stamp_path /tree "$_fs_cfg/pg_config.a")"
_fs_pb="$(pgc_source_stamp_path /tree "$_fs_cfg/pg_config.b")"

check "premise: both fake configs report the same major, which is the whole point" \
	"$(pgc_major_of "$_fs_cfg/pg_config.a")=$(pgc_major_of "$_fs_cfg/pg_config.b")" "18=18"

check "two installations of one major get different stamp paths" \
	"$([ "$_fs_pa" != "$_fs_pb" ] && echo different || echo "SAME:$_fs_pa")" "different"

check "and the major is still readable in the name" \
	"$(printf '%s' "$_fs_pa" | grep -c '\.pgc_source_stamp\.18\.')" "1"

# The same installation must still resolve to ONE stamp, or every run rebuilds
# and the check never has a recorded value to compare against.
check "control: the same pg_config twice gives the same path" \
	"$(pgc_source_stamp_path /tree "$_fs_cfg/pg_config.a")" "$_fs_pa"

# Two pg_configs pointing at ONE prefix are the same installation and share a
# stamp: the key is pkglibdir, not the pg_config path.
cp "$_fs_cfg/pg_config.a" "$_fs_cfg/pg_config.a2"
check "and two pg_configs for one prefix share a stamp, keyed on pkglibdir" \
	"$(pgc_source_stamp_path /tree "$_fs_cfg/pg_config.a2")" "$_fs_pa"

# An unreadable pg_config must not land every broken config on one key, which is
# the aliasing defect one level down.
printf '#!/bin/sh\nexit 1\n' > "$_fs_cfg/pg_config.broken"; chmod 755 "$_fs_cfg/pg_config.broken"
printf '#!/bin/sh\nexit 1\n' > "$_fs_cfg/pg_config.broken2"; chmod 755 "$_fs_cfg/pg_config.broken2"
# AND NOT ONE TIME IN 4096 (#1478). The check above uses a random directory, so a
# key cut too short aliases the two only now and then: three hex characters failed
# it once in about 4096 runs. These two paths collide on their first three md5 hex
# characters (235) and differ at eight, so a three-character key aliases them on
# every run. They need not exist: the key hashes the path string, and a pg_config
# that cannot run is exactly the unreadable case.
check "two unreadable pg_configs whose short hashes collide still get different stamps" \
	"$([ "$(pgc_source_stamp_path /tree /tmp/pgc-collide-870/pg_config.broken)" \
	    != "$(pgc_source_stamp_path /tree /tmp/pgc-collide-870/pg_config.broken2)" ] \
	    && echo different || echo SAME)" "different"
check "two unreadable pg_configs do not alias onto one stamp" \
	"$([ "$(pgc_source_stamp_path /tree "$_fs_cfg/pg_config.broken")" \
	    != "$(pgc_source_stamp_path /tree "$_fs_cfg/pg_config.broken2")" ] \
	    && echo different || echo SAME)" "different"

unset _fs_bad _fs_ok _fs_rp _fs_before _fs_cfg _fs_n _fs_pa _fs_pb

# ---- every caller must pass a pg_config, not a major ------------------------
#
# WHY THIS EXISTS, AND WHY IT IS A SWEEP RATHER THAN AN ARM ABOUT ONE LINE.
# Changing pgc_source_stamp_path from `DIR MAJOR` to `DIR PG_CONFIG` changes a
# CONTRACT, and a contract change is only as good as the sweep that finds every
# caller. @jdatcmd merged the two branches and found the case this misses
# (#903 review): #897 adds a NEW writer inside pgc_build_and_install, and
# `test/lib.sh` conflicts in pgc_setup while that new call site merges CLEANLY
# and is then wrong. The resolution you are shown is not the defect.
#
# What the mismatch costs, measured by driving the real function both ways:
#
#     correct (PG_CONFIG):     /t/.pgc_source_stamp.18.603d6145
#     a major passed instead:  /t/.pgc_source_stamp.0.nolib6f4922f4
#
# `pgc_major_of 18` finds no version in the string "18", so the major becomes 0
# and the id becomes a hash of the literal "18". The writer and the reader then
# address DIFFERENT FILES and never meet, so every suite reports
# "freshness UNVERIFIED" -- and unknown is deliberately not a failure, so
# NOTHING SAYS SO. Worse for the case this file exists for: a key derived from
# the literal "18" is the same for every PG18 prefix, so pg18a, pg18n and
# pg18_san collide again on the writer path.
#
# So this arm is about the CLASS. It reddens for any caller, in any file, that
# passes a major where a pg_config belongs -- including one that arrives through
# a clean merge in a hunk nobody was asked to resolve.

_sc_sites="$(grep -rn 'pgc_source_stamp_path ' "$PGC_TESTDIR"/*.sh "$PGC_TESTDIR"/selftest/*.sh 2>/dev/null \
	| grep -v 'pgc_source_stamp_path() {')"

# A sweep that found nothing reports "no bad callers" and looks exactly like a
# sweep that works.
check "premise: the sweep finds the call sites it is meant to police" \
	"$([ "$(printf '%s\n' "$_sc_sites" | grep -c .)" -ge 3 ] && echo enough \
		|| printf '%s\n' "$_sc_sites" | grep -c .)" "enough"

# The second argument, per call site. A major-shaped one is the defect: a bare
# integer, or a variable whose name ends in _major, or PGC_MAJOR.
_sc_bad=""; _sc_n=0
while IFS= read -r _sc_l; do
	[ -n "$_sc_l" ] || continue
	_sc_arg="$(printf '%s' "$_sc_l" | sed -n 's/.*pgc_source_stamp_path "[^"]*" "\([^"]*\)".*/\1/p')"
	[ -n "$_sc_arg" ] || continue
	case "$_sc_arg" in
		'$PGC_MAJOR' | *_major'}' | *_major | [0-9] | [0-9][0-9])
			_sc_n=$((_sc_n + 1))
			[ "$_sc_n" -le 4 ] && _sc_bad="$_sc_bad ${_sc_l%%:*}:$(printf '%s' "$_sc_l" | cut -d: -f2)"
			;;
	esac
done <<EOF
$_sc_sites
EOF

check "no caller passes a major where a pg_config belongs" \
	"$([ "$_sc_n" -eq 0 ] && echo none || echo "[$_sc_n:$_sc_bad]")" "none"

# And the sweep must be able to SEE a bad caller, or "none" means nothing. The
# fixture is the exact line #897 adds, so this arm is the one that would have
# caught the merge @jdatcmd found.
_sc_fix="$PGC_WORKDIR/contract"; rm -rf "$_sc_fix"; mkdir -p "$_sc_fix"
# ASSEMBLED, NOT WRITTEN OUT. The sweep above greps the tree for exactly this
# shape, so a fixture that spells the forbidden line literally IS a bad caller
# as far as the sweep is concerned -- and it found this one, at this line, on
# the first run. selftest 320 records the same lesson: a test for a pattern must
# not contain the pattern.
_sc_bad_arg="\$_pgc_bi""_major"
printf '\t\t"$(pgc_source_stamp_path "$_pgc_bi_src" "%s")" \\\n' "$_sc_bad_arg" \
	> "$_sc_fix/bad.sh"
_sc_probe="$(sed -n 's/.*pgc_source_stamp_path "[^"]*" "\([^"]*\)".*/\1/p' "$_sc_fix/bad.sh")"
check "premise: the argument parser reads the second argument at all" \
	"$_sc_probe" '$_pgc_bi_major'

check "a caller passing a major is caught" \
	"$(case "$_sc_probe" in '$PGC_MAJOR' | *_major'}' | *_major | [0-9] | [0-9][0-9]) echo caught ;; *) echo MISSED ;; esac)" \
	"caught"

# The mirror: a correct caller must NOT be flagged, or the arm reddens on every
# healthy tree and gets switched off.
printf '\t\t"$(pgc_source_stamp_path "$_pgc_bi_src" "$_pgc_bi_cfg")" \\\n' \
	> "$_sc_fix/good.sh"
_sc_probe2="$(sed -n 's/.*pgc_source_stamp_path "[^"]*" "\([^"]*\)".*/\1/p' "$_sc_fix/good.sh")"
check "control: a caller passing a pg_config is not flagged" \
	"$(case "$_sc_probe2" in '$PGC_MAJOR' | *_major'}' | *_major | [0-9] | [0-9][0-9]) echo FLAGGED ;; *) echo clean ;; esac)" \
	"clean"

unset _sc_sites _sc_bad _sc_n _sc_l _sc_arg _sc_fix _sc_probe _sc_probe2

# ---- the writer and the reader must address the SAME file -------------------
#
# THE CLASS, NOT THE INSTANCE (@jdatcmd, #903 review). The arm above sweeps for
# a major passed where a pg_config belongs, which catches the shape. This one
# catches the CONSEQUENCE regardless of shape: it drives the real
# pgc_build_and_install and then asks the real reader for the value, so ANY
# disagreement between the two about which file the stamp lives in reddens here
# -- a renamed variable, a reordered argument, a third caller nobody swept.
#
# It is the property that actually matters. The writer and the reader agreeing
# on a path is what makes the freshness check a check; when they disagree the
# reader finds nothing, the verdict is `unknown`, and unknown is DELIBERATELY
# not a failure -- so the whole mechanism goes quiet and every suite prints
# "freshness UNVERIFIED" while looking healthy. Nothing else in this file
# asserts it.
#
# `make` is stubbed on PATH so this costs nothing: the subject is which path the
# stamp lands on, not whether the tree compiles.

_wr="$PGC_WORKDIR/writer_reader"; rm -rf "$_wr"; mkdir -p "$_wr/src" "$_wr/bin"
printf 'int x;\n' > "$_wr/src/a.c"
printf 'all:\n\ttrue\n' > "$_wr/Makefile"
printf '#!/bin/sh\nexit 0\n' > "$_wr/bin/make"; chmod 755 "$_wr/bin/make"
{
	printf '#!/bin/sh\n'
	printf 'case "$1" in\n'
	printf '  --version) echo "PostgreSQL 18.4" ;;\n'
	printf '  --pkglibdir) echo "%s/lib" ;;\n' "$_wr"
	printf 'esac\n'
} > "$_wr/pg_config"; chmod 755 "$_wr/pg_config"

# Drive the REAL function, exactly as pgc_setup calls it.
( PATH="$_wr/bin:$PATH"; pgc_build_and_install "$_wr" "$_wr/pg_config" 18 ) >/dev/null 2>&1
_wr_rc=$?

check "premise: the build path ran to completion, so a stamp was due" "$_wr_rc" "0"

# What landed, and what the reader will look for. Globbed rather than computed,
# so this reads the writer's ACTUAL choice instead of re-deriving it.
_wr_written=""
for _wr_f in "$_wr"/.pgc_source_stamp.*; do
	[ -e "$_wr_f" ] && _wr_written="$_wr_f"
done
_wr_expected="$(pgc_source_stamp_path "$_wr" "$_wr/pg_config")"

check "premise: the writer wrote a stamp at all" \
	"$([ -n "$_wr_written" ] && echo yes || echo "none in $_wr")" "yes"

check "the writer writes the file the reader looks for" \
	"$_wr_written" "$_wr_expected"

# And end to end: the reader gets the fingerprint back, so the verdict is
# `fresh` rather than `unknown`.
check "and the reader reads back the fingerprint the writer recorded" \
	"$(pgc_read_source_stamp "$_wr_expected")" "$(pgc_source_fingerprint "$_wr")"

check "so the verdict is fresh, not unknown" \
	"$(pgc_freshness_verdict "$(pgc_read_source_stamp "$_wr_expected")" \
		"$(pgc_source_fingerprint "$_wr")")" "fresh"

unset _wr _wr_rc _wr_written _wr_expected _wr_f

# ---------------------------------------------------------------------------
# A FINGERPRINT THAT COULD NOT BE COMPUTED MUST NOT LOOK LIKE ONE THAT WAS.
#
# `$(md5sum < "$f" 2>/dev/null | cut -d' ' -f1)` substitutes an EMPTY digest
# when md5sum fails, so one transient failure -- a fork that hits EAGAIN, an
# OOM kill, a loaded runner -- silently changes the whole hash and the function
# still returns 0. Modelled with a stub md5sum that fails on its Nth call and is
# otherwise the real one, three different confident answers over ONE unchanged
# tree:
#
#     baseline                   c8e6b23db1c9
#     one digest empty (call 2)  22897add806e
#     one digest empty (call 3)  58c76fdab962
#     exit status                0
#
# On #902's PG18 leg a suite reported FATAL "the binary under test was not built
# from this source" against a tree that was correct, and the stamp it disagreed
# with equalled a clean local fingerprint of the same head. Whatever moved, the
# WRITE was right and one READ was not.
#
# The requirement is not that the computation cannot fail. It is that a failure
# is reported as one: EMPTY, which pgc_freshness_verdict already turns into
# `unknown` and the controller already prints as "freshness UNVERIFIED" and
# deliberately does not fail. A false UNVERIFIED costs a line of output. A false
# FATAL costs a red matrix and teaches people to re-run past the check.

# THE MECHANISM HAD TO CHANGE WHEN THE IMPLEMENTATION DID (#907).
#
# This block used to stub `md5sum` on PATH, because the shell forked it once per
# file. The digest is now hashlib inside test/pgc_fingerprint.py, which no PATH
# can reach -- so the stub would have left every arm below GREEN while testing
# nothing at all, which is the precise failure this file exists to refuse.
#
# A real read failure needs a real reader who is denied, and root is denied
# nothing: chmod 000 is invisible to it. Measured before this was written:
#
#     as root      28a7149e07ae   <- reads the mode-000 file regardless
#     as postgres  (empty)        <- the failure these arms need
#
# The tree therefore lives outside any 0700 directory and is read by a second
# user. Where no such user exists the arms SKIP loudly rather than pass quietly:
# a guard that cannot run is not a guard that held.
_fp="$(mktemp -d "${TMPDIR:-/tmp}/pgc-fpfail.XXXXXX")"
mkdir -p "$_fp/tree/src"
printf 'int a;\n' > "$_fp/tree/src/a.c"
printf 'int b;\n' > "$_fp/tree/src/b.c"
printf 'int c;\n' > "$_fp/tree/src/c.c"
printf 'all:\n\ttrue\n' > "$_fp/tree/Makefile"
printf 'x\n' > "$_fp/tree/pgcolumnar.control"
chmod -R a+rX "$_fp"

# A READABLE COPY OF THE HARNESS, because the reader may not be able to reach the
# checkout. The arms below source `lib.sh` as a second user, and in GitHub Actions
# the tree lives under `/home/runner/work`, which `postgres` and `nobody` cannot
# traverse. The nightly coverage job went red for two nights on exactly that, with
# six `lib.sh: Permission denied` lines in its log, and the premise checks below
# are what caught it.
#
# Copying is not a weaker test: it is the same three files, and which directory
# they are read from is an environment property rather than anything this part
# asserts. `_pgc_fp_module` resolves `pgc_fingerprint.py` beside `lib.sh` through
# BASH_SOURCE, and `lib.sh` sources `portlib.sh` the same way, so all three must
# sit together.
_fp_harness="$_fp/harness"
mkdir -p "$_fp_harness"
cp "$PGC_TESTDIR/lib.sh" "$PGC_TESTDIR/portlib.sh" "$PGC_TESTDIR/pgc_fingerprint.py" \
	"$_fp_harness/" 2>/dev/null
chmod -R a+rX "$_fp"

# INITIALISED BEFORE BLOCK 1, because the arms block below reads it in an `elif`
# and `_fp_base` is assigned only inside block 1's `else`. Under `set -uo pipefail`
# an unassigned read kills the suite, which is why the branch below used to be a
# bare `:` -- and that `:` is what silently dropped five arms (#994 review).
_fp_base=""
_fp_user=""
if [ "$(id -u)" -ne 0 ]; then
	_fp_user="-"			# already unprivileged; read in this shell
else
	for _fp_u in postgres nobody; do
		id -u "$_fp_u" >/dev/null 2>&1 && { _fp_user="$_fp_u"; break; }
	done
fi

# _fp_as reads as the unprivileged user, or in this shell when already one.
#
# IT ANSWERS `harness-unreadable` RATHER THAN NOTHING when the source fails, and
# that is the whole defect this replaces. The first version did `|| exit 1`, so a
# reader that could not source `lib.sh` produced empty output -- which is exactly
# what the arms below want. Measured under a tree the reader cannot traverse:
#
#     everything readable, lib.sh UNREACHABLE   -> []     <- the ARMS PASS on this
#     b.c unreadable,      lib.sh reachable     -> []     <- what they mean to test
#     everything readable, lib.sh reachable     -> [cde49bff94a7]
#
# Two arms reported PASS having proved nothing. A sentinel cannot be mistaken for
# a hash or for empty, so no arm can ever pass that way again.
_fp_as() {	# _fp_as EXPR -> stdout, or `harness-unreadable`
	local _fp_cmd
	# `echo`, not `printf`: a `\n` inside this concatenation is correct, and the
	# static checker cannot see through the concatenation to know that (SC1012). A
	# warning that has to be explained is worse than a form needing no explanation.
	# And this comment does not open with that checker's name, because a comment
	# that does is parsed as a DIRECTIVE -- the first wording here turned into
	# SC1072/SC1073 parse errors on the comment itself.
	_fp_cmd=". \"$_fp_harness/lib.sh\" 2>/dev/null || { echo harness-unreadable; exit 0; }; $1"
	if [ "$_fp_user" = "-" ]; then
		bash -c "$_fp_cmd"
	else
		runuser -u "$_fp_user" -- bash -c "$_fp_cmd"
	fi
}

if [ -z "$_fp_user" ]; then
	# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994). A single skip named
	# "the unreadable-source refusal" -- a name no arm below has -- leaves the three
	# names with no record at all, so a reader cannot tell WHICH arms did not run and
	# the ledger cannot tell a skipped arm from a deleted one. That is the same shape
	# the loop 30 lines below already avoids.
	for _fp_n in "premise: the unprivileged reader can source the staged harness" \
			"premise: the tree fingerprints to something when it is readable" \
			"premise: the unprivileged read agrees while everything is readable"; do
		check_skip "$_fp_n" \
			"SKIP  $_fp_n (no non-root user to read as; root ignores chmod 000)" \
			"no non-root user to read as"
	done
else
	# Premise nought: the reader can source the copy at all. Stated separately
	# from the fingerprint premise so a reachability failure and a fingerprint
	# failure cannot be read as each other.
	check "premise: the unprivileged reader can source the staged harness" \
		"$(_fp_as "printf sourced")" "sourced"

	_fp_base="$(_fp_as "pgc_source_fingerprint \"$_fp/tree\"")"
	check "premise: the tree fingerprints to something when it is readable" \
		"$([ -n "$_fp_base" ] && [ "$_fp_base" != harness-unreadable ] && echo yes || echo "${_fp_base:-empty}")" "yes"

	# Premise for the mechanism: the unprivileged reader must agree with this
	# shell while nothing is denied, or the arms below measure the user switch.
	check "premise: the unprivileged read agrees while everything is readable" \
		"$_fp_base" "$(pgc_source_fingerprint "$_fp/tree")"
fi

# THE ARMS run only when the premises above were met. A guard that cannot run is
# not a guard that held, so the alternative to running them is SKIPPING them
# loudly -- never running them against a reader that is broken for a reason they
# do not describe. The premises stay FAILED in that case, so the suite is still
# red and still says why.
# NO `: # already skipped above` BRANCH. There was one, and it was wrong: "above"
# skipped the three PREMISES, not these five arms, so on a box with no non-root
# user five arms produced no record at all -- indistinguishable from five deleted
# checks, which is the whole of #994. With `_fp_base` initialised empty the `elif`
# below fires and skips all five under their own names, and its list already names
# them, so nothing is duplicated.
if [ -z "$_fp_base" ] || [ "$_fp_base" = harness-unreadable ] \
		|| [ "$_fp_base" != "$(pgc_source_fingerprint "$_fp/tree")" ]; then
	for _fp_n in "an unreadable b.c yields no fingerprint, not a wrong one" \
			"an unreadable c.c yields no fingerprint, not a wrong one" \
			"control: and the tree fingerprints again once it is readable" \
			"so the verdict is unknown -- UNVERIFIED -- and never stale" \
			"control: a readable run still reads fresh"; do
		check_skip "$_fp_n" \
			"SKIP  $_fp_n (the unprivileged reader could not fingerprint a readable tree)" \
			"the unprivileged reader could not fingerprint a readable tree"
	done
else
	# THE ARM. One unreadable file, and the answer must be EMPTY, not a hash.
	for _fp_n in b.c c.c; do
		chmod 000 "$_fp/tree/src/$_fp_n"
		_fp_got="$(_fp_as "pgc_source_fingerprint \"$_fp/tree\"")"
		chmod 644 "$_fp/tree/src/$_fp_n"
		check "an unreadable $_fp_n yields no fingerprint, not a wrong one" \
			"$([ -z "$_fp_got" ] && echo empty || echo "$_fp_got")" "empty"
	done

	check "control: and the tree fingerprints again once it is readable" \
		"$(_fp_as "pgc_source_fingerprint \"$_fp/tree\"")" "$_fp_base"

	# And the verdict that follows, which is the property that matters: the
	# controller must say UNVERIFIED, never `stale`. `stale` is the FATAL.
	chmod 000 "$_fp/tree/src/b.c"
	check "so the verdict is unknown -- UNVERIFIED -- and never stale" \
		"$(pgc_freshness_verdict "$_fp_base" \
			"$(_fp_as "pgc_source_fingerprint \"$_fp/tree\"")")" "unknown"
	chmod 644 "$_fp/tree/src/b.c"
	check "control: a readable run still reads fresh" \
		"$(pgc_freshness_verdict "$_fp_base" \
			"$(_fp_as "pgc_source_fingerprint \"$_fp/tree\"")")" "fresh"
fi
unset _fp_user _fp_u _fp_got _fp_n _fp_base _fp_harness _fp_cmd
unset -f _fp_as

# ---------------------------------------------------------------------------
# ONE TREE HASHES ONE WAY, HOWEVER THE PATH TO IT IS SPELLED.
#
# @OffgridwithJD's finding, reproduced and widened here. `${f#"$dir"/}` strips a
# prefix that must match CHARACTER FOR CHARACTER, so `$dir` with a trailing
# slash, or reached through a symlink, puts the FULL ABSOLUTE PATH into the
# digest instead of the tree-relative one:
#
#     plain             92410d0598d6
#     trailing slash    bf101efc7c10   differs
#     dot segment /./   774152fff929   differs
#     via symlink       3f3c0e36905a   differs
#     dot-dot /src/..   92410d0598d6
#     relative .        92410d0598d6
#
# The `/./` case is the one to keep: it is what a `$(dirname X)/./` composition
# produces and it reads as harmless. Two spellings of one tree must not be able
# to disagree, because the writer and the reader reach the tree by different
# routes and a disagreement there is a FATAL about nothing.

_sp="$(mktemp -d "${TMPDIR:-/tmp}/pgc-spell.XXXXXX")"
mkdir -p "$_sp/tree/src" "$_sp/tree/objstore"
printf 'int a;\n' > "$_sp/tree/src/a.c"
printf 'int b;\n' > "$_sp/tree/objstore/b.c"
printf 'all:\n\ttrue\n' > "$_sp/tree/objstore/Makefile"
printf 'all:\n\ttrue\n' > "$_sp/tree/Makefile"
printf 'x\n' > "$_sp/tree/pgcolumnar.control"
ln -s "$_sp/tree" "$_sp/link"

_sp_plain="$(pgc_source_fingerprint "$_sp/tree")"
check "premise: the spelling fixture fingerprints at all" \
	"$([ -n "$_sp_plain" ] && echo yes || echo empty)" "yes"

check "a trailing slash hashes the same tree the same way" \
	"$(pgc_source_fingerprint "$_sp/tree/")" "$_sp_plain"
check "a /./ segment hashes the same tree the same way" \
	"$(pgc_source_fingerprint "$_sp/tree/./")" "$_sp_plain"
check "a /src/.. segment hashes the same tree the same way" \
	"$(pgc_source_fingerprint "$_sp/tree/src/..")" "$_sp_plain"
check "a symlink to the tree hashes it the same way" \
	"$(pgc_source_fingerprint "$_sp/link")" "$_sp_plain"
check "a relative path hashes the same tree the same way" \
	"$(cd "$_sp/tree" && pgc_source_fingerprint .)" "$_sp_plain"

unset _fp _fp_base _fp_got _fp_n _sp _sp_plain

# ---------------------------------------------------------------------------
# THE FIX MUST NOT RE-BASELINE EVERY STAMP ALREADY ON DISK.
#
# Detecting a failed digest means capturing the per-file lines into a variable to
# inspect them, and `$(...)` STRIPS THE TRAILING NEWLINE that the old code's
# straight pipe into md5sum included. The same unchanged tree then hashes
# differently before and after the change, every stamp on disk reads `stale`, and
# a fix for false FATALs becomes a false FATAL for everyone holding a built
# worktree (@OffgridwithJD, caught before it shipped). The matrix cannot catch
# this: it copies a fresh tree and re-stamps every run, so it lands on developers
# and on nobody's CI.
#
# This arm is a COMPATIBILITY assertion, not a tidiness one. It recomputes the
# tree the way the previous implementation did and requires the same answer.

_bc="$(mktemp -d "${TMPDIR:-/tmp}/pgc-compat.XXXXXX")"
mkdir -p "$_bc/src" "$_bc/objstore"
printf 'int a;\n'        > "$_bc/src/a.c"
printf 'int b;\n'        > "$_bc/src/b.c"
printf 'void h(void);\n' > "$_bc/src/h.h"
printf 'int m;\n'        > "$_bc/objstore/m.c"
printf 'all:\n\ttrue\n'  > "$_bc/objstore/Makefile"
printf 'all:\n\ttrue\n'  > "$_bc/Makefile"
printf 'x\n'             > "$_bc/pgcolumnar.control"
printf 'SELECT 1;\n'     > "$_bc/pgcolumnar--9.9.sql"

# The PREVIOUS implementation, transcribed: the same input set and the same
# per-file `path digest` lines, piped straight into md5sum as it was.
_pgc_fp_previous() {
	local dir="${1:-.}" d
	{
		while IFS= read -r d; do
			[ -n "$d" ] || continue
			find "$d" -maxdepth 1 -type f \( -name '*.c' -o -name '*.h' \
				-o -name 'Makefile' \) -print0 2>/dev/null
		done < <(pgc_source_build_dirs "$dir")
		find "$dir" -maxdepth 1 -type f \( -name 'Makefile' -o -name '*.control' \
			-o -name '*.sql' \) -print0 2>/dev/null
	} | sort -z | while IFS= read -r -d '' _f; do
		printf '%s %s\n' "${_f#"$dir"/}" \
			"$(md5sum < "$_f" 2>/dev/null | cut -d' ' -f1)"
	done | md5sum | cut -c1-12
}

check "the fixed fingerprint equals what the previous implementation produced" \
	"$(pgc_source_fingerprint "$_bc")" "$(_pgc_fp_previous "$_bc")"

# THE CONTROL WITHOUT WHICH THE SPELLING ARMS ARE VACUOUS. "Every spelling
# agrees" is satisfied perfectly by a fingerprint that ignores its input, so the
# set needs one arm proving the hash still MOVES on a real change.
_bc_before="$(pgc_source_fingerprint "$_bc")"
printf 'int a = 2;\n' > "$_bc/src/a.c"
check "control: a real content change still moves the fingerprint" \
	"$([ "$(pgc_source_fingerprint "$_bc")" != "$_bc_before" ] && echo moved || echo SAME)" \
	"moved"
printf 'int a;\n' > "$_bc/src/a.c"
check "control: and restoring the content restores the fingerprint" \
	"$(pgc_source_fingerprint "$_bc")" "$_bc_before"

# A tree with nothing hashable cannot be verified, so it reports no fingerprint
# rather than the hash of an empty stream -- which is a stable, comparable value
# and would have made two empty trees "match".
# THE PREMISE IS LOAD-BEARING. This arm asserts an EMPTY result, and empty is
# also what a harness that cannot run produces -- so without a premise that the
# SAME function returns something for a real tree, it passes green over a broken
# one. Same shape as the expect.refusal defect @OffgridwithJD found on main: a
# failure that produces exactly the value the test expects.
_bc_empty="$(mktemp -d "${TMPDIR:-/tmp}/pgc-empty.XXXXXX")"
check "premise: the same function returns a fingerprint for a real tree" \
	"$([ -n "$(pgc_source_fingerprint "$_bc")" ] && echo yes || echo empty)" "yes"
check "a tree with no hashable file yields no fingerprint" \
	"$([ -z "$(pgc_source_fingerprint "$_bc_empty")" ] && echo empty || echo hashed)" "empty"

unset _bc _bc_before _bc_empty

# ---------------------------------------------------------------------------
# WHEN IT REFUSES, IT MUST SAY WHAT IT HASHED.
#
# Two CI failures reported the same pair of hashes and nothing else:
#
#     source now a735c673b129, binary built from 6d122a7158d5
#
# Identical across #902/PG18/iceberg_rest and #909/PG17/iceberg_rest_server --
# two branches, two majors, two build directories, and one of the two runs
# carried the fingerprint fix. That rules out a transient digest failure (which
# gives a different wrong hash every time) and a path-dependent one (two build
# directories would give two values). It is a deterministic, content-derived
# state, and neither agent has reproduced it locally.
#
# Twelve characters cannot say which file moved. A manifest can, and the
# difference between "the hash changed" and "objstore/x.c appeared" is the
# difference between another sample and an answer. So the manifest is a function
# in its own right, the fingerprint is its hash, and the FATAL path prints it.

_mf="$(mktemp -d "${TMPDIR:-/tmp}/pgc-manifest.XXXXXX")"
mkdir -p "$_mf/src" "$_mf/objstore"
printf 'int a;\n'       > "$_mf/src/a.c"
printf 'void h(void);\n'> "$_mf/src/h.h"
printf 'int m;\n'       > "$_mf/objstore/m.c"
printf 'all:\n\ttrue\n' > "$_mf/objstore/Makefile"
printf 'all:\n\ttrue\n' > "$_mf/Makefile"
printf 'x\n'            > "$_mf/pgcolumnar.control"

_mf_lines="$(pgc_source_manifest "$_mf" | wc -l)"
check "the manifest names every file the fingerprint hashes" "$_mf_lines" "6"

check "each manifest line is a tree-relative path and a digest" \
	"$(pgc_source_manifest "$_mf" | grep -cE '^[A-Za-z0-9_./-]+ [0-9a-f]{32}$')" "6"

check "the manifest is tree-relative, never absolute" \
	"$(pgc_source_manifest "$_mf" | grep -c '^/')" "0"

# The fingerprint IS the manifest's hash, so the two cannot drift apart.
check "the fingerprint is the hash of the manifest" \
	"$(pgc_source_fingerprint "$_mf")" \
	"$(pgc_source_manifest "$_mf" | md5sum | cut -c1-12)"

# THE ARM THAT MATTERS: an ADDED file is named, not merely counted. This is the
# class the two CI failures fall into and the one a bare hash cannot report.
printf 'int zz;\n' > "$_mf/src/zz_appeared.c"
check "an added file appears in the manifest by name" \
	"$(pgc_source_manifest "$_mf" | grep -c '^src/zz_appeared.c ')" "1"
check "and comparing two manifests names it rather than saying 'changed'" \
	"$(diff <(pgc_source_manifest "$_mf" | grep -v zz_appeared) \
		<(pgc_source_manifest "$_mf") | grep -oE 'src/zz_appeared\.c')" \
	"src/zz_appeared.c"
rm -f "$_mf/src/zz_appeared.c"

# A manifest for a tree that cannot be hashed is empty, matching the fingerprint's
# own refusal rather than inventing a second convention.
_mf_hollow="$(mktemp -d "${TMPDIR:-/tmp}/pgc-hollow.XXXXXX")"
check "an unhashable tree has an empty manifest" \
	"$(pgc_source_manifest "$_mf_hollow" | wc -l)" "0"

unset _mf _mf_lines _mf_hollow

# ---------------------------------------------------------------------------
# A SYMLINKED src/ MUST BE SKIPPED, LIKE EVERY OTHER SYMLINKED BUILD DIRECTORY.
#
# `build_dirs()` adds `root/"src"` unconditionally and then applies
# `if not d.is_dir() or d.is_symlink(): continue` to every OTHER candidate --
# so `src` is the one directory that bypasses its own rule. The shell this
# replaced ran `find "$d" -maxdepth 1 -type f`, and `find -P` does not descend a
# symlinked command-line argument, so it hashed nothing there.
#
# Measured on a tree whose src/ is a symlink:
#
#     find -P on the symlinked src/   printed nothing
#     the module's manifest           src/a.c, src/h.h
#     old shell fingerprint           9861a3f1fbd1
#     module fingerprint              9eff36abd48e     <- diverges
#
# A stamp written before the port then reads `stale` on a tree that is clean,
# which is the false FATAL the whole controller exists to prevent. The module's
# own comment states the invariant it breaks here.

_sl="$(mktemp -d "${TMPDIR:-/tmp}/pgc-symsrc.XXXXXX")"
mkdir -p "$_sl/real" "$_sl/tree"
printf 'int a;\n' > "$_sl/real/a.c"
printf 'void h(void);\n' > "$_sl/real/h.h"
ln -s "$_sl/real" "$_sl/tree/src"
printf 'all:\n\ttrue\n' > "$_sl/tree/Makefile"
printf 'x\n' > "$_sl/tree/pgcolumnar.control"

check "PREMISE the fixture's src really is a symlink" \
	"$([ -L "$_sl/tree/src" ] && echo yes || echo no)" "yes"
check "PREMISE and the target really holds sources find would otherwise hash" \
	"$(ls "$_sl/real" | tr '\n' ' ')" "a.c h.h "

# find -P is the reference: it prints nothing for a symlinked directory argument.
check "a symlinked src contributes nothing, as find -P contributes nothing" \
	"$(pgc_source_manifest "$_sl/tree" | grep -c '^src/')" "0"
check "so the tree still fingerprints from its root files alone" \
	"$(pgc_source_manifest "$_sl/tree" | wc -l)" "2"

# CONTROL: a REAL src directory must still be hashed, or the fix is "skip src".
_slr="$(mktemp -d "${TMPDIR:-/tmp}/pgc-realsrc.XXXXXX")"
mkdir -p "$_slr/src"
printf 'int a;\n' > "$_slr/src/a.c"
printf 'all:\n\ttrue\n' > "$_slr/Makefile"
printf 'x\n' > "$_slr/pgcolumnar.control"
check "control: a real src directory is still hashed" \
	"$(pgc_source_manifest "$_slr" | grep -c '^src/a.c ')" "1"

unset _sl _slr

# The FATAL path's dump, driven rather than grepped for. A report nobody can run
# is a report nobody knows is empty, and "the source says it calls it" is the kind
# of claim this suite exists to refuse.
_fr="$(mktemp -d "${TMPDIR:-/tmp}/pgc-report.XXXXXX")"
mkdir -p "$_fr/src"
printf 'int a;\n' > "$_fr/src/a.c"
printf 'all:\n\ttrue\n' > "$_fr/Makefile"
printf 'x\n' > "$_fr/pgcolumnar.control"

check "the report names each hashed file" \
	"$(pgc_freshness_report "$_fr" | grep -c '^       | src/a.c ')" "1"
check "the report states how many files it hashed" \
	"$(pgc_freshness_report "$_fr" | grep -cE '^       \(3 files, under ')" "1"
check "an added file shows up in the report" \
	"$(printf 'int z;\n' > "$_fr/src/z.c"; pgc_freshness_report "$_fr" | grep -c '^       | src/z.c ')" "1"
rm -f "$_fr/src/z.c"
# The empty case says so rather than printing nothing, because a silent empty
# dump reads as "the manifest was fine" -- the failure this report exists to end.
_fr_hollow="$(mktemp -d "${TMPDIR:-/tmp}/pgc-rhollow.XXXXXX")"
check "an empty manifest is reported as empty, not as silence" \
	"$(pgc_freshness_report "$_fr_hollow" | grep -c 'empty -- nothing under')" "1"
unset _fr _fr_hollow


# ---- one tree hashes one way, however the LOCALE is set ---------------------
#
# The defect the single implementation removed on the way (#907). The shell
# sorted its manifest with `sort -z`, which uses LOCALE COLLATION, and nothing in
# this harness pins a locale. So the same tree fingerprinted two ways depending
# on whose machine it was:
#
#     LC_ALL=C            6d122a7158d5
#     LC_ALL=en_US.UTF-8  0b59bd75fa4f
#
# en_US.UTF-8 is a common desktop default, so this was a developer stamping a
# tree and CI reading it back under C.UTF-8 and calling the binary stale. The
# module sorts BYTES, which is what LC_ALL=C did and what every stamp already on
# disk was written with.
#
# `_` against `-` is what the two collations order differently, and
# columnar_arrow.c beside columnar-arrow.c is not a contrived pair in this tree.
_lc="$(mktemp -d "${TMPDIR:-/tmp}/pgc-locale.XXXXXX")"
mkdir -p "$_lc/tree/src"
for _lc_n in columnar_arrow.c columnar-arrow.c columnarXarrow.c Columnar.c columnar.c; do
	printf 'int x; /* %s */\n' "$_lc_n" > "$_lc/tree/src/$_lc_n"
done
printf 'all:\n\ttrue\n' > "$_lc/tree/Makefile"
printf 'x\n' > "$_lc/tree/pgcolumnar.control"

_lc_have=""
for _lc_l in C C.utf8 en_US.utf8; do
	# grep -c, not grep -q; see selftest 080. locale -a lists hundreds of names.
	[ "$(locale -a 2>/dev/null | grep -cx "$_lc_l" || true)" != 0 ] \
		&& _lc_have="$_lc_have $_lc_l"
done
_lc_count="$(printf '%s\n' $_lc_have | grep -c .)"

check "premise: at least two locales are installed to compare" \
	"$([ "$_lc_count" -ge 2 ] && echo enough || echo "$_lc_count")" "enough"

if [ "$_lc_count" -ge 2 ]; then
	_lc_vals=""
	for _lc_l in $_lc_have; do
		_lc_vals="$_lc_vals $(LC_ALL="$_lc_l" LANG="$_lc_l" pgc_source_fingerprint "$_lc/tree")"
	done
	check "premise: every locale produced a fingerprint" \
		"$([ -n "$(printf '%s' $_lc_vals)" ] && echo yes || echo empty)" "yes"
	check "one tree, one fingerprint, whatever the locale" \
		"$(printf '%s\n' $_lc_vals | sort -u | grep -c .)" "1"
fi
rm -rf "$_lc"
unset _lc _lc_n _lc_l _lc_have _lc_count _lc_vals

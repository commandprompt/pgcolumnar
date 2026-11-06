# ---- the list stays sorted, which is what actually stops the conflicts ------
#
# One name per line was not enough on its own. Measured, on this repository, by
# branching twice and merging:
#
#   one line,     both additions on the same line          CONFLICT
#   one per line, both appended at the end                 CONFLICT
#   one per line + sorted, names far apart                 clean
#   one per line + sorted, names that sort adjacently      CONFLICT
#
# Everyone appends at the end, which is the shape all four of #469's conflicts
# had, so one-per-line alone would have left them all conflicting. Sorted gives a
# new suite an insertion point decided by its NAME, so two unrelated additions
# land in different places and merge. It is a large reduction and not a cure:
# two names that sort next to each other still collide.
#
# This check is what keeps the property true. Without it the order decays the
# first time somebody appends by hand, and the reduction quietly goes away.
#
# LC_ALL=C, and the collation is part of the property rather than a detail (#552).
# "Sorted" is not machine-independent: C compares byte by byte so `_` (0x5F)
# precedes `e`, while en_US.UTF-8 ignores punctuation at the first level and
# compares `sortstatus` against `sortedprojection`. The array holds sort_status
# then sorted_projection, so it is sorted in one and unsorted in the other, and
# this check pinned neither. On a clean main it FAILED under en_US.UTF-8 and
# passed here only because the container defaults to C.UTF-8, which collates
# like C.
#
# The ambiguity is worse than the false red. Sorted order is what gives two
# agents' new suites different insertion points; if two contributors disagree
# about what sorted means they insert in different places, and the property stops
# delivering the merges it exists for.
_sorted_expected="$(listed_suites | LC_ALL=C sort)"
_sorted_actual="$(listed_suites)"
check "the suite list is sorted in C order, so two new suites land in different places" \
	"$([ "$_sorted_actual" = "$_sorted_expected" ] && echo sorted || echo "not sorted")" "sorted"

# The pair that decides it, asserted directly so a future edit that "fixes" the
# order to UTF-8 collation fails here with the reason rather than only failing
# the comparison above.
check "premise: C collation puts sort_status before sorted_projection" \
	"$(printf 'sorted_projection\nsort_status\n' | LC_ALL=C sort | head -1)" "sort_status"


# ---- and no name is listed twice (#2025) ------------------------------------
#
# The sorted check above compares the list with `LC_ALL=C sort`, and sort keeps
# duplicates: a name listed twice sits next to itself, so the list is still
# "sorted". The matrix then runs that suite twice in every job. Measured on the
# review of #2021: with native_scale listed twice, harness_selftest passed
# 1290/1290, because nothing else looked.
#
# So duplicates are a check of their own, and it prints the names it found.
_listed_twice() {	# the names a runner's SUITES holds more than once, on one line
	listed_suites "$@" | LC_ALL=C sort | uniq -d | tr '\n' ' ' | sed 's/ $//'
}

# The fixture is the real runner with its first suite printed twice, in place,
# so the duplicate is adjacent and the list stays sorted. The name is read from
# the runner rather than written here, so renaming that suite cannot stale it.
_dup_first="${_SUITE_LIST%%$'\n'*}"
_dup_fx="$(mktemp /tmp/pgc-runner-dup.XXXXXX.sh)"
awk '{ print } /^SUITES=\(/ { getline; print; print }' "$RUNNER" > "$_dup_fx"

check_num "premise: the duplicate fixture lists its first suite twice" \
	"$(listed_suites "$_dup_fx" | grep -cx "$_dup_first")" "2"
# What makes the next check necessary: the sorted comparison cannot see this.
check "premise: and the fixture is still sorted in C order, so only the duplicate check can catch it" \
	"$([ "$(listed_suites "$_dup_fx")" = "$(listed_suites "$_dup_fx" | LC_ALL=C sort)" ] &&
	   echo sorted || echo "not sorted")" "sorted"
check "a suite listed twice is reported by name (#2025)" \
	"$(_listed_twice "$_dup_fx")" "$_dup_first"
rm -f "$_dup_fx"

check "no suite is listed twice in SUITES (#2025)" "$(_listed_twice)" ""

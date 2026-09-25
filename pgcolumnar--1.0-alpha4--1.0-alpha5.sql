/*
 * pgcolumnar--1.0-alpha4--1.0-alpha5.sql
 *
 * Upgrade from 1.0-alpha4 to 1.0-alpha5.
 *
 * 1.0-alpha4 is a PUBLISHED pre-release (tag v1.0-alpha4), so
 * pgcolumnar--1.0-alpha3--1.0-alpha4.sql is a shipped artifact and must not
 * change. Anything alpha5 adds to the catalog belongs here, or an existing
 * alpha4 install would never run it and a fresh install would silently get a
 * different catalog from an upgraded one.
 *
 * EMPTY ON PURPOSE, AND THAT IS WHY IT EXISTS. This change opens the cycle and
 * nothing else. The file is here so that the work already pointed at alpha5 --
 * the zone-map summary that overlap and containment pruning needs (#1144), and
 * cascading step 2 (#1139) -- appends to a script that already exists rather
 * than racing to create it, and so that the version bump is reviewable on its
 * own rather than inside a feature diff.
 *
 * PostgreSQL requires the file to exist and to be readable; an upgrade script
 * that adds nothing is a valid one. `ALTER EXTENSION pgcolumnar UPDATE` from
 * alpha4 reaches alpha5 through this file and changes no catalog object, which
 * is what test/extension_upgrade.sh asserts by comparing the upgraded catalog
 * against a fresh install of the new version.
 */

-- The greatest upper bound of a RANGE column's values in a zone map unit
-- (#1144). Overlap and containment cannot use minimum/maximum: those are
-- recorded under the range type's btree ordering, which sorts by lower bound
-- and then upper bound, and the lexicographically largest range is not the one
-- reaching furthest right. A chunk holding [1,2) and [3,100) has the same
-- maximum as one holding [1,2) and [3,4).
--
-- THREE STATES, and the third is why this is a bytea rather than the element
-- type. NULL means no summary, and a reader must not prune from above -- which
-- is what every row written before this column existed carries. A ZERO-LENGTH
-- value means the unit holds a range that is unbounded above: also do not prune,
-- but a different fact, and the field is PRESENT and understated rather than
-- absent, which a two-state design cannot express. Any other length is the
-- bound, encoded with the range's ELEMENT type.
--
-- Only range columns populate it; every other column leaves it NULL. The column
-- is in pgcolumnar--1.0-alpha5.sql too, so a FRESH install and an UPGRADED one
-- converge; native_upgrade_converge.sh is what asserts that.
ALTER TABLE pgcolumnar.zone_map ADD COLUMN IF NOT EXISTS max_upper bytea;

-- reset_options must refuse what set_options refuses (#1265). It had no guard
-- at all, so it reported success for a heap table, a partitioned parent and a
-- plain view while its sibling errored on the same relation. The predicate is
-- copied from set_options verbatim and deliberately not widened; see the
-- comment in the function body.
CREATE OR REPLACE FUNCTION pgcolumnar.reset_options(
	table_name regclass,
	chunk_group_row_limit bool DEFAULT false,
	stripe_row_limit bool DEFAULT false,
	compression bool DEFAULT false,
	compression_level bool DEFAULT false,
	encode_effort bool DEFAULT false,
	sort_by bool DEFAULT false)
	RETURNS void
	LANGUAGE plpgsql
	AS $reset_options$
BEGIN
	/*
	 * THE SAME PREDICATE set_options USES, and the pair must stay that way
	 * (#1265). This function had no guard at all -- no relam test, no relkind
	 * test, just the UPDATE -- so it reported SUCCESS for a heap table, a
	 * partitioned parent and a plain view, while set_options refused the
	 * identical relation. Nothing was corrupted: it reported success for
	 * something that could not have applied, which is the harder half to
	 * notice.
	 *
	 * DELIBERATELY NOT WIDENED HERE. Whether a columnar MATERIALIZED VIEW
	 * should hold options is open: set_options refuses one while compact,
	 * vacuum_sorted and stats accept it, and widening it is coupled to the
	 * drop hook, whose `!= RELKIND_RELATION` return means an options row on a
	 * matview would be ORPHANED on a dead oid when it is dropped. That pair
	 * moves together or not at all. Copying set_options' predicate exactly is
	 * what makes this change neutral on that question.
	 *
	 * The copy is what lets two predicates drift, so phase5 drives both
	 * entry points over the same relations and asserts they reach the SAME
	 * verdict -- parity rather than a relkind list, so the arm survives the
	 * matview question being settled either way.
	 */
	IF NOT EXISTS (SELECT 1 FROM pg_class c
					 JOIN pg_am a ON a.oid = c.relam
					WHERE c.oid = table_name
					  AND a.amname = 'pgcolumnar'
					  AND c.relkind = 'r') THEN
		RAISE EXCEPTION 'relation "%" is not a columnar table', table_name
			USING ERRCODE = 'wrong_object_type',
				HINT = 'Per-table options are read by the columnar writer and '
				'apply only to an ordinary table using the pgcolumnar access '
				'method. A partitioned table has no storage of its own: reset '
				'the options on each partition.';
	END IF;

	UPDATE pgcolumnar.options o SET
		chunk_group_row_limit = CASE
			WHEN reset_options.chunk_group_row_limit
			THEN NULL ELSE o.chunk_group_row_limit END,
		stripe_row_limit = CASE
			WHEN reset_options.stripe_row_limit
			THEN NULL ELSE o.stripe_row_limit END,
		compression = CASE
			WHEN reset_options.compression
			THEN NULL ELSE o.compression END,
		compression_level = CASE
			WHEN reset_options.compression_level
			THEN NULL ELSE o.compression_level END,
		encode_effort = CASE
			WHEN reset_options.encode_effort
			THEN NULL ELSE o.encode_effort END,
		sort_by = CASE
			WHEN reset_options.sort_by
			THEN NULL ELSE o.sort_by END
	WHERE o.regclass = table_name;
END;
$reset_options$;

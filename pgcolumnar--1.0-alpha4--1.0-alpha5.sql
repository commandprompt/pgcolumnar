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
					  AND c.relkind IN ('r', 'm')) THEN
		RAISE EXCEPTION 'relation "%" is not a columnar table', table_name
			USING ERRCODE = 'wrong_object_type',
				HINT = 'Per-table options are read by the columnar writer and '
				'apply only to an ordinary table or materialized view using the '
				'pgcolumnar access method. A partitioned table has no storage of '
				'its own: reset the options on each partition.';
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

-- set_options accepts a columnar MATERIALIZED VIEW (#1265). jd settled the
-- question this file's sibling comment left open: a matview holds rows of its
-- own, so the writer reads its options, and the drop hook was widened in the
-- same change to clear the row afterwards.
--
-- THIS BLOCK IS WHY THE FILE HEADER SAYS WHAT IT SAYS. set_options was not
-- redefined here before, because it had not changed since alpha2--alpha3.
-- Widening only pgcolumnar--1.0-alpha5.sql left a fresh install accepting a
-- matview and every UPGRADED install still refusing one -- one diverging line,
-- caught by native_upgrade_converge.sh on all four upgrade paths:
--
--   116c116
--   < FN|pgcolumnar.set_options(...)|2113853fea4ad7a654560b4e1784045e|...
--   > FN|pgcolumnar.set_options(...)|44abe8992bafb0acd1e809cdb1b0c3d9|...
--
-- The body below is copied verbatim from pgcolumnar--1.0-alpha5.sql, with only
-- CREATE changed to CREATE OR REPLACE. The two must stay byte-identical from
-- the argument list down; that suite is what enforces it. CREATE OR REPLACE
-- keeps the existing COMMENT ON, which is why none is re-issued here -- the
-- same reason reset_options above does not re-issue its own.

CREATE OR REPLACE FUNCTION pgcolumnar.set_options(
	table_name regclass,
	chunk_group_row_limit int DEFAULT NULL,
	stripe_row_limit int DEFAULT NULL,
	compression name DEFAULT NULL,
	compression_level int DEFAULT NULL,
	encode_effort name DEFAULT NULL,
	sort_by name[] DEFAULT NULL,
	ttl_column name DEFAULT NULL,
	ttl_interval interval DEFAULT NULL)
	RETURNS void
	LANGUAGE plpgsql
	AS $set_options$
DECLARE
	col name;
BEGIN
	/*
	 * The options are per-relation and are read by the columnar writer, so a row
	 * recorded for a relation that is not columnar can never be used. Storing one
	 * is not merely useless: the drop hook that clears pgcolumnar.options fires
	 * only for columnar relations, so the row outlives the table and is left
	 * keyed to a dangling oid that a later relation reusing that oid inherits.
	 * Measured before this guard, on the same cluster: set_options on a heap
	 * table stored a row, DROP TABLE left it behind, and regclass then rendered
	 * as the bare oid; the identical sequence on a columnar table cleaned up.
	 *
	 * Rejecting is safe for the one workflow that could want the other order:
	 * ALTER TABLE ... SET ACCESS METHOD pgcolumnar keeps the relation's oid
	 * (measured), so options set after the conversion apply to the same relation
	 * a caller would have been trying to name before it.
	 *
	 * The ERRCODE is explicit. plpgsql's RAISE EXCEPTION defaults to P0001, and
	 * the C paths raise this same sentence with ERRCODE_WRONG_OBJECT_TYPE
	 * (42809). Without it the identical message carried two different SQLSTATEs
	 * depending on which path refused the caller, in a tree whose own privilege
	 * suites deliberately assert SQLSTATE rather than message text.
	 *
	 * relkind is part of the test, and it is what makes the guard match the
	 * cleanup rather than merely look strict. The drop hook returns before it
	 * examines the access method for anything that is not an ordinary table
	 * (columnar_tableam.c: `if (get_rel_relkind(objectId) != RELKIND_RELATION)
	 * return;`), so 'r' is exactly the set of relations whose options row can
	 * ever be cleaned up. From PG17 a PARTITIONED table may carry an access
	 * method, so `relam = pgcolumnar` alone admits a parent that has no storage,
	 * that the writer never writes, and whose row the hook will never clear.
	 * Measured on 17.6 with the amname-only test: accepted, one row recorded,
	 * and the row still there after DROP TABLE keyed to the dropped oid, while
	 * an ordinary columnar table in the same run cleaned up. PG16 and earlier
	 * cannot reach it -- they refuse `PARTITION BY ... USING pgcolumnar`
	 * outright, checked on 16.14 -- so this is PG17, 18 and 19.
	 */
	IF NOT EXISTS (SELECT 1 FROM pg_class c
					 JOIN pg_am a ON a.oid = c.relam
					WHERE c.oid = table_name
					  AND a.amname = 'pgcolumnar'
					  AND c.relkind IN ('r', 'm')) THEN
		RAISE EXCEPTION 'relation "%" is not a columnar table', table_name
			USING ERRCODE = 'wrong_object_type',
				HINT = 'Per-table options are read by the columnar writer and '
				'apply only to an ordinary table or materialized view using the '
				'pgcolumnar access method. A partitioned table has no storage of '
				'its own: set the options on each partition. Otherwise convert '
				'the table first with ALTER TABLE ... SET ACCESS METHOD '
				'pgcolumnar, then set the options.';
	END IF;

	IF encode_effort IS NOT NULL AND
	   encode_effort NOT IN ('full', 'fast') THEN
		RAISE EXCEPTION 'unknown columnar encode_effort "%"', encode_effort
			USING HINT = 'Valid values are "full" and "fast".';
	END IF;

	IF compression IS NOT NULL AND
	   compression NOT IN ('none', 'pglz', 'lz4', 'zstd') THEN
		RAISE EXCEPTION 'unknown columnar compression "%"', compression;
	END IF;

	/*
	 * Bound the integer limits to the same valid ranges as the instance-wide
	 * GUCs (pgcolumnar.chunk_group_row_limit, pgcolumnar.stripe_row_limit,
	 * pgcolumnar.compression_level). A per-table value outside these ranges is
	 * rejected here rather than stored: a limit of zero or below would produce
	 * a stripe whose recorded chunk_row_count is zero and make the row-number
	 * arithmetic (chunk id = offset / chunk_row_count) divide by zero on
	 * delete, update, and index fetch.
	 */
	IF chunk_group_row_limit IS NOT NULL AND chunk_group_row_limit < 100 THEN
		RAISE EXCEPTION 'chunk_group_row_limit must be at least 100';
	END IF;
	IF stripe_row_limit IS NOT NULL AND stripe_row_limit < 1000 THEN
		RAISE EXCEPTION 'stripe_row_limit must be at least 1000';
	END IF;
	IF compression_level IS NOT NULL AND
	   (compression_level < 1 OR compression_level > 22) THEN
		RAISE EXCEPTION 'compression_level must be between 1 and 22';
	END IF;

	/*
	 * A negative retention puts the cutoff in the FUTURE, so expire finds
	 * `maximum < cutoff` true for groups that are entirely inside their
	 * retention and retires them. That drops live rows, which is the failure
	 * this option exists to prevent. Every other option here is range-checked
	 * and this one was not.
	 *
	 * Zero is refused too. It is not a data-loss shape -- the cutoff is now, so
	 * only groups already wholly in the past go -- but "expire everything older
	 * than nothing" has no reading a caller means on purpose, and accepting it
	 * silently makes a typo indistinguishable from an instruction.
	 *
	 * ERRCODE is explicit for the reason the relkind guard above gives: this
	 * tree's suites assert SQLSTATE rather than message text, and plpgsql would
	 * otherwise default to P0001.
	 */
	IF ttl_interval IS NOT NULL AND ttl_interval <= interval '0' THEN
		RAISE EXCEPTION 'ttl_interval must be a positive interval, not %', ttl_interval
			USING ERRCODE = 'invalid_parameter_value',
				HINT = 'A negative retention puts the cutoff in the future, '
				'so pgcolumnar.expire() would retire groups whose rows are '
				'still within their retention.';
	END IF;

	/*
	 * sort_by declares the physical sort key applied by vacuum_sorted() with no
	 * explicit columns (#288). This is a cheap early check only: each named
	 * column must exist, not be dropped, and not be a VIRTUAL generated column
	 * (its value is not stored, so it cannot be sorted on). Orderability
	 * (a default btree ordering operator) is NOT checked here -- the C apply
	 * path is authoritative and re-resolves and re-validates the names every
	 * run, because a column can be dropped or altered after it is declared.
	 * attgenerated is '' or 's' before PG18; 'v' only exists from PG18, so the
	 * "<> 'v'" test is correct and inert on older majors.
	 */
	IF sort_by IS NOT NULL THEN
		FOREACH col IN ARRAY sort_by LOOP
			IF NOT EXISTS (SELECT 1 FROM pg_attribute a
						   WHERE a.attrelid = table_name
							 AND a.attname = col
							 AND a.attnum > 0
							 AND NOT a.attisdropped
							 AND a.attgenerated <> 'v') THEN
				RAISE EXCEPTION 'column "%" cannot be used in sort_by for table %',
					col, table_name
					USING HINT = 'The column must exist, must not be dropped, '
						'and must not be a VIRTUAL generated column.';
			END IF;
		END LOOP;
	END IF;

	INSERT INTO pgcolumnar.options AS o
		(regclass, chunk_group_row_limit, stripe_row_limit,
		 compression, compression_level, encode_effort, sort_by,
		 ttl_column, ttl_interval)
	VALUES (table_name, chunk_group_row_limit, stripe_row_limit,
			compression, compression_level, encode_effort, sort_by,
			ttl_column, ttl_interval)
	ON CONFLICT (regclass) DO UPDATE SET
		chunk_group_row_limit =
			COALESCE(EXCLUDED.chunk_group_row_limit, o.chunk_group_row_limit),
		stripe_row_limit =
			COALESCE(EXCLUDED.stripe_row_limit, o.stripe_row_limit),
		compression =
			COALESCE(EXCLUDED.compression, o.compression),
		compression_level =
			COALESCE(EXCLUDED.compression_level, o.compression_level),
		encode_effort =
			COALESCE(EXCLUDED.encode_effort, o.encode_effort),
		sort_by =
			COALESCE(EXCLUDED.sort_by, o.sort_by),
		ttl_column =
			COALESCE(EXCLUDED.ttl_column, o.ttl_column),
		ttl_interval =
			COALESCE(EXCLUDED.ttl_interval, o.ttl_interval);
END;
$set_options$;

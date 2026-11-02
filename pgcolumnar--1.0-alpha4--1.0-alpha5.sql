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
 * IT WAS CREATED EMPTY to open the cycle, so that the work already pointed at
 * alpha5 appended to a script that existed rather than racing to create it. It
 * is no longer empty. It now adds `pgcolumnar.zone_map.max_upper` for range
 * pruning (#1144), and replaces `reset_options`, `set_options` and `"analyze"`
 * (#1265, #1276). test/extension_upgrade.sh asserts that the upgraded catalog is
 * byte-identical to a fresh install of the new version.
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
-- #1309 adds sketch_columns, a new argument, so the old signature is dropped
-- rather than replaced, and the COMMENT ON is re-issued with it.
DROP FUNCTION IF EXISTS pgcolumnar.reset_options(regclass, bool, bool, bool, bool, bool, bool);

CREATE FUNCTION pgcolumnar.reset_options(
	table_name regclass,
	chunk_group_row_limit bool DEFAULT false,
	stripe_row_limit bool DEFAULT false,
	compression bool DEFAULT false,
	compression_level bool DEFAULT false,
	encode_effort bool DEFAULT false,
	sort_by bool DEFAULT false,
	sketch_columns bool DEFAULT false)
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
			THEN NULL ELSE o.sort_by END,
		sketch_columns = CASE
			WHEN reset_options.sketch_columns
			THEN NULL ELSE o.sketch_columns END
	WHERE o.regclass = table_name;
END;
$reset_options$;

COMMENT ON FUNCTION pgcolumnar.reset_options(regclass, bool, bool, bool, bool, bool, bool, bool)
	IS 'reset per-table columnar options to the instance defaults';

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
-- The body below is copied verbatim from pgcolumnar--1.0-alpha5.sql. The two
-- must stay byte-identical from the argument list down; that suite is what
-- enforces it. Since #1309 the old signature is dropped first, so the COMMENT ON
-- is re-issued after it, as reset_options' above is.

-- #1309 adds sketch_columns, a new argument: dropped and re-created, comment and all.
DROP FUNCTION IF EXISTS pgcolumnar.set_options(regclass, int, int, name, int, name, name[], name, interval);

CREATE FUNCTION pgcolumnar.set_options(
	table_name regclass,
	chunk_group_row_limit int DEFAULT NULL,
	stripe_row_limit int DEFAULT NULL,
	compression name DEFAULT NULL,
	compression_level int DEFAULT NULL,
	encode_effort name DEFAULT NULL,
	sort_by name[] DEFAULT NULL,
	ttl_column name DEFAULT NULL,
	ttl_interval interval DEFAULT NULL,
	sketch_columns name[] DEFAULT NULL)
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

	/*
	 * sketch_columns (#1309): each named column must exist, and must be one the
	 * writer can sketch. pgcolumnar.check_sketch_column applies the writer's own
	 * predicate, so set_options cannot accept a column the writer would skip.
	 */
	IF sketch_columns IS NOT NULL THEN
		FOREACH col IN ARRAY sketch_columns LOOP
			IF NOT EXISTS (SELECT 1 FROM pg_attribute a
						   WHERE a.attrelid = table_name
							 AND a.attname = col
							 AND a.attnum > 0
							 AND NOT a.attisdropped
							 AND a.attgenerated <> 'v') THEN
				RAISE EXCEPTION 'column "%" cannot be used in sketch_columns for table %',
					col, table_name
					USING ERRCODE = 'undefined_column',
						HINT = 'The column must exist, must not be dropped, '
						'and must not be a VIRTUAL generated column.';
			END IF;
			PERFORM pgcolumnar.check_sketch_column(table_name, col);
		END LOOP;
	END IF;

	INSERT INTO pgcolumnar.options AS o
		(regclass, chunk_group_row_limit, stripe_row_limit,
		 compression, compression_level, encode_effort, sort_by,
		 ttl_column, ttl_interval, sketch_columns)
	VALUES (table_name, chunk_group_row_limit, stripe_row_limit,
			compression, compression_level, encode_effort, sort_by,
			ttl_column, ttl_interval, sketch_columns)
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
			COALESCE(EXCLUDED.ttl_interval, o.ttl_interval),
		sketch_columns =
			COALESCE(EXCLUDED.sketch_columns, o.sketch_columns);
END;
$set_options$;

COMMENT ON FUNCTION pgcolumnar.set_options(regclass, int, int, name, int, name, name[], name, interval, name[])
	IS 'set per-table columnar options; NULL leaves a value unchanged. sort_by declares the physical sort key applied by vacuum_sorted() with no explicit columns (#288); it is NOT auto-maintained -- rows inserted after a sort append in insert order, so re-run vacuum_sorted() to re-establish it, like PostgreSQL CLUSTER';


/*
 * pgcolumnar.analyze(): resolve the storage id through the metapage (#1276).
 *
 * THE WHOLE BODY, NOT A PATCH, BECAUSE AN UPGRADE SCRIPT CANNOT PATCH ONE.
 * `pgcolumnar."analyze"` was last defined in pgcolumnar--1.0-alpha--1.0-alpha2.sql,
 * a SHIPPED artifact that must not change, so an alpha4 install carries that
 * body until something here replaces it. Editing only the alpha5 base script
 * leaves a fresh install and an upgraded one with DIFFERENT function bodies --
 * measured, and native_upgrade_converge.sh refused it:
 *
 *   FN|pgcolumnar."analyze"(regclass,text[])|308fd56285d1abe1e2b2b357d33be38a   upgraded
 *   FN|pgcolumnar."analyze"(regclass,text[])|40593c35109024f49cde0891c7b40fd9   fresh
 *
 * on all four upgrade paths. The suite is the ground truth here and it caught
 * an incompleteness reading could not: a grep for `FUNCTION pgcolumnar.analyze`
 * does not match `pgcolumnar."analyze"`, and the name is quoted because
 * `analyze` is a keyword.
 *
 * QUOTED AND `CREATE OR REPLACE`, matching what alpha2 shipped, so the
 * signature this replaces is the one that is already there.
 */
CREATE OR REPLACE FUNCTION pgcolumnar."analyze"(rel regclass, columns text[] DEFAULT NULL::text[])
	RETURNS void
	LANGUAGE plpgsql
	AS $$
DECLARE
	sid        bigint;
	att        record;
	nullfrac   double precision;
	ndistinct  bigint;
	totalrows  bigint;
	ndstat     double precision;
	hist       text;
	mcvvals    text;
	mcvfreqs   real[];
	orderable  boolean;
	nmcv       integer;
	nremaining bigint;
	nullcount  bigint;	/* live rows with no value, from the same read */
	nonnull    bigint;	/* rows with a value, from the aggregation below */
	mcvrows    bigint;	/* of those, the rows the MCV list holds */
	nv         bigint;	/* the population the histogram is placed over */
	nfrac      integer;
	-- The per-column target, resolved inside the loop. attstattarget is NULL when
	-- the column has never been given one, and core reads that as "use the global
	-- default" (analyze.c:1065 with :1897). A zero means do not collect at all.
	deftarget  integer := current_setting('default_statistics_target')::integer;
	nbuckets   integer;
	seen       integer := 0;
	disabled   integer := 0;
	unknown    text;
	schname    text;
	relnm      text;
BEGIN
	/*
	 * Writing statistics uses pg_restore_attribute_stats, which core added in
	 * 18. On 15 to 17 this would mean writing pg_statistic directly, and the
	 * risk there is in the values rather than the insert: stavalues is anyarray
	 * and must carry the column's element type, typmod and collation; staop must
	 * be the right operator for the stakind; stadistinct has a sign convention
	 * that is easy to invert. Each of those produces plausible wrong estimates
	 * rather than an error. Refuse clearly instead of failing obscurely inside
	 * the call below.
	 */
	IF current_setting('server_version_num')::int < 180000 THEN
		RAISE EXCEPTION 'pgcolumnar.analyze() requires PostgreSQL 18 or later'
			USING DETAIL = 'it writes statistics through pg_restore_attribute_stats, which older majors do not have',
				  HINT = 'use ANALYZE on this server';
	END IF;

	/*
	 * pg_restore_attribute_stats identifies the column by schema and relation
	 * NAME, not by regclass, and rejects a null schemaname. Resolve both from the
	 * oid once rather than per column.
	 */
	SELECT n.nspname, c.relname INTO schname, relnm
		FROM pg_class c
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE c.oid = rel;

	/*
	 * THROUGH THE METAPAGE, NOT relation_oid (#1276). relation_oid is not
	 * unique in pgcolumnar.storage -- storage_pkey, on storage_id, is the only
	 * unique index -- because a covering projection gets its OWN row carrying
	 * the BASE table's relation_oid. SELECT ... INTO then took whichever row
	 * heap order handed back first, without complaining about the second.
	 *
	 * The harm was missing statistics rather than wrong ones. sid is only a
	 * GATE below; the values come from reading the column. A projection
	 * NARROWER than its base fails that gate for every column index it does not
	 * carry, and CONTINUE skips those columns in silence with a successful
	 * return. Measured on a five-column table covered by a one-column
	 * projection: five columns with statistics became two.
	 *
	 * This is the idiom three siblings in this file already hold, at the
	 * sort_status, stats and maintenance_due readers. #1210 fixed the same root
	 * cause in C and does not touch this caller.
	 *
	 * IT DOES NOT FIX #1275 AND MUST NOT BE READ AS DOING SO. A matview created
	 * WITH DATA has an orphaned relation_oid until its first REFRESH, so this
	 * caller stops hitting that orphan while the orphan itself remains for every
	 * other reader.
	 *
	 * THIS ONLY HOLDS WHILE analyze() IS INVOKER-RIGHTS. get_storage_id carries
	 * its own pg_class_aclcheck for ACL_SELECT against GetUserId(), which is the
	 * CALLER here and would become the FUNCTION OWNER the day anyone adds
	 * SECURITY DEFINER to this declaration. The check would then stop protecting
	 * anybody and no test would fail, because every arm that exercises it runs
	 * as a caller who is also the owner. Two of this file's closest siblings are
	 * already SECURITY DEFINER and :769 carries a helper written for such
	 * callers, so the conversion is a live possibility rather than a hypothetical.
	 * Raised by @jdatcmd.
	 */
	SELECT s.storage_id INTO sid
		FROM pgcolumnar.storage s
		WHERE s.storage_id = pgcolumnar.get_storage_id(rel);

	IF sid IS NULL THEN
		RAISE EXCEPTION 'pgcolumnar.analyze(): % has no columnar storage', rel::text
			USING HINT = 'this function only applies to pgcolumnar tables that have been written to';
	END IF;

	/*
	 * A named column that does not exist is a caller error, not a no-op. Silently
	 * collecting nothing is the failure mode that looks exactly like success.
	 */
	IF columns IS NOT NULL THEN
		SELECT c INTO unknown
			FROM unnest(columns) AS c
			WHERE NOT EXISTS (
				SELECT 1 FROM pg_attribute a
					WHERE a.attrelid = rel AND a.attname = c
					  AND a.attnum > 0 AND NOT a.attisdropped)
			LIMIT 1;
		IF unknown IS NOT NULL THEN
			RAISE EXCEPTION 'pgcolumnar.analyze(): column "%" does not exist in %',
				unknown, rel::text;
		END IF;
	END IF;

	FOR att IN
		SELECT a.attname, a.attnum, a.atttypid, a.attstattarget
			FROM pg_attribute a
			WHERE a.attrelid = rel AND a.attnum > 0 AND NOT a.attisdropped
			  AND (columns IS NULL OR a.attname = ANY (columns))
			ORDER BY a.attnum
	LOOP
		/*
		 * The per-column statistics target, which is core's rule and not the
		 * global setting:
		 *
		 *     attstattarget = isnull ? -1 : DatumGetInt16(dat);   analyze.c:1065
		 *     if (attstattarget == 0) return NULL;                        :1070
		 *     if (stats->attstattarget < 0)                               :1897
		 *         stats->attstattarget = default_statistics_target;
		 *
		 * Zero means the DBA turned this column off, and honouring it is not
		 * optional: writing statistics for such a column overrides an explicit
		 * instruction and hands the planner numbers somebody disabled. Reading
		 * the global default for every column, as this function did, ignored
		 * ALTER TABLE ... SET STATISTICS entirely.
		 */
		IF att.attstattarget = 0 THEN
			disabled := disabled + 1;
			CONTINUE;
		END IF;
		nbuckets := coalesce(att.attstattarget, deftarget);
		/*
		 * Has this column been written yet? The zone maps answer that and
		 * nothing else here.
		 *
		 * They used to answer null_frac as well --
		 * sum(null_count) / sum(value_count + null_count) -- and that was wrong
		 * after a DELETE. Those counts describe what was WRITTEN; deleting a row
		 * marks it dead without rewriting them, so the denominator keeps counting
		 * rows the table no longer holds. On 1,000 rows with 100 nulls, deleting
		 * the 301 rows holding one value leaves a true null_frac of 0.1431 and a
		 * zone-map null_frac of 0.1000, a 30% understatement that VACUUM does not
		 * heal. Worse than the size of the error: null_frac came from the zone
		 * maps while the most-common-value frequencies came from count(*), so the
		 * two were normalised against different populations and
		 * null_frac + sum(mcv_freqs) + rest = 1 -- the identity the planner's
		 * selectivity arithmetic rests on -- silently stopped holding.
		 *
		 * So the fraction is taken from the same read as everything else below,
		 * and the zone maps keep only the job they can still do exactly: telling
		 * us whether there are any row groups at all.
		 *
		 * column_index is the 0-based attribute position. attnum is stable
		 * across a dropped column, so attnum - 1 keeps pointing at the same
		 * column after a DROP COLUMN.
		 */
		PERFORM 1
			FROM pgcolumnar.zone_map z
			WHERE z.storage_id = sid
			  AND z.column_index = att.attnum - 1
			  AND z.vector_index = -1;

		CONTINUE WHEN NOT FOUND;	/* no zone map rows: nothing exact to say */

		/*
		 * n_distinct, the row count and the null count, by reading this column
		 * and nothing else. This is the whole point of the function: on the
		 * 3M x 20 fixture a projected single-column read costs 268 ms where
		 * core's whole-table sample costs 6,302 ms, because core's fixed
		 * 30,000-row sample lands in every row group and so decodes every column
		 * of the table.
		 *
		 * count(DISTINCT) ignores NULLs, which is what n_distinct means. The
		 * null count comes from the same scan so that it cannot disagree with the
		 * denominator the frequencies below are divided by.
		 */
		EXECUTE format('SELECT count(DISTINCT %I)::bigint, count(*)::bigint,'
					   '       count(*) FILTER (WHERE %I IS NULL)::bigint'
					   '  FROM %I.%I',
					   att.attname, att.attname, schname, relnm)
			INTO ndistinct, totalrows, nullcount;

		nullfrac := CASE WHEN totalrows > 0
						 THEN nullcount::double precision / totalrows::double precision
						 ELSE 0 END;

		/*
		 * Core's own convention, and the sign is load-bearing: positive is an
		 * absolute count, negative is the negated fraction of rows. analyze.c
		 * switches to the fraction once the distinct count passes 10% of the
		 * rows, on the grounds that such a column's cardinality tracks the table
		 * size rather than sitting at a fixed value. Mirror it rather than always
		 * writing the absolute count, or a column that is unique today reads as
		 * having a fixed cardinality once the table grows.
		 *
		 * Getting this backwards does not raise -- it produces plausible wrong
		 * estimates -- so it is asserted in test/analyze_function.sh against a
		 * fixture pinned to the absolute-count side of the rule.
		 */
		IF totalrows > 0 THEN
			IF ndistinct::double precision > 0.1 * totalrows::double precision THEN
				ndstat := -(ndistinct::double precision / totalrows::double precision);
			ELSE
				ndstat := ndistinct::double precision;
			END IF;
		ELSE
			ndstat := 0;
		END IF;

		/*
		 * Whether this type can be ordered at all. Hoisted out of the histogram
		 * test below because the most-common-value list needs the same answer:
		 * both order by the column, and a type with no btree opclass has no
		 * histogram in core either.
		 */
		orderable := EXISTS (SELECT 1 FROM pg_catalog.pg_type t
							 JOIN pg_catalog.pg_opclass oc ON oc.opcintype = t.oid
							 JOIN pg_catalog.pg_am am ON am.oid = oc.opcmethod
							 WHERE t.oid = att.atttypid AND am.amname = 'btree');

		/*
		 * most_common_vals and most_common_freqs (#414 slice 3b).
		 *
		 * The selection rule is core's, and reading a complete column removes
		 * most of it. analyze_mcv_list() opens with
		 *
		 *     if (samplerows == totalrows || totalrows <= 1.0)
		 *         return num_mcv;                        -- analyze.c:2995
		 *
		 * so the entire significance filter -- a continuity-corrected Wald
		 * interval over a hypergeometric variance -- is skipped when the whole
		 * table was read. That machinery exists to judge whether a SAMPLE
		 * frequency can be trusted; we do not sample, so the question does not
		 * arise and core's own answer is to keep the list. What remains:
		 *
		 *   only values appearing more than once are eligible  analyze.c:2549
		 *   the top default_statistics_target of those, by count analyze.c:2552
		 *   frequency = count / TOTAL rows, nulls included     analyze.c:2720
		 *
		 * That last one is the one that fails quietly. Dividing by the non-null
		 * count instead scales every frequency by 1/(1-null_frac): still ordered,
		 * still summing to less than one, still plausible, and wrong everywhere
		 * the column has nulls. test/analyze_function.sh pins it with a fixture
		 * that is one-tenth null, so the two denominators cannot agree.
		 *
		 * HAVING count(*) > 1 also reproduces core's unique-column case without a
		 * branch: when nothing repeats the aggregate is empty, array_agg returns
		 * NULL, and no MCV list is written -- which is what core does at
		 * analyze.c:2588 when nmultiple is zero.
		 *
		 * array_agg(...)::text rather than string_agg builds the array literal
		 * through the type's own output function, so quoting, embedded commas and
		 * braces are correct for text columns instead of being hand-assembled.
		 */
		mcvvals := NULL;
		mcvfreqs := NULL;
		nonnull  := 0;
		mcvrows  := 0;
		IF orderable THEN
			/*
			 * The same aggregation, split into the full group and the most-common
			 * slice of it, so it can also report how many ROWS each covers. The
			 * histogram below is built over the non-null rows the MCV list does
			 * NOT hold, and it has to know how many those are to place a bound at
			 * a position rather than at a fraction.
			 *
			 * Both counts come from this one aggregation rather than from the zone
			 * maps or a second scan, so the population the histogram is placed
			 * over is by construction the population the MCV list was taken from.
			 */
			EXECUTE format(
				'WITH g AS MATERIALIZED ('
				'       SELECT %I AS v, count(*)::bigint AS c'
				'         FROM %I.%I WHERE %I IS NOT NULL GROUP BY 1),'
				'     m AS MATERIALIZED ('
				'       SELECT v, c FROM g WHERE c > 1 ORDER BY c DESC, v LIMIT %s)'
				'SELECT (SELECT array_agg(v ORDER BY c DESC, v)::text FROM m),'
				'       (SELECT array_agg((c::double precision / %s::double precision)::real'
				'                         ORDER BY c DESC, v) FROM m),'
				'       (SELECT coalesce(sum(c), 0)::bigint FROM g),'
				'       (SELECT coalesce(sum(c), 0)::bigint FROM m)',
				att.attname, schname, relnm, att.attname, nbuckets, totalrows)
				INTO mcvvals, mcvfreqs, nonnull, mcvrows;
		END IF;

		/*
		 * histogram_bounds, whose ends are exact because the read is complete
		 * (#414 slice 3).
		 *
		 * percentile_disc over an array of fractions returns ACTUAL column
		 * values, one per fraction, in a single ordered pass. Fraction 1.0 is
		 * therefore the true maximum and 0.0 the true minimum, which is the
		 * whole gain: core samples, so a value held by one row in 500,000 is
		 * missed and every range estimate above the sampled maximum collapses.
		 * percentile_cont would interpolate and invent values the column does
		 * not contain, which is wrong for a histogram of stored data and wrong
		 * for any non-numeric type.
		 *
		 * Only for types that can be ordered. A column with no btree ordering
		 * has no histogram in core either, and ORDER BY would simply fail.
		 *
		 * The most-common values are EXCLUDED, which core does at analyze.c:2744
		 * and :2768-2799 by collapsing them out of the sorted array before
		 * building buckets. Keeping them in counts them twice in selectivity:
		 * eqsel takes the value's frequency from the MCV list, and the range
		 * estimators count it again inside whichever bucket holds it. Nothing
		 * raises -- the estimates are simply inflated for the values a skewed
		 * column repeats most, which is where estimates matter.
		 *
		 * The population and the bucket count therefore both shrink, and both
		 * have to. Core sizes the histogram from what is LEFT:
		 *
		 *     num_hist = ndistinct - num_mcv;
		 *     if (num_hist > num_bins) num_hist = num_bins + 1;
		 *     if (num_hist >= 2) { ... }              -- analyze.c:2744-2747
		 *
		 * so it emits between 2 and num_bins+1 bounds and none at all below two.
		 * Asking percentile_disc for a fixed default_statistics_target+1
		 * fractions regardless would repeat values once the remaining population
		 * is smaller than that -- a 150-distinct column with 100 most-common
		 * values has 50 left and would get 101 bounds, most of them duplicates.
		 * A histogram with repeated bounds describes buckets holding no rows,
		 * which is a shape core never emits.
		 */
		nmcv := coalesce(array_length(mcvfreqs, 1), 0);
		nremaining := ndistinct - nmcv;

		nv := nonnull - mcvrows;

		hist := NULL;
		IF att.attnum > 0
		   AND orderable
		   AND nremaining >= 2
		   AND nv > 1
		THEN
			/*
			 * least(nbuckets, nremaining - 1) fractions, so the bound count is
			 * least(nbuckets + 1, nremaining): core's cap, reached from below.
			 */
			nfrac := least(nbuckets, nremaining - 1);

			/*
			 * A bound is a POSITION, not a quantile, and the difference is not
			 * academic. core's compute_scalar_stats places bound i at
			 *
			 *     values[floor(i * (nvals - 1) / (num_hist - 1))]
			 *
			 * among the rows left after the most-common values are removed.
			 * percentile_disc resolves fraction p to index ceil(p * nv) - 1, which
			 * is a different index whenever frac(i*nv/nfrac) is small, and a
			 * different VALUE whenever that shift crosses a value boundary. On a
			 * column with many rows per distinct value the two agree and the
			 * distinction is invisible; on eleven distinct rows at a statistics
			 * target of 3 they disagree at the third bound, 8 against 7.
			 *
			 * So ask percentile_disc for the fractions that resolve to core's
			 * positions instead of for evenly spaced quantiles:
			 *
			 *     p_i = (floor(i * (nv - 1) / nfrac) + 0.5) / nv
			 *
			 * The half is load-bearing rather than decorative. The exact boundary
			 * (T + 1)/nv is a double, and nv up to a few million leaves roughly
			 * 1e-9 of slack in p*nv; landing a hair above T+1 makes ceil() return
			 * T+2 and takes the NEXT value. Half a row of margin cannot be crossed
			 * by that error, and any p in (T/nv, (T+1)/nv] resolves to T.
			 *
			 * nv is the count from the aggregation above, not a derived figure:
			 * deriving it as totalrows minus a null_frac read off the zone maps
			 * would put a rounded float in a position index.
			 */

			/*
			 * The exclusion is a literal list rather than a re-aggregation. The
			 * alternative -- recomputing the most-common set in a subquery -- is
			 * a third full pass over a column this function exists to read once,
			 * and it can disagree with the list actually written if the tie-break
			 * ever differs. format_type gives the element type without a typmod,
			 * which is what the array literal must be parsed against.
			 */
			EXECUTE format(
				'SELECT percentile_disc(
						 (SELECT array_agg(((floor(i::numeric * (%s - 1) / %s) + 0.5)
											/ %s)::double precision ORDER BY i)
							FROM generate_series(0, %s) i))
					   WITHIN GROUP (ORDER BY %I)::text
				   FROM %I.%I WHERE %I IS NOT NULL %s',
				nv, nfrac, nv, nfrac, att.attname, schname, relnm, att.attname,
				CASE WHEN mcvvals IS NULL THEN ''
					 ELSE format('AND %I <> ALL (%L::%s[])', att.attname, mcvvals,
								 format_type(att.atttypid, NULL))
				END)
				INTO hist;
		END IF;

		/*
		 * The casts are load-bearing. pg_restore_attribute_stats takes VARIADIC
		 * "any", so a mistyped argument is a WARNING and the value is dropped,
		 * not an error: attname must be text (attname is `name`) and null_frac
		 * must be real (the division yields double precision). Without these the
		 * call "succeeds" having stored nothing.
		 *
		 * histogram_bounds and most_common_vals are passed as text, which is what
		 * the function takes (attribute_stats.c:70,72): it parses each array
		 * literal against the column's own type. most_common_freqs is real[]
		 * (:71) -- a float8[] there is dropped with a WARNING, not an error.
		 *
		 * One call with typed NULLs rather than a branch per combination. A NULL
		 * argument is not written: each statistic is gated on PG_ARGISNULL
		 * (:162-163 for the MCV pair), so a typed NULL and an omitted argument
		 * mean the same thing. Four optional statistics would otherwise be
		 * sixteen call sites. The NULLs must still be TYPED -- an untyped NULL
		 * reaches VARIADIC "any" as `unknown` and is the mistyped-argument case
		 * these casts exist to avoid.
		 *
		 * most_common_vals and most_common_freqs are a pair: supplying one
		 * without the other is a WARNING and drops both (stats_check_arg_pair,
		 * :265). They are computed together above, so they are null together.
		 */
		PERFORM pg_catalog.pg_restore_attribute_stats(
			'schemaname', schname,
			'relname', relnm,
			'attname', att.attname::text,
			'inherited', false,
			'null_frac', nullfrac::real,
			'n_distinct', ndstat::real,
			'most_common_vals', mcvvals::text,
			'most_common_freqs', mcvfreqs::real[],
			'histogram_bounds', hist::text);

		seen := seen + 1;
	END LOOP;

	/*
	 * Collecting nothing is an error only when nothing ASKED us not to. A column
	 * at SET STATISTICS 0 is an instruction, and core does not raise for
	 * `ANALYZE t (col)` when col is disabled -- it collects nothing and returns.
	 * Without the second term this guard turned that instruction into an error
	 * whose hint blamed missing row groups, which is a different fault entirely
	 * and would send somebody looking at the storage.
	 */
	IF seen = 0 AND disabled = 0 THEN
		RAISE EXCEPTION 'pgcolumnar.analyze(): collected statistics for no columns of %', rel::text
			USING HINT = 'the table may have no written row groups yet';
	END IF;
END;
$$;

/*
 * pgcolumnar.partition_create_ahead (#1297)
 *
 * Make sure a RANGE-partitioned parent's partitions reach now() + ahead * width,
 * each `width` wide and contiguous with the highest existing upper bound (or,
 * with none, starting at now() aligned to the width), every one columnar.
 * Returns how many it created; a second call creates nothing.
 *
 * NEVER AN EXCLUSIVE LOCK ON THE PARENT. CREATE TABLE ... PARTITION OF takes
 * ACCESS EXCLUSIVE on the parent; a standalone CREATE followed by ATTACH takes
 * SHARE UPDATE EXCLUSIVE, which readers and writers pass. Measured on PG 15..19
 * (#1297 note 1).
 *
 * A DEFAULT partition is refused (55000): it rules out DETACH CONCURRENTLY for
 * the drop half, and a parent managed half-way is worse than one refused.
 */
CREATE FUNCTION pgcolumnar.partition_create_ahead(parent regclass, width interval, ahead int)
	RETURNS int
	LANGUAGE plpgsql
	SET search_path = pg_catalog, pg_temp
	AS $create_ahead$
DECLARE
	strat     "char";
	natts     int;
	defid     oid;
	keytype   regtype;
	nsp       name;
	rel       name;
	lo        timestamptz;
	hi        timestamptz;
	target    timestamptz;
	ts_lo     timestamp;
	ts_hi     timestamp;
	ts_target timestamp;
	child     text;
	prefix    text;
	n         int := 0;
	probe_ts  timestamp;
	probe_tz  timestamptz;
	made      int := 0;
BEGIN
	IF width IS NULL OR width <= interval '0' OR ahead IS NULL OR ahead < 0 THEN
		RAISE EXCEPTION 'width must be positive and ahead not negative'
			USING ERRCODE = '22023';
	END IF;
	SELECT pt.partstrat, pt.partnatts, pt.partdefid, a.atttypid::regtype
	  INTO strat, natts, defid, keytype
	  FROM pg_partitioned_table pt
	  JOIN pg_attribute a ON a.attrelid = pt.partrelid AND a.attnum = pt.partattrs[0]
	 WHERE pt.partrelid = parent;
	IF NOT FOUND OR strat <> 'r' OR natts <> 1 THEN
		RAISE EXCEPTION '% is not RANGE-partitioned on one column', parent
			USING ERRCODE = '55000';
	END IF;
	IF keytype NOT IN ('timestamptz'::regtype, 'timestamp'::regtype) THEN
		RAISE EXCEPTION '% is partitioned on %, not on a timestamp', parent, keytype
			USING ERRCODE = '55000';
	END IF;
	IF defid <> 0 THEN
		RAISE EXCEPTION '% has a DEFAULT partition', parent
			USING ERRCODE = '55000',
			      HINT = 'A DEFAULT partition rules out DETACH CONCURRENTLY, so the partition lifecycle refuses to manage this table.';
	END IF;
	-- An upper bound of MAXVALUE leaves no room ahead, and the bound reader below
	-- would not see it: the first new partition would overlap it (#1308 review).
	IF EXISTS (SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
			   WHERE i.inhparent = parent
				 AND pg_get_expr(c.relpartbound, c.oid) ~ 'TO \(MAXVALUE\)') THEN
		RAISE EXCEPTION '% has a partition bounded by MAXVALUE', parent
			USING ERRCODE = '55000',
			      HINT = 'Nothing can be created after a partition that reaches MAXVALUE.';
	END IF;

	SELECT n.nspname, c.relname INTO nsp, rel
	  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = parent;
	-- Names are the parent's name and a 16-byte date suffix, and must fit in 63
	-- bytes: the prefix is shortened, by characters, until they do, so the date
	-- always survives (#1308 review: a 63-character parent's second partition
	-- truncated onto its first).
	prefix := rel;
	WHILE octet_length(prefix) + 16 > 63 LOOP
		prefix := left(prefix, -1);
	END LOOP;

	/*
	 * A `timestamp` key holds wall-clock time with no zone, so it is reasoned
	 * about in its own type: its bounds are read as timestamp, "now" is the
	 * session's localtimestamp, and the bounds written are never converted.
	 * Reading them as timestamptz in one zone and writing them in another left
	 * a gap of the zone's offset between partitions (America/New_York: four
	 * hours), and aligned "daily" partitions to UTC's midnight, not the key's.
	 */
	IF keytype = 'timestamp'::regtype THEN
		SELECT max((regexp_match(pg_get_expr(c.relpartbound, c.oid),
		                         'TO \(''([^'']+)''\)'))[1]::timestamp)
		  INTO ts_lo
		  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
		 WHERE i.inhparent = parent;
		IF ts_lo IS NULL THEN
			IF extract(month FROM width) <> 0 OR extract(year FROM width) <> 0 THEN
				ts_lo := date_trunc('month', localtimestamp);
			ELSE
				ts_lo := date_bin(width, localtimestamp, timestamp '2001-01-01');
			END IF;
		END IF;
		ts_target := localtimestamp + ahead * width;
		probe_ts := ts_lo;
		WHILE probe_ts <= ts_target AND n <= 1000 LOOP
			n := n + 1;
			probe_ts := probe_ts + width;
		END LOOP;
		-- Counted before any DDL: one call creating thousands of partitions holds
		-- their locks to the end and can fail on the lock table with nothing made
		-- (#1308 review: '1 second' x 20000 ran 6 minutes, then out of shared memory).
		IF n > 1000 THEN
			RAISE EXCEPTION 'partition_create_ahead would create more than 1000 partitions of %', parent
				USING ERRCODE = '22023',
				      HINT = 'Use a wider width, a shorter lead, or several calls.';
		END IF;
		WHILE ts_lo <= ts_target LOOP
			ts_hi := ts_lo + width;
			child := prefix || '_' || to_char(ts_lo, 'YYYYMMDD"T"HH24MISS');
			EXECUTE format('CREATE TABLE %I.%I (LIKE %s INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING GENERATED) USING pgcolumnar',
			               nsp, child, parent);
			EXECUTE format('ALTER TABLE %s ATTACH PARTITION %I.%I FOR VALUES FROM (%L) TO (%L)',
			               parent, nsp, child, ts_lo::text, ts_hi::text);
			made := made + 1;
			ts_lo := ts_hi;
		END LOOP;
		RETURN made;
	END IF;

	SELECT max((regexp_match(pg_get_expr(c.relpartbound, c.oid),
	                         'TO \(''([^'']+)''\)'))[1]::timestamptz)
	  INTO lo
	  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
	 WHERE i.inhparent = parent;
	IF lo IS NULL THEN
		IF extract(month FROM width) <> 0 OR extract(year FROM width) <> 0 THEN
			lo := date_trunc('month', now());
		ELSE
			lo := date_bin(width, now(), timestamptz '2001-01-01');
		END IF;
	END IF;
	target := now() + ahead * width;
	probe_tz := lo;
	WHILE probe_tz <= target AND n <= 1000 LOOP
		n := n + 1;
		probe_tz := probe_tz + width;
	END LOOP;
	-- Counted before any DDL: one call creating thousands of partitions holds
	-- their locks to the end and can fail on the lock table with nothing made
	-- (#1308 review: '1 second' x 20000 ran 6 minutes, then out of shared memory).
	IF n > 1000 THEN
		RAISE EXCEPTION 'partition_create_ahead would create more than 1000 partitions of %', parent
			USING ERRCODE = '22023',
			      HINT = 'Use a wider width, a shorter lead, or several calls.';
	END IF;
	WHILE lo <= target LOOP
		hi := lo + width;
		child := prefix || '_' || to_char(lo AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS');
		EXECUTE format('CREATE TABLE %I.%I (LIKE %s INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING GENERATED) USING pgcolumnar',
		               nsp, child, parent);
		EXECUTE format('ALTER TABLE %s ATTACH PARTITION %I.%I FOR VALUES FROM (%L) TO (%L)',
		               parent, nsp, child, lo::text, hi::text);
		made := made + 1;
		lo := hi;
	END LOOP;
	RETURN made;
END;
$create_ahead$;

/*
 * pgcolumnar.partition_drop_plan (#1297)
 *
 * The statements that retire every partition of a RANGE-partitioned parent
 * whose whole range is older than cutoff (its upper bound at or before it), in
 * range order: ALTER TABLE ... DETACH PARTITION ... CONCURRENTLY, then DROP
 * TABLE (DROP FOREIGN TABLE for one tier_partition moved to Parquet, whose file
 * stays). A partition a concurrent detach left pending gets DETACH ... FINALIZE
 * instead. A partition reaching MAXVALUE is never old; one from MINVALUE is, once
 * its upper bound is past. A partition that is itself partitioned is listed whole.
 *
 * RETURNED, NOT RUN. DETACH ... CONCURRENTLY is refused inside any function, DO
 * block or procedure, on every major (#1297 note 1), and a plain DETACH or a
 * DROP of an attached partition takes ACCESS EXCLUSIVE on the parent, which
 * blocks every query on the table. The caller runs each statement at top level:
 *
 *     SELECT * FROM pgcolumnar.partition_drop_plan('metrics', now() - interval '90 days') \gexec
 *
 * THE CUTOFF'S TYPE IS THE KEY'S (jdatcmd, #1297). A timestamp bound compared
 * with a timestamptz cutoff converts in the session's zone, so one call could
 * plan differently from two sessions. Two overloads, each refusing the other
 * type of key (42804); an untyped literal resolves to timestamptz.
 *
 * THE DROP REFUSES WHILE THE PARTITION IS ATTACHED (jdatcmd, #1331 review). Any
 * DETACH can fail -- a lock_timeout, a cancelled concurrent wait, a permission
 * error -- and \gexec carries on unless ON_ERROR_STOP is set. A plain DROP of an
 * attached partition would then take ACCESS EXCLUSIVE on the parent. So each
 * drop is a DO block that raises 55000 while the partition is still in
 * pg_inherits, a pending one included; the next plan's FINALIZE picks that up.
 * A DROP may run inside DO; only DETACH ... CONCURRENTLY must be top-level.
 *
 * The same refusals as partition_create_ahead, with the same codes: a DEFAULT
 * partition rules out the concurrent detach, and is refused rather than dropped
 * with the exclusive one. No ownership check: this reads catalogs the caller
 * can see, and DETACH checks ownership when the statements run.
 */
CREATE FUNCTION pgcolumnar.partition_drop_candidates(parent regclass, cutoff_type regtype)
	RETURNS TABLE (part regclass, pending boolean, foreign_table boolean, upper_bound text)
	LANGUAGE plpgsql STABLE
	SET search_path = pg_catalog, pg_temp
	AS $drop_candidates$
DECLARE
	strat   "char";
	natts   int;
	defid   oid;
	keytype regtype;
BEGIN
	SELECT pt.partstrat, pt.partnatts, pt.partdefid, a.atttypid::regtype
	  INTO strat, natts, defid, keytype
	  FROM pg_partitioned_table pt
	  JOIN pg_attribute a ON a.attrelid = pt.partrelid AND a.attnum = pt.partattrs[0]
	 WHERE pt.partrelid = parent;
	IF NOT FOUND OR strat <> 'r' OR natts <> 1 THEN
		RAISE EXCEPTION '% is not RANGE-partitioned on one column', parent
			USING ERRCODE = '55000';
	END IF;
	IF keytype NOT IN ('timestamptz'::regtype, 'timestamp'::regtype) THEN
		RAISE EXCEPTION '% is partitioned on %, not on a timestamp', parent, keytype
			USING ERRCODE = '55000';
	END IF;
	IF defid <> 0 THEN
		RAISE EXCEPTION '% has a DEFAULT partition', parent
			USING ERRCODE = '55000',
			      HINT = 'A DEFAULT partition rules out DETACH CONCURRENTLY, so the partition lifecycle refuses to manage this table.';
	END IF;
	IF keytype <> cutoff_type THEN
		RAISE EXCEPTION '% is partitioned on %, so its cutoff must be a %, not a %',
			parent, keytype, keytype, cutoff_type
			USING ERRCODE = '42804',
			      HINT = CASE WHEN keytype = 'timestamp'::regtype
			                  THEN 'Pass a timestamp, for example localtimestamp - interval ''90 days''.'
			                  ELSE 'Pass a timestamptz, for example now() - interval ''90 days''.' END;
	END IF;
	-- MAXVALUE above has no upper bound to read, and is never old
	RETURN QUERY
		SELECT b.part, b.pending, b.foreign_table, b.upper_bound
		  FROM (SELECT c.oid::regclass AS part, i.inhdetachpending AS pending,
		               c.relkind = 'f' AS foreign_table,
		               (regexp_match(pg_get_expr(c.relpartbound, c.oid),
		                             'TO \(''([^'']+)''\)'))[1] AS upper_bound
		          FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
		         WHERE i.inhparent = parent) b
		 WHERE b.upper_bound IS NOT NULL;
END
$drop_candidates$;

CREATE FUNCTION pgcolumnar.partition_drop_plan(parent regclass, cutoff timestamptz)
	RETURNS SETOF text
	LANGUAGE plpgsql STABLE
	SET search_path = pg_catalog, pg_temp
	AS $drop_plan_tz$
DECLARE
	p record;
BEGIN
	IF cutoff IS NULL THEN
		RAISE EXCEPTION 'cutoff must not be NULL' USING ERRCODE = '22023';
	END IF;
	FOR p IN
		SELECT d.* FROM pgcolumnar.partition_drop_candidates(parent, 'timestamptz') d
		 WHERE d.upper_bound::timestamptz <= cutoff
		 ORDER BY d.upper_bound::timestamptz
	LOOP
		RETURN NEXT format('ALTER TABLE %s DETACH PARTITION %s %s', parent, p.part,
		                   CASE WHEN p.pending THEN 'FINALIZE' ELSE 'CONCURRENTLY' END);
		-- the body as a literal, not dollar-quoted: no name can end it (#1331 review)
		RETURN NEXT format('DO %L',
		                   format('BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_inherits WHERE inhrelid = %L::regclass) THEN RAISE EXCEPTION %L, %L USING ERRCODE = %L; END IF; DROP %s %s; END',
		                          p.part::text,
		                          '% is still attached to its parent; its DETACH did not finish',
		                          p.part::text, '55000',
		                          CASE WHEN p.foreign_table THEN 'FOREIGN TABLE' ELSE 'TABLE' END, p.part));
	END LOOP;
END
$drop_plan_tz$;

CREATE FUNCTION pgcolumnar.partition_drop_plan(parent regclass, cutoff timestamp)
	RETURNS SETOF text
	LANGUAGE plpgsql STABLE
	SET search_path = pg_catalog, pg_temp
	AS $drop_plan_ts$
DECLARE
	p record;
BEGIN
	IF cutoff IS NULL THEN
		RAISE EXCEPTION 'cutoff must not be NULL' USING ERRCODE = '22023';
	END IF;
	FOR p IN
		SELECT d.* FROM pgcolumnar.partition_drop_candidates(parent, 'timestamp') d
		 WHERE d.upper_bound::timestamp <= cutoff
		 ORDER BY d.upper_bound::timestamp
	LOOP
		RETURN NEXT format('ALTER TABLE %s DETACH PARTITION %s %s', parent, p.part,
		                   CASE WHEN p.pending THEN 'FINALIZE' ELSE 'CONCURRENTLY' END);
		-- the body as a literal, not dollar-quoted: no name can end it (#1331 review)
		RETURN NEXT format('DO %L',
		                   format('BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_inherits WHERE inhrelid = %L::regclass) THEN RAISE EXCEPTION %L, %L USING ERRCODE = %L; END IF; DROP %s %s; END',
		                          p.part::text,
		                          '% is still attached to its parent; its DETACH did not finish',
		                          p.part::text, '55000',
		                          CASE WHEN p.foreign_table THEN 'FOREIGN TABLE' ELSE 'TABLE' END, p.part));
	END LOOP;
END
$drop_plan_ts$;
/* ---------------------------------------------------------------------------
 * pgcolumnar.retired_group (#1294)
 *
 * The row groups pgcolumnar.expire retired for retention, one row each, written
 * in expire's own transaction. An incremental rollup keeps its buckets when
 * retention drops their raw rows (the owner's decision on #1294), so its
 * refresh must tell a group retired by retention from one deleted or
 * rewritten; this is where it looks. Nothing but expire writes here: a
 * compaction's retired groups are a change to the data, not retention.
 * deleted_count is the group's deleted count when expire retired it: rows
 * deleted after a rollup's last refresh cannot be recomputed once the group is
 * gone, and this is how the refresh sees that (found on #1328).
 * Deleted with the storage's other metadata.
 * ------------------------------------------------------------------------- */

CREATE TABLE pgcolumnar.retired_group (
	storage_id bigint NOT NULL,
	group_number bigint NOT NULL,
	retired_at timestamptz NOT NULL,
	deleted_count bigint NOT NULL
);
CREATE UNIQUE INDEX retired_group_pkey
	ON pgcolumnar.retired_group USING btree (storage_id, group_number);

/* ---------------------------------------------------------------------------
 * pgcolumnar.column_extent (#1304 option 5)
 *
 * A bound on every value of a column, for the planner's bucket estimate: one
 * index lookup instead of every row group's zone map. Rows are inserted and
 * deleted, never updated: each committing writer adds one row (complete =
 * false) for what it flushed, and a recompute over every visible zone map
 * replaces the rows it saw with one complete row. The planner trusts the rows
 * only when a complete one is visible; then their fold is a true bound.
 * minimum and maximum are encoded as the zone maps encode them; NULL when the
 * row saw no value. No GRANT: the values are column data, like zone_map's.
 * ------------------------------------------------------------------------- */
CREATE TABLE pgcolumnar.column_extent (
	storage_id bigint NOT NULL,
	column_index smallint NOT NULL,
	complete bool NOT NULL,
	minimum bytea,
	maximum bytea
);
CREATE INDEX column_extent_pkey
	ON pgcolumnar.column_extent USING btree (storage_id, column_index);

CREATE FUNCTION pgcolumnar.column_extent(rel regclass, col name)
	RETURNS TABLE (complete bool, minimum text, maximum text)
	LANGUAGE C STRICT STABLE
	AS 'MODULE_PATHNAME', 'pgcolumnar_column_extent';

COMMENT ON FUNCTION pgcolumnar.column_extent(regclass, name)
	IS 'the bound on a column the planner reads from pgcolumnar.column_extent, printed by the column type; no row when it would read the zone maps instead (#1304); requires SELECT on the table';


/* ---------------------------------------------------------------------------
 * Incremental rollups (#1294).
 *
 * A rollup is a VIEW over a partials table. The partials table holds, per
 * (bucket, group_by...) key, the parts each aggregate combines from: count, sum,
 * min, max, and for avg a sum and a count. The view turns those into the
 * aggregates. refresh_rollup folds the rows of row groups that are new since the
 * last refresh, and recomputes the buckets of groups that were deleted from or
 * rewritten since (phase 2, dirty buckets), in ONE statement: the group list it
 * reads, the rows it folds and the groups it records all come from one snapshot
 * (#1294 point 1).
 *
 * Each recorded group carries the time range of the rows folded from it,
 * because a group that is gone has no zone maps left to ask. Retention keeps
 * history: the ranges of the groups expire retired collect in the rollup's
 * history, and a refresh that would recompute a bucket in it is refused.
 *
 * pgcolumnar.rollup and pgcolumnar.rollup_group are readable and writable by
 * anyone, row-level security limiting each role to the rollups it owns: the
 * refresh runs as its caller and writes them in that statement.
 * ------------------------------------------------------------------------- */
CREATE TABLE pgcolumnar.rollup (
	rollup regclass PRIMARY KEY,       -- the view
	partials regclass NOT NULL,        -- the table behind it
	source regclass NOT NULL,
	source_storage bigint NOT NULL,    -- the source's storage id when recorded
	source_oid oid NOT NULL,           -- and its OID: a dump writes it as a number (#1377)
	ts_column name NOT NULL,
	width interval NOT NULL,
	origin timestamptz NOT NULL,
	group_by name[] NOT NULL,
	agg_functions text[] NOT NULL,
	agg_columns name[] NOT NULL,
	-- the time ranges of the groups retention retired: their buckets keep rows
	-- the source no longer has, so they are never recomputed
	history tstzmultirange NOT NULL DEFAULT '{}',
	history_has_null boolean NOT NULL DEFAULT false
);

CREATE TABLE pgcolumnar.rollup_group (
	rollup regclass NOT NULL,
	group_number bigint NOT NULL,
	row_count bigint NOT NULL,
	deleted_count bigint NOT NULL,
	-- the range of ts_column over the rows folded; NULL when none had a ts
	ts_min timestamptz,
	ts_max timestamptz,
	ts_has_null boolean NOT NULL,
	PRIMARY KEY (rollup, group_number)
);

ALTER TABLE pgcolumnar.rollup ENABLE ROW LEVEL SECURITY;
ALTER TABLE pgcolumnar.rollup_group ENABLE ROW LEVEL SECURITY;
CREATE POLICY rollup_owner ON pgcolumnar.rollup
	USING (pg_has_role(current_user,
					   (SELECT c.relowner FROM pg_class c WHERE c.oid = rollup), 'USAGE'));
CREATE POLICY rollup_group_owner ON pgcolumnar.rollup_group
	USING (pg_has_role(current_user,
					   (SELECT c.relowner FROM pg_class c WHERE c.oid = rollup), 'USAGE'));
GRANT SELECT, INSERT, UPDATE, DELETE ON pgcolumnar.rollup, pgcolumnar.rollup_group TO PUBLIC;
/*
 * A rollup's definition is dumped (#1377); the row groups it folded are not, as
 * they name storage a restore replaces. Every column holds a regclass or names,
 * which a restore resolves, except source_oid: written as the old number, it no
 * longer matches the restored source, and refresh_rollup refuses until
 * rebase_rollup records the groups again. The storage id cannot tell them apart:
 * storageid_seq restarts with the extension, so a restored source can get the
 * one recorded, and refresh would fold its rows into the restored partials twice.
 */
SELECT pg_catalog.pg_extension_config_dump('pgcolumnar.rollup', '');

-- The view depends on its source, and the partials on the view, recorded by this
-- trigger whenever a rollup row is written, a restore included (#1376, #1377).
CREATE FUNCTION pgcolumnar.rollup_record_dependencies() RETURNS trigger
	LANGUAGE C AS 'MODULE_PATHNAME', 'pgcolumnar_rollup_record_dependencies';
CREATE TRIGGER rollup_dependencies
	AFTER INSERT OR UPDATE OF rollup, partials, source ON pgcolumnar.rollup
	FOR EACH ROW EXECUTE FUNCTION pgcolumnar.rollup_record_dependencies();

/*
 * The source's row groups, with each one's deleted count. SECURITY DEFINER
 * because row_group and delete_vector carry no GRANT; the caller must be able to
 * SELECT the source. STABLE, so it reads with the calling statement's snapshot
 * and refresh_rollup keeps to one snapshot.
 */
-- The item pointer the access method gives a row number (#1319 review).
CREATE FUNCTION pgcolumnar.row_tid(row_number bigint)
	RETURNS tid
	LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE
	AS 'MODULE_PATHNAME', 'pgcolumnar_row_tid';

CREATE FUNCTION pgcolumnar.rollup_source_groups(source regclass)
	RETURNS TABLE (group_number bigint, first_row_number bigint, row_count bigint,
				   deleted_count bigint)
	LANGUAGE plpgsql STABLE SECURITY DEFINER
	SET search_path = pg_catalog, pg_temp
AS $rsg$
DECLARE
	-- once, not per catalog row: in a filter on a scan of the catalog the call
	-- runs for every row, and each call reads the source's first page (#1614)
	sid bigint;
BEGIN
	PERFORM pgcolumnar.require_caller_select(source);
	sid := pgcolumnar.get_storage_id(source);
	RETURN QUERY
		SELECT g.group_number, g.first_row_number, g.row_count,
			   coalesce(d.deleted_count, 0)::bigint
		  FROM pgcolumnar.row_group g
		  LEFT JOIN pgcolumnar.delete_vector d
			ON d.storage_id = g.storage_id AND d.group_number = g.group_number
		 WHERE g.storage_id = sid;
END
$rsg$;

/*
 * The source's row groups that retention retired (pgcolumnar.retired_group,
 * written only by expire, #1314). SECURITY DEFINER because that catalog carries
 * no GRANT; the caller must be able to SELECT the source.
 */
CREATE FUNCTION pgcolumnar.rollup_source_retired(source regclass)
	RETURNS TABLE (group_number bigint, deleted_count bigint)
	LANGUAGE plpgsql STABLE SECURITY DEFINER
	SET search_path = pg_catalog, pg_temp
AS $rsr$
DECLARE
	-- once, not per catalog row: in a filter on a scan of the catalog the call
	-- runs for every row, and each call reads the source's first page (#1614)
	sid bigint;
BEGIN
	PERFORM pgcolumnar.require_caller_select(source);
	sid := pgcolumnar.get_storage_id(source);
	RETURN QUERY
		SELECT r.group_number, r.deleted_count FROM pgcolumnar.retired_group r
		 WHERE r.storage_id = sid;
END
$rsr$;

CREATE FUNCTION pgcolumnar.create_rollup(rollup name, source regclass, ts_column name,
										 width interval, origin timestamptz,
										 group_by name[], agg_functions text[],
										 agg_columns name[],
										 access_method name DEFAULT 'pgcolumnar')
	RETURNS regclass
	LANGUAGE plpgsql
	SET search_path = pg_catalog, pg_temp
AS $cr$
DECLARE
	nsp		name;
	tstype	regtype;
	cols	text := '';
	vcols	text := '';
	gcols	text := '';
	i		int;
	fn		text;
	col		name;
	ctype	regtype;
	ptype	text;
	partials text;
	view_oid regclass;
BEGIN
	IF width IS NULL OR width <= interval '0' OR extract(month FROM width) <> 0
	   OR extract(year FROM width) <> 0 THEN
		RAISE EXCEPTION 'a rollup width must be positive and have no months or years'
			USING ERRCODE = 'invalid_parameter_value',
				  DETAIL = 'date_bin, which buckets the rows, takes no width in months.';
	END IF;
	IF agg_functions IS NULL OR agg_columns IS NULL
	   OR cardinality(agg_functions) = 0
	   OR cardinality(agg_functions) <> cardinality(agg_columns) THEN
		RAISE EXCEPTION 'agg_functions and agg_columns must be non-empty and of the same length'
			USING ERRCODE = 'invalid_parameter_value';
	END IF;
	SELECT a.atttypid::regtype INTO tstype FROM pg_attribute a
	 WHERE a.attrelid = source AND a.attname = ts_column AND NOT a.attisdropped;
	IF tstype IS DISTINCT FROM 'timestamptz'::regtype THEN
		RAISE EXCEPTION 'column "%" of % is not a timestamptz column', ts_column, source
			USING ERRCODE = 'invalid_parameter_value';
	END IF;

	-- Each aggregate checked against the five that combine across partial
	-- results, and each column against the source: structured input, never SQL
	-- text (#1294 point 11).
	FOR i IN 1 .. cardinality(agg_functions) LOOP
		fn := agg_functions[i];
		col := agg_columns[i];
		IF fn NOT IN ('count', 'sum', 'min', 'max', 'avg') THEN
			RAISE EXCEPTION '"%" is not an aggregate a rollup can combine', fn
				USING ERRCODE = 'invalid_parameter_value',
					  HINT = 'Use count, sum, min, max or avg.';
		END IF;
		IF col = '*' THEN
			IF fn <> 'count' THEN
				RAISE EXCEPTION '* goes only with count, not %', fn
					USING ERRCODE = 'invalid_parameter_value';
			END IF;
			cols := cols || ', count bigint NOT NULL';
			vcols := vcols || ', count';
			CONTINUE;
		END IF;
		SELECT a.atttypid::regtype INTO ctype FROM pg_attribute a
		 WHERE a.attrelid = source AND a.attname = col AND a.attnum > 0 AND NOT a.attisdropped;
		IF ctype IS NULL THEN
			RAISE EXCEPTION 'column "%" does not exist in %', col, source
				USING ERRCODE = 'invalid_parameter_value';
		END IF;
		ptype := CASE WHEN ctype IN ('int2'::regtype, 'int4'::regtype, 'int8'::regtype,
									  'numeric'::regtype) THEN 'numeric'
					  WHEN ctype IN ('float4'::regtype, 'float8'::regtype) THEN 'float8'
					  ELSE NULL END;
		IF fn IN ('sum', 'avg') AND ptype IS NULL THEN
			RAISE EXCEPTION '% of a % column cannot be folded', fn, ctype
				USING ERRCODE = 'invalid_parameter_value';
		END IF;
		CASE fn
			WHEN 'count' THEN
				cols := cols || format(', %I bigint NOT NULL', 'count_' || col);
				vcols := vcols || format(', %I', 'count_' || col);
			WHEN 'sum' THEN
				cols := cols || format(', %I %s', 'sum_' || col, ptype);
				vcols := vcols || format(', %I', 'sum_' || col);
			WHEN 'min', 'max' THEN
				cols := cols || format(', %I %s', fn || '_' || col, ctype);
				vcols := vcols || format(', %I', fn || '_' || col);
			WHEN 'avg' THEN
				cols := cols || format(', %I %s, %I bigint NOT NULL', 'avg_' || col || '__sum', ptype,
									   'avg_' || col || '__n');
				vcols := vcols || format(', (%I / nullif(%I, 0))::%s AS %I', 'avg_' || col || '__sum',
										 'avg_' || col || '__n', ptype, 'avg_' || col);
		END CASE;
	END LOOP;

	IF group_by IS NOT NULL THEN
		FOREACH col IN ARRAY group_by LOOP
			SELECT a.atttypid::regtype INTO ctype FROM pg_attribute a
			 WHERE a.attrelid = source AND a.attname = col AND a.attnum > 0 AND NOT a.attisdropped;
			IF ctype IS NULL THEN
				RAISE EXCEPTION 'group_by column "%" does not exist in %', col, source
					USING ERRCODE = 'invalid_parameter_value';
			END IF;
			gcols := gcols || format(', %I %s', col, ctype);
		END LOOP;
	END IF;

	-- In the source's schema: this function's search_path is pg_catalog, so
	-- current_schema() would name the system schema, not the caller's.
	SELECT n.nspname INTO nsp FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
	 WHERE c.oid = source;
	partials := rollup || '__partials';
	EXECUTE format('CREATE TABLE %I.%I (bucket timestamptz%s%s) USING %I',
				   nsp, partials, gcols, cols, access_method);
	EXECUTE format('CREATE VIEW %I.%I AS SELECT bucket%s%s FROM %I.%I',
				   nsp, rollup,
				   (SELECT coalesce(string_agg(format(', %I', g), ''), '') FROM unnest(group_by) g),
				   vcols, nsp, partials);
	view_oid := format('%I.%I', nsp, rollup)::regclass;
	INSERT INTO pgcolumnar.rollup VALUES
		(view_oid, format('%I.%I', nsp, partials)::regclass, source,
		 pgcolumnar.get_storage_id(source), source::oid, ts_column, width, origin,
		 coalesce(group_by, '{}'), agg_functions, agg_columns);
	RETURN view_oid;
END
$cr$;

COMMENT ON FUNCTION pgcolumnar.create_rollup(name, regclass, name, interval, timestamptz, name[], text[], name[], name)
	IS 'create an incremental rollup: a view of date_bin(width, ts_column, origin) buckets per group_by key, with count, sum, min, max or avg per (agg_functions, agg_columns); fill it with refresh_rollup (#1294)';

/*
 * The fold, one statement for one snapshot. Built per rollup from its catalog
 * row: every identifier through %I and every value through %L.
 */
CREATE FUNCTION pgcolumnar.refresh_rollup(rollup regclass)
	RETURNS bigint
	LANGUAGE plpgsql
	SET search_path = pg_catalog, pg_temp
AS $rr$
DECLARE
	r		pgcolumnar.rollup;
	changed	text;
	keys	text := '';
	keyeq	text := '';
	rowcols	text;
	rcols	name[];
	dsel	text := '';
	icols	text := '';
	isel	text := '';
	uset	text := '';
	i		int;
	fn		text;
	col		name;
	c		text;
	g		name;
	folded	bigint;
	refused	boolean;
	wabs	interval;
BEGIN
	-- A REPEATABLE READ caller's snapshot predates the lock below, so a refresh
	-- that waited for another would fold the same groups again.
	IF current_setting('transaction_isolation') <> 'read committed' THEN
		RAISE EXCEPTION 'refresh_rollup runs only at READ COMMITTED, not %',
			upper(current_setting('transaction_isolation'))
			USING ERRCODE = 'invalid_transaction_state';
	END IF;
	SELECT * INTO r FROM pgcolumnar.rollup x WHERE x.rollup = refresh_rollup.rollup;
	IF NOT FOUND THEN
		RAISE EXCEPTION '% is not a rollup', rollup USING ERRCODE = 'undefined_object';
	END IF;
	-- Self-conflicting, so two refreshes run one after the other; readers pass.
	EXECUTE format('LOCK TABLE %s IN SHARE UPDATE EXCLUSIVE MODE', r.partials);

	IF r.source::oid <> r.source_oid THEN
		RAISE EXCEPTION 'the source of rollup % is not the table its row groups were recorded from', rollup
			USING ERRCODE = 'object_not_in_prerequisite_state',
				  DETAIL = 'The rollup was restored from a dump, which does not carry the row groups it folded.',
				  HINT = 'If the rollup is current, pgcolumnar.rebase_rollup records the source''s row groups.';
	END IF;
	IF pgcolumnar.get_storage_id(r.source) <> r.source_storage THEN
		RAISE EXCEPTION 'the source of rollup % was rewritten since its last refresh', rollup
			USING ERRCODE = 'object_not_in_prerequisite_state',
				  HINT = 'If the rollup is current, pgcolumnar.rebase_rollup records the source''s new row groups.';
	END IF;
	-- The width as plain seconds: a timestamptz plus '1 day' is a calendar day
	-- in the session's zone, 23 or 25 hours across a DST change, while date_bin
	-- counts a day as 24 hours.
	wabs := make_interval(secs => extract(epoch FROM r.width)::float8);

	FOREACH g IN ARRAY r.group_by LOOP
		keys := keys || format(', %I', g);
		keyeq := keyeq || format(' AND p.%I IS NOT DISTINCT FROM d.%I', g, g);
	END LOOP;
	rcols := ARRAY[r.ts_column];
	FOR i IN 1 .. cardinality(r.agg_functions) LOOP
		fn := r.agg_functions[i];
		col := r.agg_columns[i];
		IF col = '*' THEN
			dsel := dsel || ', count(*) AS count';
			icols := icols || ', count';
			isel := isel || ', d.count';
			uset := uset || ', count = p.count + d.count';
			CONTINUE;
		END IF;
		IF NOT col = ANY (rcols) THEN rcols := rcols || col; END IF;
		CASE fn
			WHEN 'count' THEN
				c := 'count_' || col;
				dsel := dsel || format(', count(%I) AS %I', col, c);
				uset := uset || format(', %I = p.%I + d.%I', c, c, c);
			WHEN 'sum' THEN
				c := 'sum_' || col;
				dsel := dsel || format(', sum(%I)::%s AS %I', col,
									   (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
										 WHERE a.attrelid = r.partials AND a.attname = c), c);
				uset := uset || format(', %I = CASE WHEN p.%I IS NULL THEN d.%I WHEN d.%I IS NULL THEN p.%I ELSE p.%I + d.%I END',
									   c, c, c, c, c, c, c);
			WHEN 'min', 'max' THEN
				c := fn || '_' || col;
				dsel := dsel || format(', %s(%I) AS %I', fn, col, c);
				uset := uset || format(', %I = %s(p.%I, d.%I)', c,
									   CASE fn WHEN 'min' THEN 'least' ELSE 'greatest' END, c, c);
			WHEN 'avg' THEN
				c := 'avg_' || col || '__sum';
				dsel := dsel || format(', sum(%I)::%s AS %I, count(%I) AS %I', col,
									   (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
										 WHERE a.attrelid = r.partials AND a.attname = c), c,
									   col, 'avg_' || col || '__n');
				uset := uset || format(', %I = CASE WHEN p.%I IS NULL THEN d.%I WHEN d.%I IS NULL THEN p.%I ELSE p.%I + d.%I END',
									   c, c, c, c, c, c, c)
							 || format(', %I = p.%I + d.%I', 'avg_' || col || '__n',
									   'avg_' || col || '__n', 'avg_' || col || '__n');
				icols := icols || format(', %I', 'avg_' || col || '__n');
				isel := isel || format(', d.%I', 'avg_' || col || '__n');
		END CASE;
		icols := icols || format(', %I', c);
		isel := isel || format(', d.%I', c);
	END LOOP;
	FOREACH g IN ARRAY r.group_by LOOP
		IF NOT g = ANY (rcols) THEN rcols := rcols || g; END IF;
	END LOOP;
	-- each source column once: sum, min and max of v all read the same v
	SELECT string_agg(format('%I', x), ', ' ORDER BY o) INTO rowcols
	  FROM unnest(rcols) WITH ORDINALITY u(x, o);

	-- ONE statement, so one snapshot, for all of it:
	--   * the groups. A recorded group that expire retired (keep history, the
	--     owner's decision on #1294) leaves the record and its range joins the
	--     rollup's history. Any other recorded group that is gone, or whose
	--     deleted count moved, is CHANGED.
	--   * the dirty buckets: every bucket a changed group's recorded range covers,
	--     and the NULL bucket when it had rows without a ts. Their partials are
	--     deleted and recomputed from the source's live rows.
	--   * the rows of the new groups (one TID range per group, so only their row
	--     groups are read), folded into the clean buckets only: the recompute
	--     already counts their rows in the dirty ones.
	--   * the record: changed groups recounted or forgotten, new ones added with
	--     the range of their rows.
	-- A group that commits while this runs is in none of it, and the next
	-- refresh takes it. Reading retirement here, not in an earlier statement,
	-- keeps a group expire retires meanwhile from looking deleted.
	-- pgcolumnar.rollup_pause holds the statement open for that arm.
	EXECUTE format($f$
		WITH cur AS (
			SELECT * FROM pgcolumnar.rollup_source_groups(%1$L::regclass)
		), ret AS (
			SELECT * FROM pgcolumnar.rollup_source_retired(%1$L::regclass)
		), held AS (
			SELECT pg_sleep(CASE WHEN current_setting('pgcolumnar.rollup_pause', true) = 'on'
								 THEN 2 ELSE 0 END) AS slept
		), rec AS (
			SELECT rg.* FROM pgcolumnar.rollup_group rg WHERE rg.rollup = %2$L::regclass
		), retiring AS (
			SELECT rg.* FROM rec rg WHERE rg.group_number IN (SELECT t.group_number FROM ret t)
		), changed AS (
			SELECT rg.group_number, rg.ts_min, rg.ts_max, rg.ts_has_null,
				   c.row_count AS now_rows, c.deleted_count AS now_deleted
			  FROM rec rg LEFT JOIN cur c USING (group_number)
			 WHERE rg.group_number NOT IN (SELECT t.group_number FROM ret t)
			   AND (c.group_number IS NULL OR c.deleted_count <> rg.deleted_count)
		), dirty AS (
			SELECT coalesce(range_agg(tstzrange(date_bin(%4$L::interval, ch.ts_min, %6$L::timestamptz),
												date_bin(%4$L::interval, ch.ts_max, %6$L::timestamptz)
												+ %17$L::interval))
								FILTER (WHERE ch.ts_min IS NOT NULL), '{}') AS b,
				   coalesce(bool_or(ch.ts_has_null), false) AS n
			  FROM changed ch
		), kept AS (
			SELECT x.history + coalesce((SELECT range_agg(tstzrange(t.ts_min, t.ts_max, '[]')) FROM retiring t
										  WHERE t.ts_min IS NOT NULL), '{}') AS m,
				   x.history_has_null OR coalesce((SELECT bool_or(t.ts_has_null) FROM retiring t), false) AS n
			  FROM pgcolumnar.rollup x WHERE x.rollup = %2$L::regclass
		), blocked AS (
			SELECT 1 FROM dirty d, kept k WHERE d.b && k.m OR (d.n AND k.n)
			UNION ALL
			-- rows deleted since the last refresh from a group expire then
			-- retired: they are gone, so their buckets cannot be recomputed
			SELECT 1 FROM retiring t JOIN ret x USING (group_number)
			 WHERE x.deleted_count <> t.deleted_count
		), new_groups AS (
			SELECT c.* FROM cur c
			 WHERE NOT EXISTS (SELECT 1 FROM rec rg WHERE rg.group_number = c.group_number)
		), rows AS MATERIALIZED (
			SELECT ng.group_number AS pgcolumnar__group, s.* FROM new_groups ng, held,
			LATERAL (SELECT %3$s FROM %1$s
					  WHERE ctid >= pgcolumnar.row_tid(ng.first_row_number)
						AND ctid <= pgcolumnar.row_tid(ng.first_row_number + ng.row_count - 1)
					 -- OFFSET 0 keeps this subquery from being pulled up into a join:
					 -- PostgreSQL builds a TID range scan only from a relation's own
					 -- restrictions, and as a join clause the range read every group.
					 OFFSET 0) s
		), fold AS (
			SELECT w.* FROM rows w, dirty d
			 WHERE NOT CASE WHEN w.%5$I IS NULL THEN d.n
							ELSE d.b @> date_bin(%4$L::interval, w.%5$I, %6$L::timestamptz) END
		), recomputed AS MATERIALIZED (
			-- one range predicate per span of dirty buckets, so zone maps prune
			SELECT s.* FROM dirty d, held, unnest(d.b) u(span),
			LATERAL (SELECT %3$s FROM %1$s
					  WHERE %5$I >= lower(u.span) AND %5$I < upper(u.span) OFFSET 0) s
			UNION ALL
			SELECT s.* FROM dirty d, held,
			LATERAL (SELECT %3$s FROM %1$s WHERE %5$I IS NULL OFFSET 0) s
			 WHERE d.n
		), delta AS (
			SELECT date_bin(%4$L::interval, %5$I, %6$L::timestamptz) AS bucket%7$s%8$s
			  FROM fold GROUP BY 1%9$s
		), redo AS (
			SELECT date_bin(%4$L::interval, %5$I, %6$L::timestamptz) AS bucket%7$s%8$s
			  FROM recomputed GROUP BY 1%9$s
		), cleared AS (
			DELETE FROM %10$s p USING dirty d
			 WHERE CASE WHEN p.bucket IS NULL THEN d.n ELSE d.b @> p.bucket END
		), upd AS (
			UPDATE %10$s p SET %11$s
			  FROM delta d WHERE p.bucket IS NOT DISTINCT FROM d.bucket%12$s
			RETURNING p.bucket%13$s
		), ins AS (
			INSERT INTO %10$s (bucket%7$s%14$s)
			SELECT d.bucket%15$s%16$s FROM delta d
			 WHERE NOT EXISTS (SELECT 1 FROM upd p WHERE p.bucket IS NOT DISTINCT FROM d.bucket%12$s)
		), reins AS (
			INSERT INTO %10$s (bucket%7$s%14$s)
			SELECT d.bucket%15$s%16$s FROM redo d
		), forgotten AS (
			DELETE FROM pgcolumnar.rollup_group rg
			 WHERE rg.rollup = %2$L::regclass
			   AND (rg.group_number IN (SELECT t.group_number FROM retiring t)
					OR rg.group_number IN (SELECT ch.group_number FROM changed ch WHERE ch.now_rows IS NULL))
		), recounted AS (
			UPDATE pgcolumnar.rollup_group rg
			   SET row_count = ch.now_rows, deleted_count = ch.now_deleted
			  FROM changed ch
			 WHERE rg.rollup = %2$L::regclass AND rg.group_number = ch.group_number
			   AND ch.now_rows IS NOT NULL
		), recorded AS (
			INSERT INTO pgcolumnar.rollup_group
			SELECT %2$L::regclass, ng.group_number, ng.row_count, ng.deleted_count,
				   x.lo, x.hi, coalesce(x.has_null, false)
			  FROM new_groups ng
			  LEFT JOIN (SELECT w.pgcolumnar__group, min(w.%5$I) AS lo, max(w.%5$I) AS hi,
								bool_or(w.%5$I IS NULL) AS has_null
						   FROM rows w GROUP BY 1) x ON x.pgcolumnar__group = ng.group_number
		), historied AS (
			UPDATE pgcolumnar.rollup x SET history = k.m, history_has_null = k.n
			  FROM kept k WHERE x.rollup = %2$L::regclass AND EXISTS (SELECT 1 FROM retiring)
		)
		SELECT (SELECT count(*) FROM fold) + (SELECT count(*) FROM recomputed),
			   EXISTS (SELECT 1 FROM blocked)
		$f$,
		r.source, r.rollup, rowcols, r.width, r.ts_column, r.origin,
		keys, dsel,
		(SELECT coalesce(string_agg(format(', %s', n + 1), ''), '') FROM generate_series(1, cardinality(r.group_by)) n),
		r.partials, substr(uset, 3), keyeq,
		(SELECT coalesce(string_agg(format(', p.%I', x), ''), '') FROM unnest(r.group_by) x),
		icols,
		(SELECT coalesce(string_agg(format(', d.%I', x), ''), '') FROM unnest(r.group_by) x),
		isel, wabs)
	INTO folded, refused;
	-- Raised after the statement, so everything it wrote rolls back with it.
	IF refused THEN
		RAISE EXCEPTION 'rollup %: a changed row group of its source reaches buckets whose raw rows retention removed', rollup
			USING ERRCODE = 'feature_not_supported',
				  DETAIL = 'Recomputing those buckets from the source would drop the history they keep, or the rows to recompute them from are gone.',
				  HINT = 'If the rollup is right as it stands, pgcolumnar.rebase_rollup records the source''s current row groups.';
	END IF;
	RETURN folded;
END
$rr$;

COMMENT ON FUNCTION pgcolumnar.refresh_rollup(regclass)
	IS 'bring a rollup up to date in one statement: fold the rows of row groups appended to its source since the last refresh, and recompute the buckets of groups deleted from or rewritten since, except buckets that keep retained history; returns the rows folded and recomputed (#1294)';

CREATE FUNCTION pgcolumnar.rebase_rollup(rollup regclass)
	RETURNS bigint
	LANGUAGE plpgsql
	SET search_path = pg_catalog, pg_temp
AS $rb$
DECLARE
	r		pgcolumnar.rollup;
	n		bigint;
BEGIN
	SELECT * INTO r FROM pgcolumnar.rollup x WHERE x.rollup = rebase_rollup.rollup;
	IF NOT FOUND THEN
		RAISE EXCEPTION '% is not a rollup', rollup USING ERRCODE = 'undefined_object';
	END IF;
	EXECUTE format('LOCK TABLE %s IN SHARE UPDATE EXCLUSIVE MODE', r.partials);
	-- The caller asserts the rollup is current: the source's groups are recorded
	-- as folded, and the rollup's rows are not touched (#1294 point 9).
	-- Each group's time range is read from its rows, one TID range per group,
	-- so a rebase reads the whole source. The history stays as it was.
	DELETE FROM pgcolumnar.rollup_group rg WHERE rg.rollup = r.rollup;
	EXECUTE format($f$
		INSERT INTO pgcolumnar.rollup_group
		SELECT %2$L::regclass, s.group_number, s.row_count, s.deleted_count,
			   x.lo, x.hi, coalesce(x.has_null, false)
		  FROM pgcolumnar.rollup_source_groups(%1$L::regclass) s,
		  LATERAL (SELECT min(%3$I) AS lo, max(%3$I) AS hi, bool_or(%3$I IS NULL) AS has_null
					 FROM %1$s
					WHERE ctid >= pgcolumnar.row_tid(s.first_row_number)
					  AND ctid <= pgcolumnar.row_tid(s.first_row_number + s.row_count - 1)) x
		$f$, r.source, r.rollup, r.ts_column);
	GET DIAGNOSTICS n = ROW_COUNT;
	UPDATE pgcolumnar.rollup x SET source_storage = pgcolumnar.get_storage_id(r.source),
								  source_oid = r.source::oid
	 WHERE x.rollup = r.rollup;
	RETURN n;
END
$rb$;

COMMENT ON FUNCTION pgcolumnar.rebase_rollup(regclass)
	IS 'record a rollup''s source row groups as folded without touching the rollup, after the source was rewritten; the caller asserts the rollup is current (#1294)';

-- #1795: the same export, writing a remote path with a pgcolumnar_parquet
-- server's endpoint and region and the caller's user mapping, as a foreign table
-- of that server reads. tier_partition writes through it.
CREATE FUNCTION pgcolumnar.export_parquet(rel regclass, path text, server name)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_export_parquet';

COMMENT ON FUNCTION pgcolumnar.export_parquet(regclass, text, name)
	IS 'export a columnar table to a Parquet file, writing a remote path with the named pgcolumnar_parquet server and the caller''s user mapping (#1795); returns rows written';

-- #1295: move a cold columnar partition to Parquet in one transaction.
CREATE FUNCTION pgcolumnar.tier_partition(partition regclass, server name, path text,
										  lock_wait interval DEFAULT '10 seconds')
	RETURNS bigint
	LANGUAGE plpgsql
	SET search_path = pg_catalog, pg_temp
AS $tier$
DECLARE
	fault	text := coalesce(current_setting('pgcolumnar.tier_fault_after', true), '');
	rel		pg_class;
	parent	regclass;
	bound	text;
	fdw		name;
	cols	text;
	deps	text;
	note	text;
	acl		record;
	moved	bigint;
	seen	bigint;
	covered	record;
	callers_timeout text := current_setting('lock_timeout');
BEGIN
	-- A REPEATABLE READ or SERIALIZABLE snapshot predates the lock taken below, so
	-- the export would miss rows committed in between, and DROP would take them
	-- (#1295, measured by @jdatcmd). Under READ COMMITTED every statement here
	-- takes a fresh snapshot, so the export sees everything the lock let finish.
	IF current_setting('transaction_isolation') <> 'read committed' THEN
		RAISE EXCEPTION 'tier_partition runs only at READ COMMITTED, not %',
			upper(current_setting('transaction_isolation'))
			USING ERRCODE = 'invalid_transaction_state',
				  DETAIL = 'An older snapshot would export fewer rows than the partition holds, and the rest would be dropped with it.';
	END IF;

	SELECT * INTO rel FROM pg_class WHERE oid = partition;
	IF NOT rel.relispartition THEN
		RAISE EXCEPTION '"%" is not a partition', partition
			USING ERRCODE = 'wrong_object_type';
	END IF;
	-- A partition that is not columnar is refused by export_parquet, before
	-- anything moves; a check of its own here was measured redundant.
	bound := pg_get_expr(rel.relpartbound, rel.oid);
	IF bound = 'DEFAULT' THEN
		RAISE EXCEPTION 'the DEFAULT partition "%" cannot be tiered', partition
			USING ERRCODE = 'feature_not_supported',
				  DETAIL = 'A foreign partition cannot take the rows no other partition accepts.';
	END IF;
	SELECT w.fdwname INTO fdw FROM pg_foreign_server s
		JOIN pg_foreign_data_wrapper w ON w.oid = s.srvfdw WHERE s.srvname = server;
	IF fdw IS NULL THEN
		RAISE EXCEPTION 'server "%" does not exist', server USING ERRCODE = 'undefined_object';
	ELSIF fdw <> 'pgcolumnar_parquet' THEN
		RAISE EXCEPTION 'server "%" uses the % wrapper, not pgcolumnar_parquet', server, fdw
			USING ERRCODE = 'wrong_object_type';
	END IF;

	-- The move writes the file and then reads it back through the foreign
	-- partition, as the caller, before it commits (#1795). The export needs
	-- pg_write_server_files and a foreign table reads a file only for a member of
	-- pg_read_server_files, so a caller without both is refused here, before
	-- anything is written, rather than after the export.
	IF NOT (pg_has_role(current_user, 'pg_write_server_files', 'USAGE')
			AND pg_has_role(current_user, 'pg_read_server_files', 'USAGE')) THEN
		RAISE EXCEPTION 'must be superuser or a member of both the pg_write_server_files and the pg_read_server_files roles to tier a partition'
			USING ERRCODE = 'insufficient_privilege',
				  DETAIL = 'tier_partition writes the file and reads it back through the foreign partition before it commits.';
	END IF;

	SELECT i.inhparent INTO parent FROM pg_inherits i WHERE i.inhrelid = partition;
	-- ATTACH of a foreign table is not allowed under a UNIQUE index; refused here,
	-- before the export, rather than at step 5 with an orphan file behind it.
	IF EXISTS (SELECT 1 FROM pg_index WHERE indrelid = parent AND (indisunique OR indisprimary)) THEN
		RAISE EXCEPTION '"%" has a UNIQUE index or primary key, so a foreign partition cannot be attached to it', parent
			USING ERRCODE = 'feature_not_supported';
	END IF;
	-- DROP would fail on a dependent, or remove it silently with CASCADE. Allowed:
	-- the partition's own row type, TOAST table and indexes, its own constraints
	-- other than foreign keys, its defaults, and triggers cloned from the parent,
	-- all of which the foreign partition does not need or gets again on ATTACH.
	SELECT string_agg(pg_describe_object(d.classid, d.objid, d.objsubid), ', ' ORDER BY 1)
	  INTO deps
	  FROM pg_depend d
	 WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = partition
	   AND NOT (d.classid = 'pg_type'::regclass
			 OR d.classid = 'pg_attrdef'::regclass
			 OR (d.classid = 'pg_class'::regclass AND
				 (SELECT c.relkind FROM pg_class c WHERE c.oid = d.objid) IN ('t', 'i', 'I'))
			 OR (d.classid = 'pg_constraint'::regclass AND EXISTS
				 (SELECT 1 FROM pg_constraint k WHERE k.oid = d.objid
				  AND k.conrelid = partition AND k.contype <> 'f'))
			 OR (d.classid = 'pg_trigger'::regclass AND EXISTS
				 (SELECT 1 FROM pg_trigger t WHERE t.oid = d.objid AND t.tgparentid <> 0)));
	IF deps IS NOT NULL THEN
		RAISE EXCEPTION 'other objects depend on partition "%"', partition
			USING ERRCODE = 'dependent_objects_still_exist',
				  DETAIL = format('Dropping the partition would fail on, or remove: %s.', deps);
	END IF;
	-- export_parquet overwrites an existing file without a word. A file another
	-- foreign table in this database reads is refused; one only a failed earlier
	-- attempt left behind is overwritten, so a retry can use the same path.
	-- A foreign table reads every file its path expands to, as pq_resolve_paths
	-- expands it (#1941). So the path is refused when a table's path is:
	--   - the same path;
	--   - a remote directory (it ends in '/'), or any local path, and the path
	--     lies below it at any depth. Nothing lies below a local file, so a
	--     local path that is not a directory refuses nothing more;
	--   - a glob that the path matches: a local path, or an s3:// key, holding
	--     *, ? or [. A * or ? does not match '/', a backslash escapes the next
	--     character, and a [class] matches any one character but '/', which
	--     refuses at least what the class matches. A class is read as fnmatch
	--     reads it: a ']' right after '[' or '[!' is a member, a backslash
	--     inside it escapes the next character, and a '[' with no closing ']'
	--     is a literal '['. The longest token wins, so a token of just '[]' or
	--     '[!]' is such an unclosed '[' (#1946 review).
	-- Both paths are normalised first (#1936): a remote scheme matches in any
	-- case, s3a:// and s3n:// match s3:// (#1930), and so does the host of an
	-- http(s) URL. The bucket and key keep their case. A local path is compared
	-- as written: a symbolic link, '..' or a relative spelling is not resolved.
	-- A NULL path matches nothing, so it reaches the null refusal (#1937 review).
	SELECT c.relid, c.opath INTO covered
	  FROM (SELECT t.ftrelid::regclass AS relid, substr(o.opt, 6) AS opath,
				   v.np[1] AS tp, v.np[2] AS fp
			  FROM pg_foreign_table t, unnest(t.ftoptions) AS o(opt),
				   LATERAL (SELECT array_agg(
							CASE WHEN p ~* '^s3[an]?://'
								 THEN 's3://' || substr(p, strpos(p, '://') + 3)
								 WHEN p ~* '^https?://'
								 THEN lower(substring(p FROM '^[^:]*://[^/]*'))
									  || substr(p, length(substring(p FROM '^[^:]*://[^/]*')) + 1)
								 WHEN p ~* '^(gs|az)://'
								 THEN lower(split_part(p, '://', 1)) || substr(p, strpos(p, '://'))
								 ELSE p END ORDER BY n)
						   FROM unnest(ARRAY[path, substr(o.opt, 6)]) WITH ORDINALITY AS s(p, n)) AS v(np)
			 WHERE o.opt LIKE 'path=%') AS c
	 WHERE c.tp = c.fp
		OR (c.fp ~ '^(s3|gs|az|https?)://' AND right(c.fp, 1) = '/' AND starts_with(c.tp, c.fp))
		OR (c.fp !~ '^(s3|gs|az|https?)://' AND c.fp <> ''
			AND starts_with(c.tp, rtrim(c.fp, '/') || '/'))
		OR (c.fp !~ '^(gs|az|https?)://' AND c.fp ~ '[*?[]'
			AND c.tp ~ ('^' || (SELECT string_agg(
								CASE WHEN m[1] = '*' THEN '[^/]*'
									 WHEN m[1] IN ('[]', '[!]')
									 THEN regexp_replace(m[1], '([].[^$|()*+?{}\\])', '\\\1', 'g')
									 WHEN m[1] = '?' OR m[1] ~ '^\[.+\]$' THEN '[^/]'
									 ELSE regexp_replace(right(m[1], 1), '([].[^$|()*+?{}\\])', '\\\1', 'g')
								END, '' ORDER BY k)
							FROM regexp_matches(c.fp, '(\\.|\[!?\]?(?:\\.|[^]\\])*\]|.)', 'g')
								 WITH ORDINALITY AS r(m, k)) || '$'))
	 LIMIT 1;
	IF FOUND THEN
		RAISE EXCEPTION 'file "%" is read by a foreign table in this database', path
			USING ERRCODE = 'duplicate_file',
				  DETAIL = format('Foreign table %s reads path %L.', covered.relid, covered.opath);
	END IF;

	-- Every column as ATTACH will compare it: type, collation where it is not
	-- the type's own, a stored or virtual generation expression, and NOT NULL
	-- (#1317 review: each of collation and generation failed at ATTACH, after
	-- the export, leaving an orphan file).
	SELECT string_agg(format('%I %s%s%s%s', a.attname, format_type(a.atttypid, a.atttypmod),
							 CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation
								  THEN ' COLLATE ' || a.attcollation::regcollation::text ELSE '' END,
							 CASE a.attgenerated
								  WHEN 's' THEN format(' GENERATED ALWAYS AS (%s) STORED', pg_get_expr(d.adbin, d.adrelid))
								  WHEN 'v' THEN format(' GENERATED ALWAYS AS (%s) VIRTUAL', pg_get_expr(d.adbin, d.adrelid))
								  ELSE '' END,
							 CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END),
					  ', ' ORDER BY a.attnum)
		INTO cols FROM pg_attribute a
		JOIN pg_type t ON t.oid = a.atttypid
		LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
		WHERE a.attrelid = partition AND a.attnum > 0 AND NOT a.attisdropped;
	note := obj_description(partition, 'pg_class');

	-- Every lock this needs waits at most lock_wait: a long reader on the parent
	-- would otherwise queue every new query behind the move's request.
	PERFORM set_config('lock_timeout',
					   (extract(epoch FROM lock_wait) * 1000)::bigint::text || 'ms', true);

	-- Writes to the cold partition wait; reads go on.
	EXECUTE format('LOCK TABLE %s IN SHARE MODE', partition);
	-- Through the server the foreign partition will read with, so the write
	-- and every later read use one endpoint and one identity (#1795).
	moved := pgcolumnar.export_parquet(partition, path, server);
	-- Under the lock nothing can commit to the partition, so a statement taken
	-- now sees what the export saw. A difference is an error, never a loss.
	EXECUTE format('SELECT count(*) FROM %s', partition) INTO seen;
	IF seen <> moved THEN
		RAISE EXCEPTION 'exported % rows but partition "%" holds %', moved, partition, seen
			USING ERRCODE = 'internal_error';
	END IF;
	IF fault = 'export' THEN RAISE EXCEPTION 'injected fault after export'; END IF;
	IF fault = 'pause' THEN PERFORM pg_sleep(2); END IF;

	-- From here the parent is held ACCESS EXCLUSIVE until commit: catalog work,
	-- then one count through the new foreign partition.
	EXECUTE format('ALTER TABLE %s DETACH PARTITION %s', parent, partition);
	IF fault = 'detach' THEN RAISE EXCEPTION 'injected fault after detach'; END IF;
	EXECUTE format('DROP TABLE %s', partition);
	IF fault = 'drop' THEN RAISE EXCEPTION 'injected fault after drop'; END IF;

	-- regnamespace and regrole quote themselves when needed, so %s, never %I:
	-- %I quoted them a second time (#1317 review: a schema "Tier" or an owner
	-- "Owner X" failed after the export).
	EXECUTE format('CREATE FOREIGN TABLE %s.%I (%s) SERVER %I OPTIONS (path %L)',
				   rel.relnamespace::regnamespace, rel.relname, cols, server, path);
	EXECUTE format('ALTER FOREIGN TABLE %s.%I OWNER TO %s',
				   rel.relnamespace::regnamespace, rel.relname, rel.relowner::regrole);
	FOR acl IN SELECT * FROM aclexplode(rel.relacl) x WHERE x.grantee <> rel.relowner LOOP
		EXECUTE format('GRANT %s ON %s.%I TO %s%s', acl.privilege_type,
					   rel.relnamespace::regnamespace, rel.relname,
					   CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE acl.grantee::regrole::text END,
					   CASE WHEN acl.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
	END LOOP;
	IF note IS NOT NULL THEN
		EXECUTE format('COMMENT ON FOREIGN TABLE %s.%I IS %L',
					   rel.relnamespace::regnamespace, rel.relname, note);
	END IF;
	IF fault = 'create' THEN RAISE EXCEPTION 'injected fault after create'; END IF;
	EXECUTE format('ALTER TABLE %s ATTACH PARTITION %s.%I %s',
				   parent, rel.relnamespace::regnamespace, rel.relname, bound);
	IF fault = 'attach' THEN RAISE EXCEPTION 'injected fault after attach'; END IF;
	-- Read the file back through the foreign partition, as the caller, before
	-- commit (#1795). The write proved the key can write; this proves the caller
	-- can read what the parent now points at. A key that can write but not read
	-- (s3:PutObject without s3:GetObject) fails here, and the move rolls back.
	EXECUTE format('SELECT count(*) FROM %s.%I', rel.relnamespace::regnamespace, rel.relname)
		INTO seen;
	IF seen <> moved THEN
		RAISE EXCEPTION 'wrote % rows to "%" but the foreign partition reads %', moved, path, seen
			USING ERRCODE = 'internal_error';
	END IF;
	-- No ANALYZE: pgcolumnar_parquet has no AnalyzeForeignTable, so there is no
	-- sample to take (checked on #1295).
	-- The caller's lock_timeout again, or it would last to the end of their
	-- transaction: set_config(..., true) is transaction-local, not call-local.
	PERFORM set_config('lock_timeout', callers_timeout, true);
	RETURN moved;
END
$tier$;

COMMENT ON FUNCTION pgcolumnar.tier_partition(regclass, name, text, interval)
	IS 'move a cold columnar partition to a Parquet file, attached in its place as a foreign partition with the same name, bounds, grants and comment, in one transaction at READ COMMITTED (#1295). Holds ACCESS EXCLUSIVE on the parent for the catalog steps; every lock waits at most lock_wait. Returns rows moved';

-- stats() and sort_status() read the table's storage id once (#1637). Both
-- filtered a shared catalog with `storage_id = pgcolumnar.get_storage_id(rel)`.
-- In the filter of a sequential scan that call runs once per catalog row, and
-- each call reads the table's first page, so the cost grew with every other
-- columnar table in the database. Copied from pgcolumnar--1.0-alpha5.sql, so a
-- fresh install and an upgraded one converge.
CREATE OR REPLACE FUNCTION pgcolumnar.stats(
	rel regclass,
	OUT stripeid bigint,
	OUT fileoffset bigint,
	OUT rowcount bigint,
	OUT deletedrows bigint,
	OUT chunkcount integer,
	OUT datalength bigint)
	RETURNS SETOF record
	LANGUAGE plpgsql STABLE SECURITY DEFINER
	SET search_path = pg_catalog, pg_temp
	AS $stats$
DECLARE
	-- once, not per catalog row: in the filter of a sequential scan of the
	-- catalog the call runs for every row, and each call reads the table's
	-- first page (#1637)
	sid bigint;
BEGIN
	PERFORM pgcolumnar.require_caller_select(rel);
	sid := pgcolumnar.get_storage_id(rel);
	RETURN QUERY
	-- Native (PGCN v1) tables report one row per row group from the native
	-- catalog.
	SELECT rg.group_number,
		   rg.file_offset,
		   rg.row_count,
		   COALESCE((SELECT sum(rm.deleted_count)::bigint
					 FROM pgcolumnar.delete_vector rm
					 WHERE rm.storage_id = rg.storage_id
					   AND rm.group_number = rg.group_number), 0::bigint),
		   (SELECT count(DISTINCT zm.vector_index)::int
			FROM pgcolumnar.zone_map zm
			WHERE zm.storage_id = rg.storage_id
			  AND zm.group_number = rg.group_number
			  AND zm.vector_index >= 0),
		   rg.byte_length
	FROM pgcolumnar.row_group rg
	WHERE rg.storage_id = sid
	ORDER BY 1;
END;
$stats$;

CREATE OR REPLACE FUNCTION pgcolumnar.sort_status(
	rel regclass,
	OUT sort_key name[],
	OUT sorted_kind text,
	OUT total_groups bigint,
	OUT sorted_groups bigint,
	OUT appended_groups bigint,
	OUT sorted_rows bigint,
	OUT appended_rows bigint)
	RETURNS record
	-- SECURITY DEFINER, like stats() (#560): the body reads pgcolumnar's internal
	-- catalogs (storage, row_group, options), which carry no GRANT, so an
	-- invoker-rights function false-denied a table's own owner on their own table
	-- (#608). require_caller_select gates the REAL caller via GetOuterUserId(), so
	-- definer rights do not widen who may read a table's sort status. search_path
	-- is pinned as a definer function must.
	LANGUAGE plpgsql STABLE SECURITY DEFINER
	SET search_path = pg_catalog, pg_temp
	AS $sort_status$
DECLARE
	-- once, not per catalog row: in the filter of a sequential scan of the
	-- catalog the call runs for every row, and each call reads the table's
	-- first page (#1637)
	sid bigint;
BEGIN
	PERFORM pgcolumnar.require_caller_select(rel);
	sid := pgcolumnar.get_storage_id(rel);
	WITH s AS (
		SELECT st.storage_id, st.sorted_through, st.sorted_from
		FROM pgcolumnar.storage st
		WHERE st.storage_id = sid
	),
	g AS (
		-- A NULL mark means the storage was never ordered, so no group is in the
		-- run. Comparing against NULL would make every count NULL instead.
		--
		-- The run is a range, not everything below a boundary (#342). A group
		-- numbered below sorted_from was not written by the rewrite that set the
		-- mark: its stripe id was drawn before the rewrite's first, so it is a
		-- concurrent writer's group and is not ordered. sorted_from is NULL only
		-- for a mark written before this column existed, where the old
		-- everything-below reading is kept.
		SELECT rg.row_count,
			   (s.sorted_through IS NOT NULL
				AND rg.group_number <= s.sorted_through
				AND (s.sorted_from IS NULL
					 OR rg.group_number >= s.sorted_from)) AS in_run
		FROM pgcolumnar.row_group rg
		JOIN s ON rg.storage_id = s.storage_id
	)
	-- sort_key reports the ACTUAL clustering recorded by the last recluster
	-- (#415, storage.sorted_by), falling back to the declared options.sort_by
	-- when nothing has been reclustered yet. Before #415 this read only the
	-- declared key, so it was NULL on a table clustered but never declared.
	SELECT COALESCE(
			(SELECT st.sorted_by FROM pgcolumnar.storage st
			 WHERE st.storage_id = sid),
			(SELECT o.sort_by FROM pgcolumnar.options o WHERE o.regclass = rel)),
		   -- HOW that key is applied (#761). sort_key names the columns and says
		   -- nothing about whether they are sorted or laid on a Z-order curve,
		   -- and a Z-order over two or more columns is not a sort on any one of
		   -- them. The catalog has carried this since #758; pgcolumnar.storage
		   -- has no GRANT and is superuser-only, so a table's own owner could
		   -- read it nowhere. NULL when the storage was never ordered, or was
		   -- ordered before the column existed.
		   (SELECT st.sorted_kind FROM pgcolumnar.storage st
			WHERE st.storage_id = sid),
		   (SELECT count(*)::bigint FROM g),
		   (SELECT count(*)::bigint FROM g WHERE g.in_run),
		   (SELECT count(*)::bigint FROM g WHERE NOT g.in_run),
		   COALESCE((SELECT sum(g.row_count)::bigint FROM g WHERE g.in_run), 0::bigint),
		   COALESCE((SELECT sum(g.row_count)::bigint FROM g WHERE NOT g.in_run), 0::bigint)
	INTO sort_key, sorted_kind, total_groups, sorted_groups, appended_groups,
		 sorted_rows, appended_rows;
END;
$sort_status$;

-- Per-row-group distinct-count sketches (#1309): the option column, the catalog and
-- the check set_options calls. All three are in pgcolumnar--1.0-alpha5.sql too, so
-- a fresh install and an upgraded one converge.
ALTER TABLE pgcolumnar.options ADD COLUMN IF NOT EXISTS sketch_columns name[];

/* ---------------------------------------------------------------------------
 * pgcolumnar.sketch (#1309)
 *
 * One HyperLogLog sketch per row group for each column the table's options name
 * in sketch_columns: 2048 one-byte registers, from the type's extended hash
 * (seed 0) of each non-NULL value. Keyed like bloom, and like bloom NOT dumped:
 * a restore rewrites the rows, and the writer rewrites the sketches with them.
 * ------------------------------------------------------------------------- */
CREATE TABLE pgcolumnar.sketch (
	storage_id bigint NOT NULL,
	group_number bigint NOT NULL,
	column_index smallint NOT NULL,
	registers bytea NOT NULL
);
CREATE UNIQUE INDEX sketch_pkey
	ON pgcolumnar.sketch USING btree (storage_id, group_number, column_index);

CREATE FUNCTION pgcolumnar.check_sketch_column(table_name regclass, col name)
	RETURNS void
	LANGUAGE C STABLE STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_check_sketch_column';

COMMENT ON FUNCTION pgcolumnar.check_sketch_column(regclass, name)
	IS 'raise 0A000 unless the column can carry a distinct-count sketch (#1309): an extended hash, and a deterministic collation';

/* ---------------------------------------------------------------------------
 * pgcolumnar.approx_count_distinct (#1309 phase 2)
 *
 * A HyperLogLog distinct count over the type's extended hash, with the same
 * 2048 registers and register rule as pgcolumnar.sketch. Its state is the
 * registers, serialized as bytea between parallel workers and combined by a
 * register-wise max. NULLs are not counted, as with count(DISTINCT).
 * ------------------------------------------------------------------------- */
CREATE FUNCTION pgcolumnar.approx_count_distinct_transfn(internal, anyelement)
	RETURNS internal
	LANGUAGE C PARALLEL SAFE
	AS 'MODULE_PATHNAME', 'pgcolumnar_acd_transfn';

CREATE FUNCTION pgcolumnar.approx_count_distinct_combinefn(internal, internal)
	RETURNS internal
	LANGUAGE C PARALLEL SAFE
	AS 'MODULE_PATHNAME', 'pgcolumnar_acd_combinefn';

CREATE FUNCTION pgcolumnar.approx_count_distinct_serialfn(internal)
	RETURNS bytea
	LANGUAGE C STRICT PARALLEL SAFE
	AS 'MODULE_PATHNAME', 'pgcolumnar_acd_serialfn';

CREATE FUNCTION pgcolumnar.approx_count_distinct_deserialfn(bytea, internal)
	RETURNS internal
	LANGUAGE C STRICT PARALLEL SAFE
	AS 'MODULE_PATHNAME', 'pgcolumnar_acd_deserialfn';

CREATE FUNCTION pgcolumnar.approx_count_distinct_finalfn(internal)
	RETURNS bigint
	LANGUAGE C PARALLEL SAFE
	AS 'MODULE_PATHNAME', 'pgcolumnar_acd_finalfn';

CREATE AGGREGATE pgcolumnar.approx_count_distinct(anyelement) (
	SFUNC = pgcolumnar.approx_count_distinct_transfn,
	STYPE = internal,
	FINALFUNC = pgcolumnar.approx_count_distinct_finalfn,
	COMBINEFUNC = pgcolumnar.approx_count_distinct_combinefn,
	SERIALFUNC = pgcolumnar.approx_count_distinct_serialfn,
	DESERIALFUNC = pgcolumnar.approx_count_distinct_deserialfn,
	PARALLEL = SAFE
);

COMMENT ON AGGREGATE pgcolumnar.approx_count_distinct(anyelement)
	IS 'approximate count of distinct non-NULL values: HyperLogLog, 2048 registers, standard error about 2.3% (#1309)';

-- #1865: the namespace as a text[] of levels, so a level may hold a dot. The
-- text forms above split their namespace on every dot.
CREATE FUNCTION pgcolumnar.iceberg_rest_table_location(catalog_uri text,
													   namespace text[],
													   table_name text)
	RETURNS text
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_table_location_levels';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_table_location(text, text[], text)
	IS 'resolve the current metadata-location of a table named by an Iceberg REST catalog; the namespace is a text[] of levels, so a level may hold a dot (#1865)';

CREATE FUNCTION pgcolumnar.iceberg_rest_scan(catalog_uri text,
											 namespace text[],
											 table_name text)
	RETURNS SETOF record
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_scan_levels';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_scan(text, text[], text)
	IS 'read a table named by an Iceberg REST catalog at its current snapshot; the namespace is a text[] of levels, so a level may hold a dot; supply a column definition list (#1865)';

CREATE FUNCTION pgcolumnar.iceberg_rest_namespace_levels(catalog_uri text)
	RETURNS SETOF text[]
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_namespace_levels';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_namespace_levels(text)
	IS 'list the namespaces of an Iceberg REST catalog, one per row, each as a text[] of its levels; a row passes back as the namespace argument of the other iceberg_rest_* functions, except a row with a level that holds the catalog''s namespace separator, which they refuse with 22023 (#1865)';

CREATE FUNCTION pgcolumnar.iceberg_rest_tables(catalog_uri text, namespace text[])
	RETURNS SETOF text
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_tables_levels';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_tables(text, text[])
	IS 'list the table names in a namespace of an Iceberg REST catalog, one per row; the namespace is a text[] of levels, so a level may hold a dot (#1865)';

-- parquet_schema reports a nested column under its top-level name, not as its
-- leaves (#1893). The comment in pgcolumnar--1.0-alpha5.sql says so too, so a
-- fresh install and an upgraded one converge.
COMMENT ON FUNCTION pgcolumnar.parquet_schema(text)
	IS 'report the columns of a Parquet file, one row per column read_parquet declares, and the PostgreSQL type each maps to: a list or struct under its own name, other nesting one row per leaf (#1893); for a directory or glob, of its first file; field_id is the SchemaElement field id Iceberg projects by, NULL when the writer emitted none (Phase G scan core, #388)';

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

/* pgcolumnar 1.0-alpha2 --> 1.0-alpha3 upgrade
 * Generated from the catalog delta between a fresh 1.0-alpha2 and 1.0-alpha3
 * install and verified by test/native_upgrade_converge.sh, which asserts that an
 * upgraded catalog matches a fresh one in definition, ACL and comment for every
 * object.
 *
 * The 1.0-alpha3 cycle so far changes one function signature. Everything else in
 * [Unreleased] is shared-library only and needs no catalog change.
 */
\echo Use "ALTER EXTENSION pgcolumnar UPDATE" to load this file. \quit

-- New catalog for pgcolumnar.parallel_copy's opt-in load dedup (#403 item 7).
-- Loads pgcolumnar.parallel_copy has already performed, for its opt-in dedup
-- (#403 item 7). One row per (table, file fingerprint) that committed.
--
-- The fingerprint is the SHA-256 of the loaded file's bytes, so a file that
-- changed at the same path is a different load. Path, size and mtime would all
-- call that the same file.
--
-- The row is written AFTER the data commits, never before. A crash between the
-- two leaves data with no fingerprint, so a retry loads again, which is the
-- behaviour without this feature and is the safe direction. The reverse order
-- would leave a fingerprint with no data and refuse rows that were never stored.
CREATE TABLE pgcolumnar.load_fingerprint (
	relation_oid oid NOT NULL,
	fingerprint bytea NOT NULL,       -- SHA-256 of the file's bytes
	rows bigint NOT NULL,
	loaded_at timestamptz NOT NULL DEFAULT now()
);
-- NOT unique, deliberately. The lookup is an existence test, and a unique
-- index would turn the one case that can produce a second row into an ERROR
-- raised AFTER the data committed: two concurrent loads of the same file both
-- check before either records, both commit, and the loser's record insert would
-- fail, reporting failure for a load that succeeded. A duplicate record is
-- harmless; a false failure is not.
CREATE INDEX load_fingerprint_idx
	ON pgcolumnar.load_fingerprint USING btree (relation_oid, fingerprint);

-- Declared retention for pgcolumnar.expire (#403 item 5a). Nothing drops rows on
-- its own; expire is called by name.
ALTER TABLE pgcolumnar.options ADD COLUMN IF NOT EXISTS ttl_column name;
ALTER TABLE pgcolumnar.options ADD COLUMN IF NOT EXISTS ttl_interval interval;

-- Three functions gain parameters, which changes their signatures, so none can
-- be a CREATE OR REPLACE: sort_status (#761), parallel_copy (#403 item 7) and
-- set_options (#403 item 5a).
DROP FUNCTION IF EXISTS pgcolumnar.sort_status(regclass);
DROP FUNCTION IF EXISTS pgcolumnar.parallel_copy(regclass, text, int);
DROP FUNCTION IF EXISTS pgcolumnar.set_options(regclass, int, int, name, int, name, name[]);

CREATE FUNCTION pgcolumnar.set_options(
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
					  AND c.relkind = 'r') THEN
		RAISE EXCEPTION 'relation "%" is not a columnar table', table_name
			USING ERRCODE = 'wrong_object_type',
				HINT = 'Per-table options are read by the columnar writer and '
				'apply only to an ordinary table using the pgcolumnar access '
				'method. A partitioned table has no storage of its own: set the '
				'options on each partition. Otherwise convert the table first '
				'with ALTER TABLE ... SET ACCESS METHOD pgcolumnar, then set '
				'the options.';
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

COMMENT ON FUNCTION pgcolumnar.set_options(regclass, int, int, name, int, name, name[], name, interval)
	IS 'set per-table columnar options; NULL leaves a value unchanged. sort_by declares the physical sort key applied by vacuum_sorted() with no explicit columns (#288); it is NOT auto-maintained -- rows inserted after a sort append in insert order, so re-run vacuum_sorted() to re-establish it, like PostgreSQL CLUSTER';

CREATE FUNCTION pgcolumnar.expire(tablename regclass)
	RETURNS bigint
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_expire';

COMMENT ON FUNCTION pgcolumnar.expire(regclass)
	IS 'drop row groups whose rows are all older than the retention declared by set_options(ttl_column, ttl_interval), without rewriting them (#403)';

CREATE FUNCTION pgcolumnar.parallel_copy(target regclass, filename text,
										 workers int DEFAULT NULL,
										 dedup boolean DEFAULT false)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_parallel_copy';

COMMENT ON FUNCTION pgcolumnar.parallel_copy(regclass, text, int, boolean)
	IS 'atomic parallel bulk load of a COPY text file into a columnar table using background workers: a single columnar table (any row order), or a RANGE-partitioned columnar table sorted by the partition key with one distinct partition set per worker (#300). With dedup, a file already loaded into this table is refused rather than loaded twice (#403)';

CREATE FUNCTION pgcolumnar.sort_status(
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
BEGIN
	PERFORM pgcolumnar.require_caller_select(rel);
	WITH s AS (
		SELECT st.storage_id, st.sorted_through, st.sorted_from
		FROM pgcolumnar.storage st
		WHERE st.storage_id = pgcolumnar.get_storage_id(rel)
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
			 WHERE st.storage_id = pgcolumnar.get_storage_id(rel)),
			(SELECT o.sort_by FROM pgcolumnar.options o WHERE o.regclass = rel)),
		   -- HOW that key is applied (#761). sort_key names the columns and says
		   -- nothing about whether they are sorted or laid on a Z-order curve,
		   -- and a Z-order over two or more columns is not a sort on any one of
		   -- them. The catalog has carried this since #758; pgcolumnar.storage
		   -- has no GRANT and is superuser-only, so a table's own owner could
		   -- read it nowhere. NULL when the storage was never ordered, or was
		   -- ordered before the column existed.
		   (SELECT st.sorted_kind FROM pgcolumnar.storage st
			WHERE st.storage_id = pgcolumnar.get_storage_id(rel)),
		   (SELECT count(*)::bigint FROM g),
		   (SELECT count(*)::bigint FROM g WHERE g.in_run),
		   (SELECT count(*)::bigint FROM g WHERE NOT g.in_run),
		   COALESCE((SELECT sum(g.row_count)::bigint FROM g WHERE g.in_run), 0::bigint),
		   COALESCE((SELECT sum(g.row_count)::bigint FROM g WHERE NOT g.in_run), 0::bigint)
	INTO sort_key, sorted_kind, total_groups, sorted_groups, appended_groups,
		 sorted_rows, appended_rows;
END;
$sort_status$;

COMMENT ON FUNCTION pgcolumnar.sort_status(regclass)
	IS 'how much of an ordered columnar table is still in its ordered run, and by what kind of ordering (#301, #761)';

COMMENT ON FUNCTION pgcolumnar.vacuum_sorted(regclass, name[])
	IS 'compact a columnar table, storing rows sorted ascending (NULLS LAST) on the given columns. With no columns, applies the table''s declared sort_by key from set_options (#288), like a bare CLUSTER re-applying a remembered index; errors if none is declared. Supports any btree-orderable column including text and numeric, unlike Z-order cluster(), which takes integer, date/time, boolean and floating-point columns only. One-shot: not auto-maintained.';

-- pgcolumnar.maintenance_due(): validate both threshold parameters (#860).
-- Unvalidated, a NaN or above-1 threshold silently suppressed all
-- maintenance and a negative one made every table permanently "due", in the
-- gate the autovacuum daemon consults before calling compact_rewrite.
-- The body below is verbatim from pgcolumnar--1.0-alpha3.sql.
CREATE OR REPLACE FUNCTION pgcolumnar.maintenance_due(
	rel regclass,
	compact_due_fraction float8 DEFAULT 0.2,
	recluster_due_fraction float8 DEFAULT 0.05,
	OUT total_rows bigint,
	OUT deleted_rows bigint,
	OUT deleted_fraction float8,
	OUT sort_key name[],
	OUT appended_groups bigint,
	OUT appended_rows bigint,
	OUT appended_fraction float8,
	OUT compact_rewrite_due boolean,
	OUT recluster_due boolean,
	OUT recommendation text)
	RETURNS record
	-- SECURITY DEFINER, mirroring stats(): the report reads pgcolumnar's internal
	-- catalogs through sort_status(), which ordinary roles cannot SELECT, so an
	-- invoker-rights function false-denied every non-superuser caller -- the
	-- cron/monitoring role this report is for. require_caller_select (inside
	-- stats()) still gates the REAL caller via GetOuterUserId(), so definer rights
	-- do not widen who may read a table's statistics. search_path is pinned as a
	-- definer function must.
	LANGUAGE plpgsql STABLE SECURITY DEFINER
	SET search_path = pg_catalog, pg_temp
	AS $maintenance_due$
DECLARE
	st_rows bigint;
	st_del  bigint;
	ss      record;
BEGIN
	-- Validate both thresholds before reading anything (#860). Neither one was
	-- checked, and this is the gate the autovacuum daemon consults BEFORE it ever
	-- calls compact_rewrite, which does check its own. Four ways an unchecked
	-- threshold goes wrong, none of which raises anything:
	--   NaN   -- `fraction >= NaN` is false in IEEE, so nothing is ever due and
	--            the work is suppressed silently and permanently.
	--   > 1   -- the same outcome for any fraction: never due.
	--   < 0   -- `fraction >= -1` is true for EVERY table, so the daemon believes
	--            compaction is always due and rewrites every columnar table on
	--            every pass. This is the dangerous direction: not a suppressed
	--            report but a permanent, self-renewing rewrite.
	--   NULL  -- the verdict is NULL, and the daemon reads a NULL verdict as
	--            "not due" (SPI_getbinval isnull), so it is the NaN case again.
	-- 0.0 and 1.0 are LEGAL and stay legal: 0.0 means "any decay at all is worth
	-- acting on", 1.0 means "only a fully dead table". The bounds are inclusive,
	-- matching pgcolumnar.compact_rewrite's own guard, and test/native_reclaim.sh
	-- pins both endpoints as ACCEPTED so this guard cannot quietly become
	-- over-broad, which is how a bounds check usually breaks.
	--
	-- The explicit NaN test is redundant with `> 1.0` today, because PostgreSQL
	-- float8 ordering is not IEEE ordering: it sorts NaN above every other value.
	-- It is written out anyway so the intent survives an edit to the bounds.
	IF compact_due_fraction IS NULL
	   OR compact_due_fraction = 'NaN'::float8
	   OR compact_due_fraction < 0.0
	   OR compact_due_fraction > 1.0 THEN
		RAISE EXCEPTION 'compact_due_fraction must be a number between 0 and 1'
			USING ERRCODE = 'invalid_parameter_value';
	END IF;
	IF recluster_due_fraction IS NULL
	   OR recluster_due_fraction = 'NaN'::float8
	   OR recluster_due_fraction < 0.0
	   OR recluster_due_fraction > 1.0 THEN
		RAISE EXCEPTION 'recluster_due_fraction must be a number between 0 and 1'
			USING ERRCODE = 'invalid_parameter_value';
	END IF;

	-- stats() enforces require_caller_select(rel) before it returns a row, so a
	-- caller without SELECT on rel is refused here rather than reported to.
	SELECT COALESCE(sum(s.rowcount), 0), COALESCE(sum(s.deletedrows), 0)
	  INTO st_rows, st_del
	  FROM pgcolumnar.stats(rel) s;

	SELECT * INTO ss FROM pgcolumnar.sort_status(rel);

	total_rows   := st_rows;
	deleted_rows := st_del;
	deleted_fraction := CASE WHEN st_rows > 0
							 THEN st_del::float8 / st_rows ELSE 0 END;

	sort_key        := ss.sort_key;
	appended_groups := ss.appended_groups;
	appended_rows   := ss.appended_rows;
	appended_fraction := CASE WHEN (ss.sorted_rows + ss.appended_rows) > 0
							  THEN ss.appended_rows::float8
								   / (ss.sorted_rows + ss.appended_rows)
							  ELSE 0 END;

	compact_rewrite_due := (deleted_fraction >= compact_due_fraction);
	-- A sorted RUN must exist for recluster to mean anything. sort_status()
	-- reports a never-ordered table as entirely appended (no run), and
	-- vacuum_sorted() establishes a run without setting options.sort_by, so the
	-- run -- sorted_groups > 0 -- is the signal, not the sort_by label (sort_key
	-- is reported for information and may be NULL on an ordered table).
	recluster_due := (ss.sorted_groups > 0
					  AND ss.appended_groups > 0
					  AND appended_fraction >= recluster_due_fraction);

	recommendation := NULLIF(
		concat_ws(', ',
			CASE WHEN compact_rewrite_due THEN 'compact_rewrite' END,
			CASE WHEN recluster_due THEN 'recluster' END),
		'');
	RETURN;
END;
$maintenance_due$;

COMMENT ON FUNCTION pgcolumnar.maintenance_due(regclass, float8, float8)
	IS 'report whether an online maintenance verb (compact_rewrite, recluster) is worth running, from table statistics alone; thresholds are parameters with defaults measured on #415, each required to be a number between 0 and 1 inclusive (#860); pure report, takes no lock and rewrites nothing (#415)';

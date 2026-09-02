/* pgColumnar 1.0 - native (PGCN v1) metadata catalog and access method
 * registration.
 *
 * The catalog matches section 11 of
 * design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md. Column order and index
 * definitions are part of the on-disk format.
 *
 * The catalog holds the native storage, row_group, column_chunk, zone_map,
 * and bloom tables, the shared delete_vector and options tables, the storageid_seq
 * sequence, the columnar_handler function, and the columnar access method.
 */

-- complain if script is sourced in psql, rather than via CREATE EXTENSION
\echo Use "CREATE EXTENSION pgcolumnar" to load this file. \quit

/* ---------------------------------------------------------------------------
 * Sequences (spec 7.6)
 * ------------------------------------------------------------------------- */

CREATE SEQUENCE pgcolumnar.storageid_seq
	MINVALUE 10000000000
	NO CYCLE;

/* ---------------------------------------------------------------------------
 * pgcolumnar.delete_vector (spec 7.5)
 *
 * Tracks deleted rows for updates and deletes without rewriting stripes. One
 * row per chunk group, keyed by group_number; a set bit in "bitmap" marks a
 * deleted row (bit i is the group's i-th row, row_group.first_row_number + i,
 * LSB-first in byte i/8). deleted_count is the number of set bits.
 * ------------------------------------------------------------------------- */

CREATE TABLE pgcolumnar.delete_vector (
	storage_id bigint NOT NULL,
	group_number bigint NOT NULL,
	bitmap bytea,
	deleted_count integer NOT NULL
);

CREATE UNIQUE INDEX delete_vector_pkey
	ON pgcolumnar.delete_vector USING btree (storage_id, group_number);

/* ---------------------------------------------------------------------------
 * pgcolumnar.options (spec 7.4)
 *
 * Per-table overrides of the instance-wide compression, compression level,
 * chunk-group row limit, and stripe row limit. A NULL column means the table
 * uses the instance default (the GUC) for that option. Keyed by regclass.
 * ------------------------------------------------------------------------- */

CREATE TABLE pgcolumnar.options (
	regclass regclass NOT NULL,
	chunk_group_row_limit integer,
	stripe_row_limit integer,
	compression_level integer,
	compression name,
	encode_effort name,
	sort_by name[],                    -- declared physical sort key (#288)
	-- Declared retention (#403 item 5a), read only by pgcolumnar.expire.
	-- Nothing drops rows on its own: expire is called by name.
	ttl_column name,
	ttl_interval interval
);

/*
 * sort_by holds COLUMN NAMES, not attnums, on purpose. pgcolumnar.options is
 * the one catalog carried through pg_dump (pg_extension_config_dump below); a
 * plain (non-binary-upgrade) pg_dump does not re-emit dropped columns, so live
 * attnums renumber densely on restore while names do not. The governing rule:
 * store NAMES in the dumped catalog (regclass, sort_by); store ATTNUMS only in
 * the storage_id-keyed catalogs that are NOT dumped (projection.sort_key,
 * row_group.sort_key), which are regenerated on restore anyway. See the
 * regclass rationale below. NULL means no declared sort key; the apply path
 * (vacuum_sorted with no explicit columns) resolves the names to attnums each
 * time and re-validates them, so a later DROP/RENAME of a named column is
 * caught then rather than corrupting anything.
 */

CREATE UNIQUE INDEX options_pkey
	ON pgcolumnar.options USING btree (regclass);

/*
 * Carry the per-table options through pg_dump (#248).
 *
 * Rows in an extension's own tables are not dumped unless the extension says so.
 * Without this, pg_dump emitted the table definition and its data but never the
 * options row, so a restored columnar table silently reverted to default
 * stripe/chunk limits, compression and encode_effort. Silent, because nothing
 * fails: the data is all there and only the settings are gone.
 *
 * This table and ONLY this table. Every other pgcolumnar catalog table is keyed
 * by storage_id, which is assigned when the relation is created, so a restore
 * generates new ones -- dumping those rows would restore metadata pointing at
 * storage that no longer exists, which is worse than losing it. options is keyed
 * by regclass, a name that survives dump and restore, and it holds user intent
 * rather than physical layout, which is the same reason it is the only one worth
 * carrying.
 *
 * Projections are user intent too and are still lost across a dump, for the
 * storage_id reason above; re-emitting pgcolumnar.add_projection() calls is a
 * different mechanism and its own problem.
 */
SELECT pg_catalog.pg_extension_config_dump('pgcolumnar.options', '');

/*
 * The declared intent behind each projection, as opposed to pgcolumnar.projection
 * which records the materialized result (#266).
 *
 * pgcolumnar.projection is keyed by storage_id and stores attnums, so pg_dump
 * cannot carry it: a restore assigns new storage ids, and rows pointing at
 * storage that does not exist would be worse than losing them. This table is
 * keyed by regclass and stores column NAMES, for the same reason
 * pgcolumnar.options is keyed by regclass and the sort_by key stores names: a
 * name survives a dump and a restore, and a restore renumbers an attnum.
 *
 * So a dump carries the declaration and not the data. After a restore the
 * declarations are present and the projection storage is not, and
 * pgcolumnar.rebuild_projections() materializes them. Readers never consult this
 * table. They read pgcolumnar.projection, where a row appears only after its
 * storage exists.
 */
CREATE TABLE pgcolumnar.projection_declaration (
	rel regclass NOT NULL,
	name name NOT NULL,
	columns text[] NOT NULL,
	sort_key text[] NOT NULL
);

CREATE UNIQUE INDEX projection_declaration_pkey
	ON pgcolumnar.projection_declaration USING btree (rel, name);

SELECT pg_catalog.pg_extension_config_dump('pgcolumnar.projection_declaration', '');

/* ---------------------------------------------------------------------------
 * pgcolumnar.projection (gap 26)
 *
 * Multiple physical projections per table (C-Store). Each projection is a named,
 * ordered subset of the table's columns stored as its own columnar storage
 * (proj_storage_id) sorted on sort_key, sharing the row-number identity space.
 * projection_id 0 is the implicit base projection (all columns, insert order);
 * a table with no rows here has a single implicit base projection, so a table
 * with no declared projections behaves as one with only its base.
 * ------------------------------------------------------------------------- */

CREATE TABLE pgcolumnar.projection (
	storage_id bigint NOT NULL,       -- the table's base storage id
	projection_id integer NOT NULL,   -- 0 = base, 1..N additional
	name name NOT NULL,
	proj_storage_id bigint NOT NULL,  -- this projection's own storage id
	sort_key smallint[] NOT NULL,     -- attnums in sort order ({} = insert order)
	columns smallint[] NOT NULL       -- attnums stored (base = all live columns)
);

CREATE UNIQUE INDEX projection_pkey
	ON pgcolumnar.projection USING btree (storage_id, projection_id);

CREATE UNIQUE INDEX projection_name_idx
	ON pgcolumnar.projection USING btree (storage_id, name);

CREATE UNIQUE INDEX projection_storage_idx
	ON pgcolumnar.projection USING btree (proj_storage_id);

/* ---------------------------------------------------------------------------
 * Native format catalog (format PGCN v1).
 *
 * The native on-disk format (design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md
 * section 11). Dropped with the extension; per-table row cleanup is wired
 * into ColumnarDeleteMetadata.
 * ------------------------------------------------------------------------- */

CREATE TABLE pgcolumnar.storage (
	storage_id bigint NOT NULL,       -- native relation storage id
	relation_oid oid NOT NULL,
	format_version integer NOT NULL,  -- native format major version (1)
	vector_length integer NOT NULL,   -- values per vector (1024)
	row_group_limit integer NOT NULL, -- max rows per row group
	-- The row group number the last ordering rewrite ended at (#301). NULL means
	-- the storage was never ordered.
	--
	-- pgcolumnar.vacuum_sorted, pgcolumnar.cluster and pgcolumnar.recluster order
	-- every live row, so every group up to and including this number is part of
	-- one ordered run. Groups numbered above it were written later, in insert
	-- order, and are the unsorted tail. That is what makes a sorted layout decay,
	-- and pgcolumnar.sort_status reports the size of each part.
	--
	-- It is a boundary rather than a count because the online maintenance paths
	-- retire groups and write replacements with fresh, higher numbers. A count
	-- would silently re-point at those replacements as the run shrank; a boundary
	-- leaves them above the mark, where they belong.
	--
	-- It lives here rather than in pgcolumnar.options because a storage row has
	-- exactly the right lifetime. Any rewrite creates a new storage id, so an
	-- unsorted vacuum leaves this NULL and correctly reports the table as
	-- unsorted, with no invalidation step. A value in an options row, which is
	-- keyed by relation, would outlive the layout it describes.
	sorted_through bigint,
	-- Lower end of the ordered run (#342). The run is [sorted_from,
	-- sorted_through]; a bare upper bound cannot exclude a concurrently written
	-- group whose id was drawn below the rewrite's own first id, which is how a
	-- foreign group came to be counted as ordered.
	sorted_from bigint,
	-- What the ordered run is clustered BY and HOW (#415). sorted_by is the
	-- clustering columns; sorted_kind is 'zorder' (recluster/cluster) or
	-- 'lexicographic' (vacuum_sorted). Both NULL on an unordered storage, or on
	-- one ordered before this column existed -- treated as "unknown key", which
	-- the self-gating recluster never skips. They let recluster tell "already
	-- clustered by these columns this way" from "clustered by something else",
	-- so it returns without a full rewrite when nothing decayed.
	sorted_by name[],
	sorted_kind text
);
CREATE UNIQUE INDEX storage_pkey
	ON pgcolumnar.storage USING btree (storage_id);

CREATE TABLE pgcolumnar.row_group (
	storage_id bigint NOT NULL,
	-- ONE-BASED. group_number is the stripe id reserved from the metapage when
	-- the group began buffering, and PgColumnarInitMetapage starts
	-- reservedStripeId at 1, so there is no group 0 on any storage. The same
	-- numbering is used by column_chunk, zone_map, bloom and delete_vector.
	--
	-- This said "0-based row group ordinal" until #817. Nothing read the comment
	-- at runtime, but a reader did: the planner's zone-map sample walked
	-- [0, ngroups), so it spent its first probe on a number that cannot exist and
	-- never probed the highest group at all, which priced a predicate differently
	-- according to where in the table its groups sat.
	group_number bigint NOT NULL,
	file_offset bigint NOT NULL,      -- logical byte offset of the group
	row_count bigint NOT NULL,
	byte_length bigint NOT NULL,
	first_row_number bigint NOT NULL, -- row number of the group's first row
	sort_key smallint[] NOT NULL DEFAULT '{}'  -- attnums the group is sorted on
);
CREATE UNIQUE INDEX row_group_pkey
	ON pgcolumnar.row_group USING btree (storage_id, group_number);

CREATE TABLE pgcolumnar.column_chunk (
	storage_id bigint NOT NULL,
	group_number bigint NOT NULL,
	column_index smallint NOT NULL,   -- 0-based attribute position
	value_count bigint NOT NULL,
	encoding_descriptor bytea NOT NULL, -- the chosen cascade (Phase D4)
	block_codec smallint NOT NULL,    -- optional final block codec (0 = none)
	page_offset bigint NOT NULL,      -- logical byte offset of the chunk's page
	page_length bigint NOT NULL
);
CREATE UNIQUE INDEX column_chunk_pkey
	ON pgcolumnar.column_chunk USING btree (storage_id, group_number, column_index);

CREATE TABLE pgcolumnar.zone_map (
	storage_id bigint NOT NULL,
	group_number bigint NOT NULL,
	column_index smallint NOT NULL,
	vector_index integer NOT NULL,    -- -1 for the whole-chunk aggregate
	minimum bytea,                    -- encoded per the column type
	maximum bytea,
	sum numeric,                      -- NULL when the type has no sum
	value_count bigint NOT NULL,
	null_count bigint NOT NULL
);
CREATE UNIQUE INDEX zone_map_pkey
	ON pgcolumnar.zone_map USING btree (storage_id, group_number, column_index, vector_index);

-- Per-column-chunk bloom filter for equality skipping on hashable columns
-- (native spec 7.2). One row per (storage_id, group_number, column_index).
CREATE TABLE pgcolumnar.bloom (
	storage_id bigint NOT NULL,
	group_number bigint NOT NULL,
	column_index smallint NOT NULL,
	filter bytea NOT NULL
);
CREATE UNIQUE INDEX bloom_pkey
	ON pgcolumnar.bloom USING btree (storage_id, group_number, column_index);

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

/* ---------------------------------------------------------------------------
 * pgcolumnar.free_space (Phase F physical reclaim)
 *
 * Freed logical byte ranges from retired row groups, available for reuse by a
 * later stripe reservation once no snapshot can still read them. file_offset is
 * page-aligned; freed_xid is the retiring transaction's id, and the range is
 * reusable only once the oldest-xmin horizon has passed it. Reuse makes online
 * compaction space-neutral instead of forever advancing the file highwater.
 * ------------------------------------------------------------------------- */

CREATE TABLE pgcolumnar.free_space (
	storage_id bigint NOT NULL,
	file_offset bigint NOT NULL,
	byte_length bigint NOT NULL,
	freed_xid bigint NOT NULL
);
CREATE UNIQUE INDEX free_space_pkey
	ON pgcolumnar.free_space USING btree (storage_id, file_offset);
CREATE INDEX free_space_fit
	ON pgcolumnar.free_space USING btree (storage_id, byte_length);

/* ---------------------------------------------------------------------------
 * Access method (spec 8.1)
 * ------------------------------------------------------------------------- */

CREATE FUNCTION pgcolumnar.columnar_handler(internal)
	RETURNS table_am_handler
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_handler';

CREATE ACCESS METHOD pgcolumnar
	TYPE TABLE
	HANDLER pgcolumnar.columnar_handler;

COMMENT ON ACCESS METHOD pgcolumnar IS 'pgColumnar column-oriented storage';

/* ---------------------------------------------------------------------------
 * Conversion between heap and columnar (spec 8.2)
 *
 * alter_table_set_access_method converts a table between heap and columnar by
 * driving PostgreSQL's own ALTER TABLE ... SET ACCESS METHOD, which rewrites
 * the table through the target access method (columnar's insert path when
 * converting to columnar, its scan path when converting away). Row counts and
 * values round-trip. "t" is a table name (optionally schema-qualified);
 * "method" is "pgcolumnar" or "heap" (or any other table access method).
 * ------------------------------------------------------------------------- */

CREATE FUNCTION pgcolumnar.alter_table_set_access_method(t text, method text)
	RETURNS void
	LANGUAGE plpgsql
	AS $alter_table_set_access_method$
DECLARE
	rel regclass := t::regclass;
	nsp text;
	tbl text;
	tmp text;
BEGIN
	/*
	 * PostgreSQL 15 introduced ALTER TABLE ... SET ACCESS METHOD, which
	 * rewrites the table in place through the target access method and
	 * preserves the relation's identity and dependents. Use it when available.
	 */
	IF current_setting('server_version_num')::int >= 150000 THEN
		EXECUTE format('ALTER TABLE %s SET ACCESS METHOD %I', rel::text, method);
		RETURN;
	END IF;

	/*
	 * PostgreSQL 13 and 14 have no ALTER TABLE ... SET ACCESS METHOD. Convert
	 * by building a sibling table that uses the target access method, copying
	 * every row through it, and swapping names. Column definitions, defaults,
	 * NOT NULL and CHECK constraints, and indexes are carried over
	 * (LIKE ... INCLUDING ALL). This does not preserve the original table's OID
	 * or objects that depend on it (views, foreign keys); on those majors that
	 * is a documented limitation of the conversion helper.
	 */
	SELECT n.nspname, c.relname INTO nsp, tbl
	  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
	 WHERE c.oid = rel;
	tmp := tbl || '_pgcolumnar_conv';

	EXECUTE format('CREATE TABLE %I.%I (LIKE %I.%I INCLUDING ALL) USING %I',
				   nsp, tmp, nsp, tbl, method);
	EXECUTE format('INSERT INTO %I.%I SELECT * FROM %I.%I',
				   nsp, tmp, nsp, tbl);
	EXECUTE format('DROP TABLE %I.%I', nsp, tbl);
	EXECUTE format('ALTER TABLE %I.%I RENAME TO %I', nsp, tmp, tbl);
END;
$alter_table_set_access_method$;

COMMENT ON FUNCTION pgcolumnar.alter_table_set_access_method(text, text)
	IS 'convert a table between heap and columnar storage';

/* ---------------------------------------------------------------------------
 * Per-table option set and reset (spec 8.2)
 *
 * set_options stores per-table option overrides; a NULL argument
 * leaves that option unchanged. reset_options clears an option
 * back to the instance default when its boolean argument is true. Options take
 * effect for writes that begin after they are set.
 * ------------------------------------------------------------------------- */

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

CREATE FUNCTION pgcolumnar.reset_options(
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

COMMENT ON FUNCTION pgcolumnar.reset_options(regclass, bool, bool, bool, bool, bool, bool)
	IS 'reset per-table columnar options to the instance defaults';

/* ---------------------------------------------------------------------------
 * Storage-id lookup, statistics, and vacuum (spec 8.2)
 * ------------------------------------------------------------------------- */

CREATE FUNCTION pgcolumnar.get_storage_id(rel regclass)
	RETURNS bigint
	LANGUAGE C STABLE STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_relation_storageid';

COMMENT ON FUNCTION pgcolumnar.get_storage_id(regclass)
	IS 'storage id linking a columnar table to its metadata rows';

CREATE FUNCTION pgcolumnar.add_projection(
	rel regclass,
	name text,
	columns text[],
	sort_key text[] DEFAULT '{}')
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_add_projection';

COMMENT ON FUNCTION pgcolumnar.add_projection(regclass, text, text[], text[])
	IS 'declare a physical projection: a named column subset sorted on sort_key (gap 26)';

CREATE FUNCTION pgcolumnar.drop_projection(rel regclass, name text)
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_drop_projection';

COMMENT ON FUNCTION pgcolumnar.drop_projection(regclass, text)
	IS 'drop a declared projection and free its storage (gap 26)';

/*
 * Materialize every declaration that has no projection behind it (#266).
 *
 * The case this exists for is a logical restore. pg_dump carries
 * pgcolumnar.projection_declaration and cannot carry the projection storage, so
 * a restored table has the declarations and none of the projections. This builds
 * them, and returns the number that it built.
 *
 * You can run it at any time. It does not act on a declaration that is already
 * materialized, so a second run builds nothing.
 */
CREATE FUNCTION pgcolumnar.rebuild_projections(rel regclass DEFAULT NULL)
	RETURNS integer
	LANGUAGE plpgsql
	AS $$
DECLARE
	d          record;
	rebuilt    integer := 0;
BEGIN
	/*
	 * Forget a declaration whose relation is gone (#304). The drop hook removes
	 * these, so a current build does not make them. A database created by a
	 * build that did not clean up on drop still holds them, and one such row
	 * used to abort this function for every other table in the database: the
	 * guard below resolves pd.rel, and resolving a dropped relation raises.
	 * Deleting them here makes an affected database repair itself.
	 */
	DELETE FROM pgcolumnar.projection_declaration pd
	 WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.oid = pd.rel);

	FOR d IN
		SELECT pd.rel, pd.name, pd.columns, pd.sort_key
		  FROM pgcolumnar.projection_declaration pd
		 WHERE (rebuild_projections.rel IS NULL OR pd.rel = rebuild_projections.rel)
		   AND NOT EXISTS (
			   SELECT 1
				 FROM pgcolumnar.projection p
				WHERE p.storage_id = pgcolumnar.get_storage_id(pd.rel)
				  AND p.name = pd.name
				  AND p.projection_id > 0)
		 ORDER BY pd.rel::text, pd.name
	LOOP
		PERFORM pgcolumnar.add_projection(d.rel, d.name::text, d.columns, d.sort_key);
		rebuilt := rebuilt + 1;
	END LOOP;
	RETURN rebuilt;
END;
$$;

COMMENT ON FUNCTION pgcolumnar.rebuild_projections(regclass)
	IS 'materialize declared projections that have no storage, after a logical restore (#266)';

CREATE FUNCTION pgcolumnar.read_projection(rel regclass, name text)
	RETURNS SETOF text
	LANGUAGE C STABLE
	AS 'MODULE_PATHNAME', 'pgcolumnar_read_projection';

COMMENT ON FUNCTION pgcolumnar.read_projection(regclass, text)
	IS 'read a projection''s stored columns (live rows), joined by | -- verification/debug (gap 26)';

CREATE FUNCTION pgcolumnar.reconstruct_via_projection(rel regclass, name text)
	RETURNS SETOF text
	LANGUAGE C STABLE
	AS 'MODULE_PATHNAME', 'pgcolumnar_reconstruct_via_projection';

COMMENT ON FUNCTION pgcolumnar.reconstruct_via_projection(regclass, text)
	IS 'read all live rows via a projection, reconstructing non-covered columns from the base by row number (gap 26)';

-- #562: EXECUTE is granted to PUBLIC by CREATE FUNCTION, so without these the
-- C check below is the only boundary. Two layers on purpose: the C check is
-- what makes the functions safe, and this is what keeps an unprivileged role
-- from reaching them at all.
REVOKE ALL ON FUNCTION pgcolumnar.read_projection(regclass, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION pgcolumnar.reconstruct_via_projection(regclass, text) FROM PUBLIC;

CREATE FUNCTION pgcolumnar.require_caller_select(rel regclass) RETURNS void
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_require_caller_select';

REVOKE ALL ON FUNCTION pgcolumnar.require_caller_select(regclass) FROM PUBLIC;

COMMENT ON FUNCTION pgcolumnar.require_caller_select(regclass)
	IS 'raise unless the calling role may SELECT the relation; for SECURITY DEFINER callers (#560)';

-- SECURITY DEFINER, because this reads pgcolumnar's catalog tables and those
-- carry no GRANT, so a columnar table's own owner could not read the statistics
-- of the table they own. Granting SELECT on the catalog instead would publish
-- pgcolumnar.zone_map, which holds per-column minimum, maximum and sum for every
-- columnar table. That is actual column data and a far larger disclosure than
-- the usability defect it would fix.
--
-- Definer rights mean nothing else will refuse anyone, so the privilege check is
-- explicit and is the first statement. search_path is pinned because a definer
-- function must not resolve names through a caller-controlled path.
CREATE FUNCTION pgcolumnar.stats(
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
BEGIN
	PERFORM pgcolumnar.require_caller_select(rel);
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
	WHERE rg.storage_id = pgcolumnar.get_storage_id(rel)
	ORDER BY 1;
END;
$stats$;

COMMENT ON FUNCTION pgcolumnar.stats(regclass)
	IS 'per-row-group statistics for a columnar table';

/*
 * How much of an ordered layout is still ordered (#301).
 *
 * pgcolumnar.vacuum_sorted and pgcolumnar.cluster order the whole relation once.
 * They do not keep it ordered: rows inserted later append in insert order, so
 * the ordered run stays at the front and an unsorted tail grows behind it. This
 * reports the size of each part, so a DBA can decide when a re-sort is worth its
 * cost instead of guessing.
 *
 * The ordered run is every row group numbered within the range the rewrite left
 * in pgcolumnar.storage: from sorted_from to sorted_through inclusive. Groups
 * above it were written later. Groups below it belong to a writer that started
 * before the rewrite did and so were never ordered by it (#342); recording only
 * an upper bound counted those as ordered.
 *
 * The row counts are stored rows. Rows deleted but not yet reclaimed are still
 * stored, so they are still counted. pgcolumnar.stats reports the deleted count
 * per group for callers that need to subtract it.
 *
 * Limits to read before acting on the numbers:
 *
 * 1. The online pgcolumnar.recluster sets the mark only for the part of its
 *    output it can prove is one contiguous ordered run. It reorders under a lock
 *    that permits concurrent inserts, and a boundary can only mean "everything
 *    at or below this is ordered" if no other session's group is numbered below
 *    it. With no concurrent writer it records the whole relation. With one, it
 *    records the run up to the point the other session interrupted, which can be
 *    a small part of what it ordered, and reports the rest as decay. It errs
 *    toward reporting too much decay, never too little.
 *
 * 2. The mark says where an ordered run ended, not that the rows in it are still
 *    in that order. Nothing in the design can move a stored row, so the run
 *    holds its order; but an UPDATE writes the new row version at the end, which
 *    counts as appended, and the old version stays in the run until it is
 *    reclaimed.
 *
 * A relation that was never ordered reports no sorted groups, because a rewrite
 * always creates a new storage row and only an ordering rewrite sets the mark on
 * it. A relation with nothing written reports zeros.
 */
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

CREATE FUNCTION pgcolumnar.expire(tablename regclass)
	RETURNS bigint
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_expire';

COMMENT ON FUNCTION pgcolumnar.expire(regclass)
	IS 'drop row groups whose rows are all older than the retention declared by set_options(ttl_column, ttl_interval), without reading or rewriting them (#403)';

CREATE FUNCTION pgcolumnar.vacuum(tablename regclass, stripe_count int DEFAULT 0)
	RETURNS void
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_vacuum';

COMMENT ON FUNCTION pgcolumnar.vacuum(regclass, int)
	IS 'compact a columnar table by combining stripes and reclaiming deleted rows';

CREATE FUNCTION pgcolumnar.vacuum_sorted(
	tablename regclass,
	VARIADIC sort_columns name[])
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_vacuum_sorted';

COMMENT ON FUNCTION pgcolumnar.vacuum_sorted(regclass, name[])
	IS 'compact a columnar table, storing rows sorted ascending (NULLS LAST) on the given columns. With no columns, applies the table''s declared sort_by key from set_options (#288), like a bare CLUSTER re-applying a remembered index; errors if none is declared. Supports any btree-orderable column including text and numeric, unlike Z-order cluster(), which takes integer, date/time, boolean and floating-point columns only. One-shot: not auto-maintained.';

/*
 * One-argument form: apply the declared sort_by key (#288). A VARIADIC function
 * cannot be called cleanly with zero variadic arguments from an unknown literal
 * (vacuum_sorted('t') would not resolve), so this explicit overload gives a
 * clean bare-table call. It shares the C entry point, which uses PG_NARGS() to
 * detect the missing column list and fall back to the persisted key.
 */
CREATE FUNCTION pgcolumnar.vacuum_sorted(tablename regclass)
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_vacuum_sorted';

COMMENT ON FUNCTION pgcolumnar.vacuum_sorted(regclass)
	IS 'apply the table''s declared sort_by key from set_options (#288); errors if none is declared. Equivalent to a bare CLUSTER re-applying a remembered index.';

CREATE FUNCTION pgcolumnar.cluster(
	tablename regclass,
	VARIADIC columns name[])
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_cluster';

COMMENT ON FUNCTION pgcolumnar.cluster(regclass, name[])
	IS 'eager reorg: rewrite a columnar table with rows ordered by the Z-order space-filling curve over the given columns. Holds AccessExclusiveLock like CLUSTER/VACUUM FULL; the online incremental path is Phase F3';

CREATE FUNCTION pgcolumnar.compact(tablename regclass)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_compact';

COMMENT ON FUNCTION pgcolumnar.compact(regclass)
	IS 'lazy online compaction: retire row groups that are fully deleted, dropping their metadata so scans skip them. Holds only ShareUpdateExclusiveLock (concurrent reads and writes). Returns the number of groups retired (Phase F3a)';

CREATE FUNCTION pgcolumnar.truncate(tablename regclass)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_truncate';

COMMENT ON FUNCTION pgcolumnar.truncate(regclass)
	IS 'physical end-truncation: return trailing reclaimed blocks to the OS. Best-effort -- takes AccessExclusiveLock conditionally for the brief physical step and returns 0 without waiting if the table is busy. Only removes space freed before the oldest-xmin horizon. Gated by pgcolumnar.enable_end_truncation. Returns the number of blocks truncated (Phase F)';

CREATE FUNCTION pgcolumnar.compact_rewrite(
	tablename regclass,
	min_deleted_fraction float8 DEFAULT 0.2,
	max_groups int DEFAULT 0)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_compact_rewrite';

COMMENT ON FUNCTION pgcolumnar.compact_rewrite(regclass, float8, int)
	IS 'lazy online space reclaim: rewrite partially-deleted row groups (deleted fraction >= min_deleted_fraction) to drop their dead rows, under ShareUpdateExclusiveLock (concurrent reads and writes). Returns the number of groups rewritten (Phase F3b)';

CREATE FUNCTION pgcolumnar.recluster(
	tablename regclass,
	VARIADIC columns name[])
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_recluster';

COMMENT ON FUNCTION pgcolumnar.recluster(regclass, name[])
	IS 'lazy online reclustering: re-establish global Z-order clustering over the given columns under ShareUpdateExclusiveLock (concurrent reads and writes), unlike the eager cluster() which holds AccessExclusiveLock. Returns the number of groups reclustered (Phase F3c)';

CREATE FUNCTION pgcolumnar.export_arrow(rel regclass, path text)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_export_arrow';

COMMENT ON FUNCTION pgcolumnar.export_arrow(regclass, text)
	IS 'export a columnar table to an Arrow IPC stream file; returns rows written';

CREATE FUNCTION pgcolumnar.export_parquet(rel regclass, path text)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_export_parquet';

COMMENT ON FUNCTION pgcolumnar.export_parquet(regclass, text)
	IS 'export a columnar table to a Parquet file; returns rows written';

CREATE FUNCTION pgcolumnar.parallel_export_parquet(target regclass, path text,
												   workers int DEFAULT NULL)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_parallel_export_parquet';

COMMENT ON FUNCTION pgcolumnar.parallel_export_parquet(regclass, text, int)
	IS 'parallel Parquet export using read-only background workers into a directory readable by pgcolumnar.read_parquet: a single columnar table split by row-group ranges, or a partitioned columnar table one file per partition; returns rows written (#300)';

CREATE FUNCTION pgcolumnar.import_arrow(rel regclass, path text)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_import_arrow';

COMMENT ON FUNCTION pgcolumnar.import_arrow(regclass, text)
	IS 'insert rows from an Arrow IPC stream file into a columnar table; returns rows inserted';

CREATE FUNCTION pgcolumnar.import_parquet(rel regclass, path text)
	RETURNS bigint
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_import_parquet';

COMMENT ON FUNCTION pgcolumnar.import_parquet(regclass, text)
	IS 'insert rows from a Parquet file, directory, or glob into a table; returns rows inserted (gap 27)';

CREATE FUNCTION pgcolumnar.parquet_schema(path text)
	RETURNS TABLE(column_name text, data_type text, nullable boolean, field_id integer)
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_parquet_schema';

COMMENT ON FUNCTION pgcolumnar.parquet_schema(text)
	IS 'report the leaf columns of a Parquet file and the PostgreSQL type each maps to; for a directory or glob, of its first file; field_id is the SchemaElement field id Iceberg projects by, NULL when the writer emitted none (Phase G scan core, #388)';

CREATE FUNCTION pgcolumnar.read_parquet(path text)
	RETURNS SETOF record
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_read_parquet';

COMMENT ON FUNCTION pgcolumnar.read_parquet(text)
	IS 'read a Parquet file, directory, or glob in place as a set of rows; requires a column definition list covering every leaf column, e.g. SELECT * FROM pgcolumnar.read_parquet(path) AS t(id int, name text) (Phase G)';

CREATE FUNCTION pgcolumnar.read_parquet(path text, field_ids integer[])
	RETURNS SETOF record
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_read_parquet';

COMMENT ON FUNCTION pgcolumnar.read_parquet(text, integer[])
	IS 'read a Parquet file by field id: output column i is bound to the file column whose Parquet field id equals field_ids[i], reading only those columns in that order, e.g. SELECT * FROM pgcolumnar.read_parquet(path, ARRAY[12,7]) AS t(c int, a int) (#388)';

CREATE FUNCTION pgcolumnar.read_avro_manifest(path text)
	RETURNS TABLE(status integer, content integer, file_path text,
				  file_format text, record_count bigint,
				  file_size_in_bytes bigint, partition text,
				  sequence_number bigint)
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_read_avro_manifest';

COMMENT ON FUNCTION pgcolumnar.read_avro_manifest(text)
	IS 'decode an Apache Iceberg Avro manifest file and report its data-file entries; the first step of Iceberg read support (#388)';

CREATE FUNCTION pgcolumnar.read_manifest_list(path text)
	RETURNS TABLE(manifest_path text, manifest_length bigint, content integer,
				  partition_spec_id integer, added_files_count integer,
				  existing_files_count integer, deleted_files_count integer,
				  added_rows_count bigint, existing_rows_count bigint,
				  deleted_rows_count bigint, sequence_number bigint,
				  min_sequence_number bigint, added_snapshot_id bigint)
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_read_manifest_list';

COMMENT ON FUNCTION pgcolumnar.read_manifest_list(text)
	IS 'decode an Apache Iceberg snapshot manifest-list Avro file and report the manifest files it points at (#388)';

CREATE FUNCTION pgcolumnar.iceberg_current_snapshot(metadata_path text)
	RETURNS TABLE(snapshot_id bigint, parent_snapshot_id bigint,
				  sequence_number bigint, timestamp_ms bigint, operation text,
				  manifest_list text, schema_id integer)
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_current_snapshot';

COMMENT ON FUNCTION pgcolumnar.iceberg_current_snapshot(text)
	IS 'read an Apache Iceberg table metadata.json and report its current snapshot (#388)';

CREATE FUNCTION pgcolumnar.iceberg_data_files(metadata_path text)
	RETURNS TABLE(file_path text, file_format text, record_count bigint,
				  partition text)
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_data_files';

COMMENT ON FUNCTION pgcolumnar.iceberg_data_files(text)
	IS 'list the live data files of an Apache Iceberg table current snapshot; refuses tables with delete files (#388)';

CREATE FUNCTION pgcolumnar.iceberg_scan(metadata_path text)
	RETURNS SETOF record
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_scan';

COMMENT ON FUNCTION pgcolumnar.iceberg_scan(text)
	IS 'read an Apache Iceberg table at its current snapshot; supply a column definition list, whose names resolve to the table schema field ids, e.g. SELECT * FROM pgcolumnar.iceberg_scan(path) AS t(id bigint, region text); applies position, equality, and deletion-vector deletes (#388)';

CREATE FUNCTION pgcolumnar.iceberg_rest_table_location(catalog_uri text,
													   namespace text,
													   table_name text)
	RETURNS text
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_table_location';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_table_location(text, text, text)
	IS 'resolve the current metadata-location of a table named by an Iceberg REST catalog (catalog URI + namespace + table); the bearer token is read from the server environment variable PGCOLUMNAR_ICEBERG_REST_TOKEN, never a SQL argument (#388)';

CREATE FUNCTION pgcolumnar.iceberg_rest_scan(catalog_uri text,
											 namespace text,
											 table_name text)
	RETURNS SETOF record
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_scan';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_scan(text, text, text)
	IS 'read a table named by an Iceberg REST catalog at its current snapshot; supply a column definition list, as for iceberg_scan; the metadata location is resolved through the catalog and read like any other Iceberg table (#388)';

CREATE FUNCTION pgcolumnar.iceberg_rest_namespaces(catalog_uri text)
	RETURNS SETOF text
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_namespaces';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_namespaces(text)
	IS 'list the namespaces of an Iceberg REST catalog, one per row, multi-level namespaces dot-joined (#388)';

CREATE FUNCTION pgcolumnar.iceberg_rest_tables(catalog_uri text, namespace text)
	RETURNS SETOF text
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_rest_tables';

COMMENT ON FUNCTION pgcolumnar.iceberg_rest_tables(text, text)
	IS 'list the table names in a namespace of an Iceberg REST catalog, one per row (#388)';

/* ---------------------------------------------------------------------------
 * Parquet foreign-data wrapper (Phase G)
 *
 * A foreign table over a Parquet file, a directory of *.parquet files, or a glob
 * pattern, read as one relation; its column definitions are bound against every
 * file by position, like read_parquet's column list. Usage:
 *   CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;
 *   CREATE FOREIGN TABLE ft (id int, name text) SERVER pq
 *       OPTIONS (path '/data/f.parquet');
 * ------------------------------------------------------------------------- */

CREATE FUNCTION pgcolumnar.parquet_fdw_handler()
	RETURNS fdw_handler
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_parquet_fdw_handler';

CREATE FUNCTION pgcolumnar.parquet_fdw_validator(text[], oid)
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_parquet_fdw_validator';

CREATE FOREIGN DATA WRAPPER pgcolumnar_parquet
	HANDLER pgcolumnar.parquet_fdw_handler
	VALIDATOR pgcolumnar.parquet_fdw_validator;

COMMENT ON FOREIGN DATA WRAPPER pgcolumnar_parquet
	IS 'read a Parquet file, directory, or glob as a foreign table; table option: path (Phase G)';

CREATE FUNCTION pgcolumnar.iceberg_fdw_handler()
	RETURNS fdw_handler
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_fdw_handler';

CREATE FUNCTION pgcolumnar.iceberg_fdw_validator(text[], oid)
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_fdw_validator';

CREATE FOREIGN DATA WRAPPER pgcolumnar_iceberg
	HANDLER pgcolumnar.iceberg_fdw_handler
	VALIDATOR pgcolumnar.iceberg_fdw_validator;

COMMENT ON FOREIGN DATA WRAPPER pgcolumnar_iceberg
	IS 'read an Apache Iceberg table as a foreign table, pruning data files by a predicate on an identity-partition column; table option: metadata_path (#388)';

CREATE FUNCTION pgcolumnar.iceberg_catalog_fdw_validator(text[], oid)
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_iceberg_catalog_validator';

-- Validator-only wrapper (no HANDLER): a REST catalog creates no foreign tables.
-- A SERVER under it holds catalog_uri; a USER MAPPING holds the per-role token.
CREATE FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
	VALIDATOR pgcolumnar.iceberg_catalog_fdw_validator;

COMMENT ON FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
	IS 'name an Iceberg REST catalog: a SERVER holds catalog_uri, a USER MAPPING holds the bearer token; the iceberg_rest_* functions accept a server name in place of a catalog URI (#656)';

CREATE FUNCTION pgcolumnar.vm_selftest(rel regclass, blk int)
	RETURNS boolean
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_vm_selftest';

COMMENT ON FUNCTION pgcolumnar.vm_selftest(regclass, int)
	IS 'gap 28 phase-1 self-test: set a VM-fork all-visible bit and read it back';

CREATE FUNCTION pgcolumnar.vm_is_visible(rel regclass, blk int)
	RETURNS boolean
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_vm_is_visible';

COMMENT ON FUNCTION pgcolumnar.vm_is_visible(regclass, int)
	IS 'gap 28: is the synthetic block marked all-visible in the VM fork?';

-- These two are phase-1 self-test helpers, not user API. CREATE FUNCTION grants
-- EXECUTE to PUBLIC, which put a visibility-map write behind nothing but USAGE on
-- this schema -- and the documented maintenance API lives in the same schema, so
-- any deployment exposing that also exposed these (#558). The C code checks
-- ownership as well; this REVOKE is the second layer, not the only one.
REVOKE ALL ON FUNCTION pgcolumnar.vm_selftest(regclass, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION pgcolumnar.vm_is_visible(regclass, int) FROM PUBLIC;

CREATE FUNCTION pgcolumnar.vacuum_full(
	schema name DEFAULT 'public',
	sleep_time real DEFAULT 0.0,
	stripe_count int DEFAULT 0)
	RETURNS void
	LANGUAGE plpgsql
	AS $vacuum_full$
DECLARE
	r record;
BEGIN
	FOR r IN
		SELECT c.oid AS reloid
		FROM pg_class c
		JOIN pg_am a ON a.oid = c.relam
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE a.amname = 'pgcolumnar'
		  AND c.relkind = 'r'
		  AND n.nspname = vacuum_full.schema
	LOOP
		PERFORM pgcolumnar.vacuum(r.reloid::regclass, stripe_count);
		IF sleep_time > 0 THEN
			PERFORM pg_sleep(sleep_time);
		END IF;
	END LOOP;
END;
$vacuum_full$;

COMMENT ON FUNCTION pgcolumnar.vacuum_full(name, real, int)
	IS 'compact every columnar table in a schema';

-- ---------------------------------------------------------------------------
-- Parallel bulk ingest (#300). Phase 1: the file range splitter. Given a
-- server-side file and a worker count, return workers+1 ascending byte offsets
-- that partition the file into that many line-aligned ranges, so a parallel load
-- can hand range [off[i], off[i+1]) to worker i. The ranges are record-aligned
-- for COPY *text* format only (a raw newline always ends a text record); they are
-- NOT safe for CSV, whose quoted fields may contain literal newlines. `workers` is
-- capped internally so a huge value cannot allocate unbounded memory.
-- ---------------------------------------------------------------------------
CREATE FUNCTION pgcolumnar.file_split_offsets(path text, workers int)
	RETURNS bigint[]
	LANGUAGE C STRICT
	AS 'MODULE_PATHNAME', 'pgcolumnar_file_split_offsets';

COMMENT ON FUNCTION pgcolumnar.file_split_offsets(text, int)
	IS 'byte offsets that split a COPY text-format file into N record-aligned ranges (#300)';

-- Parallel bulk ingest: atomically load a server-side COPY text-format file into a
-- RANGE-partitioned columnar table across N background workers. Each worker loads a
-- DISTINCT set of partitions (distinct storage), the only shape pgColumnar allows a
-- parallel AND atomic bulk load: concurrent writers to one non-partitioned table
-- serialize on the per-storage write lock and, under two-phase commit, deadlock
-- (single-table parallel load is a planned columnar-core enhancement). Loaders
-- PREPARE; a coordinator background worker COMMIT PREPAREDs them all, or ROLLBACK
-- PREPAREDs on any failure. Returns rows loaded. The target is either a single
-- columnar table (workers write its one storage concurrently) or a RANGE-partitioned
-- table (each worker loads a distinct partition; requires a single-column
-- numeric/date-time key, no DEFAULT partition, and the file sorted ascending by that
-- key). COPY text format, and max_prepared_transactions >= workers. workers => NULL
-- derives a default from max_parallel_workers.
--
-- Two behaviors to know: (1) the load commits in background workers, INDEPENDENTLY
-- of the calling transaction, so its rows survive a subsequent ROLLBACK of the
-- caller -- treat the call like a COMMIT. (2) Do not call it while the calling
-- transaction holds a lock on the target (e.g. after LOCK TABLE or a write to it):
-- the loaders would block on that lock and the wait is invisible to the deadlock
-- detector. See design/PARALLEL_COPY_PLAN.md.
CREATE FUNCTION pgcolumnar.parallel_copy(target regclass, filename text,
										 workers int DEFAULT NULL,
										 dedup boolean DEFAULT false)
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_parallel_copy';

COMMENT ON FUNCTION pgcolumnar.parallel_copy(regclass, text, int, boolean)
	IS 'atomic parallel bulk load of a COPY text file into a columnar table using background workers: a single columnar table (any row order), or a RANGE-partitioned columnar table sorted by the partition key with one distinct partition set per worker (#300). With dedup, a file already loaded into this table is refused rather than loaded twice (#403)';

/*
 * Per-column statistics without reading the whole table (#414).
 *
 * Core ANALYZE decodes essentially the entire table. It samples a fixed 30,000
 * rows, and on a table of any size those rows fall in every row group, so every
 * group is decoded for every column. Measured on 3M rows x 20 columns, 1237 MB,
 * serial: ANALYZE costs 6,302 ms against 7,680 ms to decode all nineteen text
 * columns outright, while decoding just one column costs 268 ms.
 *
 * That cannot be recovered inside the table-AM callbacks, which is why this is a
 * function. acquire_sample_rows copies whole tuples (ExecCopySlotHeapTuple), so
 * the AM cannot decline to produce columns core is about to copy: ANALYZE of one
 * named column costs 6,073 ms against 6,302 ms for all twenty, a 6% saving. Nor
 * is there slack in which groups the sample touches -- at a tenth the
 * chunk_group_row_limit the cost was unchanged, because a fixed-size sample
 * touches proportionally more groups when they are smaller.
 *
 * Core ANALYZE remains the correctness path and is what autovacuum runs. This is
 * an opt-in accelerator for wide tables and, like pgcolumnar.vacuum(), nothing
 * schedules it: see #415.
 *
 * Collected so far, all of it exact rather than sampled, and all of it from ONE
 * read of the column: null_frac, n_distinct, the most-common values with their
 * frequencies, and a histogram of what remains once those are excluded.
 *
 * One read is the property that matters, not merely the source of each number.
 * null_frac came from the zone maps until #485, which was cheaper and was wrong
 * after a DELETE, because those counts describe what was written. Taking it from
 * the same read as the rest is what makes every statistic here describe one
 * population, which is the identity the planner's selectivity arithmetic needs.
 *
 * "Exact" is the whole difference and it is not a refinement of core's numbers.
 * Core samples 30,000 rows, so a value held by one row in 500,000 is missed
 * entirely and every range estimate above the sampled maximum collapses; a
 * frequency is right to about three digits rather than exactly. Reading the
 * column removes the sampling error rather than reducing it -- which is also why
 * core's own significance filter for the most-common list does not apply here,
 * as analyze_mcv_list() says itself at analyze.c:2995.
 */
CREATE FUNCTION pgcolumnar.analyze(rel regclass, columns text[] DEFAULT NULL)
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

	SELECT s.storage_id INTO sid
		FROM pgcolumnar.storage s
		WHERE s.relation_oid = rel;

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

COMMENT ON FUNCTION pgcolumnar.analyze(regclass, text[])
	IS 'collect per-column statistics by reading one column at a time rather than sampling every column (#414); null_frac, n_distinct and the most-common frequencies all come from that read, so they describe one population (#485); core ANALYZE remains the correctness path and nothing schedules this, see #415';

-- pgcolumnar.maintenance_due (#415): report whether an online maintenance verb
-- is worth running on a columnar table, from table statistics alone. A pure
-- report -- it takes no lock and rewrites nothing. A cron job or an operator
-- consults it; a background worker, if one is ever built, is a thin consumer of
-- the same verdict (see #415).
--
-- Thresholds are PARAMETERS, not GUCs: the pgcolumnar GUC prefix is reserved
-- (MarkGUCPrefixReserved), so an unregistered pgcolumnar.* GUC is rejected, and
-- a report is better configured at the call site than globally. The defaults are
-- measured on #415 -- compact_rewrite's overhead reaches ~10% of a query at a
-- deleted fraction of 0.2 (the knee), and clustering decay is already costly at
-- the smallest fraction measured, so recluster gates low at 0.05.
--
-- recluster_due gates on the sort key EXISTING. sort_status() reports a
-- never-ordered table as entirely appended, because it has no sorted run; that
-- is not decay and there is no ordering to restore, so the sort-key guard
-- suppresses the recommendation there.
CREATE FUNCTION pgcolumnar.maintenance_due(
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

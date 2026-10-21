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
	-- Nothing drops rows on its own: expire is called by name, or by the
	-- daemon when pgcolumnar.autovacuum_expire is on (off by default, #1290).
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
	null_count bigint NOT NULL,
	max_upper bytea                   -- range columns only (#1144)
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
	IS 'drop row groups whose rows are all older than the retention declared by set_options(ttl_column, ttl_interval), without rewriting them (#403)';

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

CREATE FUNCTION pgcolumnar.cluster_hilbert(
	tablename regclass,
	VARIADIC columns name[])
	RETURNS void
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_cluster_hilbert';

COMMENT ON FUNCTION pgcolumnar.cluster_hilbert(regclass, name[])
	IS 'eager reorg on the Hilbert curve: as cluster(), but the rows are ordered by the Hilbert index over the given columns, which keeps neighbouring keys neighbouring in storage more tightly than Z-order does. Holds AccessExclusiveLock like CLUSTER/VACUUM FULL; the online counterpart is recluster_hilbert() (#889)';

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

CREATE FUNCTION pgcolumnar.recluster_hilbert(
	tablename regclass,
	VARIADIC columns name[])
	RETURNS bigint
	LANGUAGE C
	AS 'MODULE_PATHNAME', 'pgcolumnar_recluster_hilbert';

COMMENT ON FUNCTION pgcolumnar.recluster_hilbert(regclass, name[])
	IS 'lazy online reclustering on the Hilbert curve: as recluster(), but re-establishes Hilbert clustering over the given columns under ShareUpdateExclusiveLock (concurrent reads and writes). The curve is sticky -- plain recluster() maintains a Hilbert table rather than converting it, and naming this verb is how a Z-ordered table is switched (#889)';

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
	IF EXISTS (SELECT 1 FROM pg_foreign_table t
			   WHERE ('path=' || path) = ANY (t.ftoptions)) THEN
		RAISE EXCEPTION 'file "%" is read by a foreign table in this database', path
			USING ERRCODE = 'duplicate_file';
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
	moved := pgcolumnar.export_parquet(partition, path);
	-- Under the lock nothing can commit to the partition, so a statement taken
	-- now sees what the export saw. A difference is an error, never a loss.
	EXECUTE format('SELECT count(*) FROM %s', partition) INTO seen;
	IF seen <> moved THEN
		RAISE EXCEPTION 'exported % rows but partition "%" holds %', moved, partition, seen
			USING ERRCODE = 'internal_error';
	END IF;
	IF fault = 'export' THEN RAISE EXCEPTION 'injected fault after export'; END IF;
	IF fault = 'pause' THEN PERFORM pg_sleep(2); END IF;

	-- From here the parent is held ACCESS EXCLUSIVE until commit: catalog work only.
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

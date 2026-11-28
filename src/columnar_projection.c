/*-------------------------------------------------------------------------
 *
 * pgcolumnar_projection.c
 *		DDL for multiple physical projections (gap 26, format 2.2).
 *
 * A projection is a named, ordered subset of a table's columns stored as its
 * own columnar storage, sorted on its sort key, sharing the table's row-number
 * identity space (the C-Store model; see design/gaps/26-*). The catalog
 * (pgcolumnar.projection) and the add/drop DDL are provided here: declaring a
 * projection allocates its storage id, records the catalog row, and back-fills
 * the projection's storage from existing rows (PgColumnarBackfillProjection). Read
 * paths (read_projection, reconstruct_via_projection) are also provided.
 *
 * projection_id 0 is the implicit base projection (all live columns, insert
 * order). A table with no pgcolumnar.projection rows has a single implicit base
 * projection, so pre-existing and 2.0/2.1 tables are unaffected. The base row is
 * recorded lazily the first time a projection is added.
 *
 * Written fresh for pgColumnar; it does not reuse any upstream file.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_metadata.h"
#include "columnar_write_state.h"
#include "access/table.h"
#include "catalog/pg_type.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "utils/acl.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"
#include "utils/ruleutils.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/tuplestore.h"

PG_FUNCTION_INFO_V1(pgcolumnar_add_projection);
PG_FUNCTION_INFO_V1(pgcolumnar_drop_projection);
PG_FUNCTION_INFO_V1(pgcolumnar_read_projection);

/*
 * Collect the live (non-dropped) attribute numbers of a relation, in attnum
 * order. Returns a palloc'd int16 array and sets *n.
 */
static int16 *
live_attnums(Relation rel, int *n)
{
	TupleDesc	tupdesc = RelationGetDescr(rel);
	int16	   *out = palloc(sizeof(int16) * tupdesc->natts);
	int			count = 0;
	int			i;

	for (i = 0; i < tupdesc->natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);

		if (att->attisdropped)
			continue;
		out[count++] = (int16) att->attnum;
	}
	*n = count;
	return out;
}

/*
 * Resolve a text[] of column names to an int16 array of attnums against relid,
 * validating that each names a live user column and that there are no
 * duplicates. Returns a palloc'd array and sets *n. Errors on any problem.
 */
static int16 *
resolve_columns(Oid relid, ArrayType *names, const char *what, int *n)
{
	Datum	   *elems;
	bool	   *nulls;
	int			count;
	int16	   *out;
	int			i,
				j;

	deconstruct_array(names, TEXTOID, -1, false, TYPALIGN_INT,
					  &elems, &nulls, &count);

	out = (count > 0) ? palloc(sizeof(int16) * count) : NULL;
	for (i = 0; i < count; i++)
	{
		char	   *colname;
		AttrNumber	attno;

		if (nulls[i])
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("%s must not contain NULL", what)));

		colname = text_to_cstring(DatumGetTextPP(elems[i]));
		attno = get_attnum(relid, colname);
		if (attno == InvalidAttrNumber || attno < 0)
			ereport(ERROR,
					(errcode(ERRCODE_UNDEFINED_COLUMN),
					 errmsg("column \"%s\" does not exist", colname)));

		for (j = 0; j < i; j++)
			if (out[j] == (int16) attno)
				ereport(ERROR,
						(errcode(ERRCODE_DUPLICATE_COLUMN),
						 errmsg("column \"%s\" appears more than once in %s",
								colname, what)));
		out[i] = (int16) attno;
	}
	*n = count;
	return out;
}

/*
 * Ensure the base projection row (projection_id 0) exists for this table,
 * recording all live columns in insert order. No-op if already present.
 */
static void
record_base_projection(Relation rel, uint64 storageId, List *existing)
{
	ListCell   *lc;
	PgColumnarProjection base;

	foreach(lc, existing)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (p->projectionId == 0)
			return;
	}

	memset(&base, 0, sizeof(base));
	base.storageId = storageId;
	base.projectionId = 0;
	base.name = "base";
	base.projStorageId = storageId;		/* base shares the table's storage */
	base.sortKey = NULL;
	base.sortKeyLen = 0;
	base.columns = live_attnums(rel, &base.columnsLen);
	PgColumnarInsertProjectionRow(&base);
}

/*
 * declaration_resolves
 *		Does every column name in this declaration still name a live column?
 *
 * resolve_columns raises on a name it cannot resolve, which is right when a user
 * is declaring a projection and wrong when a rewrite is repairing one: there the
 * error would propagate out of whatever statement triggered the repair. Asked
 * first, and separately, so the caller can decline to materialise instead.
 *
 * A declaration goes stale because ALTER TABLE ... RENAME COLUMN does not carry
 * the rename through projection_declaration's columns and sort_key (#888).
 * Measured before this existed: the repair raised `column "a" does not exist`
 * inside an unrelated ALTER TABLE ... ALTER COLUMN id TYPE bigint and the type
 * change rolled back.
 */
static bool
declaration_resolves(Oid relid, ArrayType *names)
{
	Datum	   *elems;
	bool	   *nulls;
	int			count;
	int			i;

	if (names == NULL)
		return true;

	deconstruct_array(names, TEXTOID, -1, false, TYPALIGN_INT,
					  &elems, &nulls, &count);

	for (i = 0; i < count; i++)
	{
		AttrNumber	attno;

		if (nulls[i])
			return false;

		attno = get_attnum(relid, text_to_cstring(DatumGetTextPP(elems[i])));
		if (attno == InvalidAttrNumber || attno < 0)
			return false;
	}

	return true;
}

/*
 * materialize_projection
 *		Record one projection under the relation's current storage id and
 *		back-fill it from the rows the table holds now.
 *
 * Extracted from pgcolumnar_add_projection so that the post-rewrite re-record
 * (PgColumnarRerecordProjectionsAfterRewrite) drives the SAME code rather than a
 * second copy of it. Everything here is per-materialisation; what stayed behind
 * in add_projection is what belongs to the DECLARING act -- the owner check and
 * the declaration row itself, neither of which a re-record repeats.
 *
 * Takes the column lists as name arrays, the form the declaration holds, and
 * resolves them against the relation as it is now. add_projection passes the
 * user's arrays straight through, so its behaviour is unchanged.
 */
static void
materialize_projection(Relation rel, char *projname, ArrayType *colsArr,
					   ArrayType *sortArr)
{
	Oid			relid = RelationGetRelid(rel);
	uint64		storageId = PgColumnarStorageId(rel);
	List	   *existing;
	PgColumnarProjection proj;
	ListCell   *lc;
	int			nextId = 1;
	int			i,
				j;

	existing = PgColumnarListProjections(storageId);
	record_base_projection(rel, storageId, existing);
	/* re-read so the base row is included when picking the next id / name check */
	existing = PgColumnarListProjections(storageId);

	memset(&proj, 0, sizeof(proj));
	proj.storageId = storageId;
	proj.name = projname;
	proj.columns = resolve_columns(relid, colsArr, "columns", &proj.columnsLen);
	if (proj.columnsLen == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("a projection must have at least one column")));

	if (sortArr != NULL)
		proj.sortKey = resolve_columns(relid, sortArr, "sort_key", &proj.sortKeyLen);

	/* every sort_key column must be part of the projection's columns */
	for (i = 0; i < proj.sortKeyLen; i++)
	{
		bool		found = false;

		for (j = 0; j < proj.columnsLen; j++)
			if (proj.columns[j] == proj.sortKey[i])
			{
				found = true;
				break;
			}
		if (!found)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("sort_key column \"%s\" is not in the projection column list",
							get_attname(relid, proj.sortKey[i], false))));
	}

	/* name must be unique for this table; next id is max + 1 */
	foreach(lc, existing)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (strcmp(p->name, projname) == 0)
			ereport(ERROR,
					(errcode(ERRCODE_DUPLICATE_OBJECT),
					 errmsg("projection \"%s\" already exists on \"%s\"",
							projname, get_rel_name(relid))));
		if (p->projectionId >= nextId)
			nextId = p->projectionId + 1;
	}

	proj.projectionId = nextId;
	proj.projStorageId = PgColumnarNextStorageId();
	PgColumnarInsertProjectionRow(&proj);

	/* populate the projection from the table's existing rows (gap 26 back-fill) */
	PgColumnarBackfillProjection(rel, &proj);

	/*
	 * An open write state on this relation cached its projection-writer list on
	 * its first row and latched it, including when the list was empty because no
	 * projection existed yet. The projection set has just changed, so drop that
	 * cache: without this, every later write in the same transaction skips the
	 * projection just created and does so silently (#875).
	 *
	 * After the back-fill, not before. The back-fill populates the projection
	 * from the rows that already exist; resetting first would change what it
	 * sees rather than what follows it.
	 */
	PgColumnarResetProjectionWritersForRelation(relid);
}

/*
 * pgcolumnar.add_projection(rel, name, columns text[], sort_key text[])
 *		Declare a projection: a named column subset sorted on sort_key.
 */
Datum
pgcolumnar_add_projection(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *projname;
	ArrayType  *colsArr;
	ArrayType  *sortArr;
	Relation	rel;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1) || PG_ARGISNULL(2))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("rel, name, and columns must not be NULL")));

	relid = PG_GETARG_OID(0);
	projname = text_to_cstring(PG_GETARG_TEXT_PP(1));
	colsArr = PG_GETARG_ARRAYTYPE_P(2);
	sortArr = PG_ARGISNULL(3) ? NULL : PG_GETARG_ARRAYTYPE_P(3);

	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not a columnar table",
						get_rel_name(relid))));

	if (strlen(projname) == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("projection name must not be empty")));
	if (strlen(projname) >= NAMEDATALEN)
		ereport(ERROR,
				(errcode(ERRCODE_NAME_TOO_LONG),
				 errmsg("projection name \"%s\" is too long", projname)));

	PgColumnarRequireTableOwnerByOid(relid);

	/*
	 * ShareLock: block concurrent INSERT/UPDATE/DELETE (RowExclusiveLock) while
	 * we back-fill the projection from existing rows, so no concurrently written
	 * row is missed -- the same lock non-concurrent CREATE INDEX takes. Reads are
	 * unaffected. (A CONCURRENTLY variant is future work.)
	 */
	rel = table_open(relid, ShareLock);

	materialize_projection(rel, projname, colsArr, sortArr);

	/*
	 * Record the declaration behind it, by relation and column name, so a dump
	 * and restore can carry the intent even though it cannot carry the storage
	 * (#266). Written here rather than in the SQL binding so that a projection
	 * cannot come into existence without one.
	 */
	PgColumnarRecordProjectionDeclaration(relid, projname, colsArr,
										sortArr ? sortArr :
										construct_empty_array(TEXTOID));

	/*
	 * NoLock: keep the ShareLock until this transaction ends, as CREATE INDEX
	 * does (#2215). Releasing it here let a writer in another session start
	 * while the projection was still uncommitted: the writer could not see the
	 * projection, so its rows never reached it, and once this transaction
	 * committed a projection scan returned wrong answers with no error. Holding
	 * it also makes pgcolumnar.parallel_copy in this same transaction refuse up
	 * front (#2183's own-lock refusal), since its loaders run in their own
	 * transactions and could not see the uncommitted projection either.
	 */
	table_close(rel, NoLock);
	PG_RETURN_VOID();
}

/*
 * projection_hint_relname
 *		The relation name a HINT can be pasted from: schema-qualified and quoted.
 *
 * get_rel_name() alone is unqualified, and a HINT that names a relation outside
 * the reader's search_path tells them to run a statement that fails. Measured on
 * 18.4: a stale declaration on a table in schema "s" produced
 * HINT: ... pgcolumnar.rebuild_projections('t'), and running exactly that gave
 * ERROR: relation "t" does not exist (@jdatcmd, #892 review).
 */
static char *
projection_hint_relname(Oid relid)
{
	return quote_qualified_identifier(get_namespace_name(get_rel_namespace(relid)),
									  get_rel_name(relid));
}

/*
 * PgColumnarRerecordProjectionsAfterRewrite
 *		Re-materialise this relation's declared projections under whatever
 *		storage id it has NOW (#876, #887).
 *
 * A rewrite mints a new base storage id, and pgcolumnar.projection is keyed by
 * that id, so every projection row a rewrite leaves behind describes storage the
 * relation no longer has. pgcolumnar_delete_storage_tree removes those rows
 * (#867), which leaves read_projection raising 42704 for a projection that is
 * still declared over an intact table. This restores them.
 *
 * Skips a declaration whose projection is already recorded under the current
 * storage id, so it writes nothing for a statement that rewrote nothing, and
 * nothing for the paths that re-record for themselves (pgcolumnar_compact_relation
 * and its zorder sibling). test/projection_rewrite.sh carries those three as
 * regression arms rather than leaving the property to this comment.
 *
 * Re-derived from the DECLARATION rather than copied from the old rows, for two
 * reasons. The old rows are already gone on the TRUNCATE path. And a rewrite can
 * change the relation's shape: ALTER TABLE ... ADD COLUMN with a volatile
 * default rewrites AND adds a column, and the base projection records all live
 * columns, so copying the old row forward would leave projection 0 naming a
 * stale column set. Resolving names against the relation as it is now gets both
 * cases right for the same reason.
 *
 * Not called from pgcolumnar_relation_set_new_filelocator, which is where #887
 * proposed it. That callback cannot do this job: a rewriting ALTER TABLE reaches
 * it on the TRANSIENT relation make_new_heap builds, with no columnar fork and a
 * different oid, so neither the old storage id nor the projection list is ever
 * in scope. Measured on 18.4 with the callback logging its own relid: TRUNCATE
 * arrives as the user's relation, ALTER COLUMN TYPE arrives as pg_temp_<oid>.
 */
void
PgColumnarRerecordProjectionsAfterRewrite(Oid relid)
{
	List	   *decls;
	ListCell   *lc;
	Relation	rel;
	uint64		storageId;
	List	   *existing;

	if (!PgColumnarIsColumnarRelation(relid))
		return;

	decls = PgColumnarListProjectionDeclarations(relid);
	if (decls == NIL)
		return;

	/*
	 * DECIDE FIRST, UNDER AccessShareLock, AND ONLY THEN TAKE ShareLock.
	 *
	 * The repair is reached for every AlterTableStmt on a relation with a declared
	 * projection, not only for one that rewrote it. Measured: three metadata-only
	 * statements -- SET (fillfactor), ALTER COLUMN SET STATISTICS,
	 * SET (autovacuum_enabled) -- opened the relation with ShareLock three times
	 * with nothing to repair (@jdatcmd, #892 review).
	 *
	 * ShareLock conflicts with RowExclusiveLock. The old comment justified it as
	 * "the statement already holds AccessExclusiveLock, so this takes nothing new",
	 * which is true of a rewriting statement and false of the metadata-only ones
	 * that also arrive here. So the cheap question is asked under AccessShareLock,
	 * which conflicts with nothing a writer takes, and the heavier lock is taken
	 * only when there is a projection to materialise.
	 */
	rel = table_open(relid, AccessShareLock);
	storageId = PgColumnarStorageId(rel);
	existing = PgColumnarListProjections(storageId);

	{
		bool		anyMissing = false;

		foreach(lc, decls)
		{
			PgColumnarProjectionDeclaration *d =
				(PgColumnarProjectionDeclaration *) lfirst(lc);
			ListCell   *lc2;
			bool		present = false;

			foreach(lc2, existing)
			{
				PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc2);

				if (p->projectionId > 0 && strcmp(p->name, d->name) == 0)
				{
					present = true;
					break;
				}
			}
			if (!present)
			{
				anyMissing = true;
				break;
			}
		}

		if (!anyMissing)
		{
			table_close(rel, AccessShareLock);
			return;
		}
	}

	/*
	 * There is work to do, so now take the lock the back-fill needs: it reads
	 * every live row, and concurrent writers must be held off exactly as they are
	 * when a projection is first created.
	 */
	table_close(rel, AccessShareLock);
	rel = table_open(relid, ShareLock);
	storageId = PgColumnarStorageId(rel);
	existing = PgColumnarListProjections(storageId);

	foreach(lc, decls)
	{
		PgColumnarProjectionDeclaration *d =
			(PgColumnarProjectionDeclaration *) lfirst(lc);
		ListCell   *lc2;
		bool		present = false;

		foreach(lc2, existing)
		{
			PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc2);

			if (p->projectionId > 0 && strcmp(p->name, d->name) == 0)
			{
				present = true;
				break;
			}
		}
		if (present)
			continue;

		/*
		 * A declaration naming a column this relation no longer has cannot be
		 * materialised, and must not take the statement that triggered this
		 * repair down with it. WARNING and move on: the declaration survives,
		 * so pgcolumnar.rebuild_projections() remains the recovery once the
		 * names are correct again.
		 */
		if (!declaration_resolves(relid, d->columns) ||
			!declaration_resolves(relid, d->sortKey))
		{
			ereport(WARNING,
					(errcode(ERRCODE_UNDEFINED_COLUMN),
					 errmsg("could not restore projection \"%s\" on \"%s\" after rewrite",
							d->name, projection_hint_relname(relid)),
					 errdetail("Its declaration names a column the table no longer has."),
	/*
	 * add_projection, not "correct the declaration": there is no operation that
	 * edits a declaration in place, and the two obvious alternatives were measured
	 * to fail here. rebuild_projections() re-runs the same stale declaration and
	 * raises the same missing-column error, and drop_projection() refuses with
	 * 42704 because the projection row is exactly what is absent. add_projection()
	 * with the same name replaces the declaration and materialises it: measured,
	 * it restored all 200 rows on a table whose declaration named a dropped column.
	 */
					 errhint("Call pgcolumnar.add_projection(%s, %s, ...) again, naming columns the table has. That replaces the declaration.",
							 quote_literal_cstr(projection_hint_relname(relid)),
							 quote_literal_cstr(d->name))));
			continue;
		}

		/*
		 * A repair that cannot finish must not abort the statement that triggered
		 * it. declaration_resolves above catches the one failure mode we know
		 * about; materialize_projection can raise for others, and anything it
		 * raises would otherwise propagate into a statement that succeeds on main
		 * (@jdatcmd, #892 review).
		 *
		 * An internal subtransaction is the only way to catch and continue: after
		 * an ERROR the transaction is unusable until it is rolled back, so this is
		 * the same shape plpgsql's EXCEPTION uses.
		 */
		{
			MemoryContext oldcxt = CurrentMemoryContext;
			ResourceOwner oldowner = CurrentResourceOwner;

			BeginInternalSubTransaction(NULL);
			PG_TRY();
			{
				materialize_projection(rel, d->name, d->columns, d->sortKey);
				ReleaseCurrentSubTransaction();
				MemoryContextSwitchTo(oldcxt);
				CurrentResourceOwner = oldowner;
			}
			PG_CATCH();
			{
				ErrorData  *edata;

				MemoryContextSwitchTo(oldcxt);
				edata = CopyErrorData();
				FlushErrorState();
				RollbackAndReleaseCurrentSubTransaction();
				MemoryContextSwitchTo(oldcxt);
				CurrentResourceOwner = oldowner;

				ereport(WARNING,
						(errcode(edata->sqlerrcode),
						 errmsg("could not restore projection \"%s\" on \"%s\" after rewrite",
								d->name, projection_hint_relname(relid)),
						 errdetail("%s", edata->message),
						 errhint("Call pgcolumnar.rebuild_projections(%s) once the cause is fixed.",
								 quote_literal_cstr(projection_hint_relname(relid)))));
				FreeErrorData(edata);
			}
			PG_END_TRY();
		}
		/* the new row must be visible to the next iteration's id/name check */
		CommandCounterIncrement();
		existing = PgColumnarListProjections(PgColumnarStorageId(rel));
	}

	/*
	 * NoLock, as in add_projection (#2215): a projection materialised here is
	 * uncommitted until the statement's transaction ends, and a writer let in
	 * before then would miss it. A rewriting statement holds ACCESS EXCLUSIVE
	 * anyway; a metadata-only ALTER TABLE does not, and reaches this point when
	 * a declaration has no projection.
	 */
	table_close(rel, NoLock);
}

/*
 * pgcolumnar.drop_projection(rel, name)
 *		Drop a declared projection. The base projection cannot be dropped.
 */
Datum
pgcolumnar_drop_projection(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *projname;
	Relation	rel;
	uint64		storageId;
	List	   *existing;
	ListCell   *lc;
	int			targetId = -1;
	uint64		targetStorageId = 0;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("rel and name must not be NULL")));

	relid = PG_GETARG_OID(0);
	projname = text_to_cstring(PG_GETARG_TEXT_PP(1));

	/*
	 * Ownership FIRST, then the relation type. Both precede table_open so a
	 * non-owner never reaches the lock manager, and doing them in this order
	 * means a non-owner is told only that they are not the owner: asking about
	 * an arbitrary relation must not report back whether it is columnar. The
	 * sibling entry points in columnar_vacuum.c and columnar_visibilitymap.c
	 * are ordered the same way.
	 */
	PgColumnarRequireTableOwnerByOid(relid);

	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not a columnar table",
						get_rel_name(relid))));

	/*
	 * SHARE ROW EXCLUSIVE, held to transaction end (#2215). It was SHARE UPDATE
	 * EXCLUSIVE, released at the end of this function, which does not conflict
	 * with a writer's ROW EXCLUSIVE at all. A writer in another session still
	 * saw the projection (the delete was uncommitted), wrote its rows into the
	 * projection's storage, and once this transaction committed those row
	 * groups described storage nothing named any more. The same held for
	 * pgcolumnar.parallel_copy in this transaction, whose loaders cannot see the
	 * uncommitted drop; holding a lock that conflicts with ROW EXCLUSIVE makes
	 * #2183's refusal stop it. SHARE ROW EXCLUSIVE rather than add_projection's
	 * SHARE because it also conflicts with itself, as SHARE UPDATE EXCLUSIVE
	 * did, so two drops of one projection still queue rather than race on the
	 * catalog row. Readers are not blocked.
	 */
	rel = table_open(relid, ShareRowExclusiveLock);
	storageId = PgColumnarStorageId(rel);
	existing = PgColumnarListProjections(storageId);

	foreach(lc, existing)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (strcmp(p->name, projname) == 0)
		{
			targetId = p->projectionId;
			targetStorageId = p->projStorageId;
			break;
		}
	}

	if (targetId < 0)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("projection \"%s\" does not exist on \"%s\"",
						projname, get_rel_name(relid)),
				 errhint("A declared projection is re-recorded automatically after a "
						 "rewrite (#887), so this is no longer the usual cause. It can "
						 "still read as absent when its declaration names a column the "
						 "table no longer has, which the rewrite reports as a WARNING, "
						 "or when the name given is the implicit base projection, which "
						 "is not readable by name. pgcolumnar.rebuild_projections() "
						 "re-records a declared projection once its declaration "
						 "resolves.")));
	if (targetId == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("the base projection cannot be dropped")));

	/*
	 * Free the projection's own storage before forgetting where it was. The
	 * comment that stood here said phase 1 wrote no data to a projection's
	 * storage so there was nothing to free; that stopped being true once
	 * projections were written, and the metadata was orphaned from then on --
	 * row groups, chunks, zone maps and bloom filters describing storage
	 * nothing could name any more, because the row naming it had just been
	 * deleted.
	 */
	if (targetStorageId != storageId)
		PgColumnarDeleteMetadata(targetStorageId);
	PgColumnarDeleteProjectionRow(storageId, targetId);

	/* and forget the declaration, so a later rebuild does not resurrect it (#266) */
	PgColumnarDeleteProjectionDeclaration(relid, projname);

	/*
	 * The same latched cache as add_projection's, in the other direction. A
	 * write earlier in this transaction cached a writer for the projection just
	 * deleted, and without this the writes that follow keep appending to it: the
	 * rows land in a projection storage whose catalog rows are already gone, and
	 * the transaction commits with an orphan. Measured 1 orphan storage id when
	 * the drop happens mid-transaction, 0 when it has the transaction to itself,
	 * which is what pins it to the cache rather than to the deletes above.
	 */
	PgColumnarResetProjectionWritersForRelation(relid);

	table_close(rel, NoLock);
	PG_RETURN_VOID();
}

/*
 * declaration_names_column
 *		Whether one text[] field of a declaration holds this column name.
 *		Names compare exactly, as identifiers do: "BB" is not bb.
 */
static bool
declaration_names_column(ArrayType *arr, const char *attname)
{
	Datum	   *elems;
	bool	   *nulls;
	int			nelems;
	int			i;

	if (arr == NULL)
		return false;
	deconstruct_array(arr, TEXTOID, -1, false, TYPALIGN_INT,
					  &elems, &nulls, &nelems);
	for (i = 0; i < nelems; i++)
	{
		if (!nulls[i] && strcmp(TextDatumGetCString(elems[i]), attname) == 0)
			return true;
	}
	return false;
}

/*
 * PgColumnarDropProjectionsOnColumn
 *		Drop every projection that stores a column being dropped (#2240).
 *
 * Called from the object-access hook for a column drop that did not come
 * through ALTER TABLE ... DROP COLUMN, which refuses first (see
 * pgcolumnar_reject_drop_projected_column). The other routes are a cascade:
 * ALTER TYPE ... DROP ATTRIBUTE ... CASCADE on a typed table, DROP DOMAIN ...
 * CASCADE, and any drop that reaches a column through pg_depend. Each of them
 * needs CASCADE before core drops the column, so dropping the projection with it
 * is what the caller asked for.
 *
 * Refusing here instead, as ALTER TABLE does, is not possible. Core sometimes
 * drops a column on its own just before it drops the whole table: DROP SCHEMA
 * ... CASCADE does this when the table was created before a domain one of its
 * columns uses. The hook cannot tell that drop from any other, and a refusal
 * made DROP SCHEMA fail.
 *
 * Left in place, the projection kept the dropped attnum. The next INSERT asked
 * typcache for type OID 0 and the table became unwritable. The declaration kept
 * the name, so a column later added under that name was taken for it.
 *
 * The materialized projection matches by attnum and the declaration by name.
 * A declaration can exist with no materialized projection (a rewrite reports
 * one that no longer resolves), so both are read, and each projection is
 * reported once.
 */
void
PgColumnarDropProjectionsOnColumn(Relation rel, AttrNumber attnum,
								  const char *attname)
{
	Oid			relid = RelationGetRelid(rel);
	uint64		storageId = PgColumnarStorageId(rel);
	List	   *dropped = NIL;
	ListCell   *lc;

	foreach(lc, PgColumnarListProjections(storageId))
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);
		int			i;

		if (p->projectionId == 0)
			continue;
		for (i = 0; i < p->columnsLen; i++)
		{
			if (p->columns[i] != attnum)
				continue;
			if (p->projStorageId != storageId)
				PgColumnarDeleteMetadata(p->projStorageId);
			PgColumnarDeleteProjectionRow(storageId, p->projectionId);
			dropped = lappend(dropped, pstrdup(p->name));
			break;
		}
	}

	foreach(lc, PgColumnarListProjectionDeclarations(relid))
	{
		PgColumnarProjectionDeclaration *d =
			(PgColumnarProjectionDeclaration *) lfirst(lc);
		bool		seen = false;
		ListCell   *dc;

		/* add_projection keeps every sort-key column in columns */
		if (!declaration_names_column(d->columns, attname))
			continue;
		PgColumnarDeleteProjectionDeclaration(relid, d->name);

		/* one notice per projection, materialized or only declared */
		foreach(dc, dropped)
		{
			if (strcmp((char *) lfirst(dc), d->name) == 0)
				seen = true;
		}
		if (!seen)
			dropped = lappend(dropped, d->name);
	}

	/*
	 * drop_projection also resets the relation's cached projection writers.
	 * This needs none: a write, this drop and another write in one
	 * transaction left no row group in the dropped storage without one
	 * (#2240). Each delete above has already made itself visible.
	 */
	foreach(lc, dropped)
		ereport(NOTICE,
				(errmsg("drop cascades to projection \"%s\" on table \"%s\"",
						(char *) lfirst(lc), RelationGetRelationName(rel))));
}

/*
 * pgcolumnar.read_projection(rel, name) -> setof text
 *		Debug/verification reader for a projection's storage (gap 26, phase 2).
 *		Scans the projection's stripes (in stored sort order), skips rows whose
 *		base row number is deleted or invisible per the base delete_vector/visibility,
 *		and returns each live row's projection columns rendered by their output
 *		functions and joined by '|' (a NULL column renders as \N). The base is
 *		flushed first so rows written earlier in this transaction are visible.
 */
Datum
pgcolumnar_read_projection(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *projname;
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Relation	rel;
	uint64		storageId;
	List	   *projs;
	ListCell   *lc;
	PgColumnarProjection *proj = NULL;
	TupleDesc	projTupdesc;
	TupleDesc	retdesc;
	Tuplestorestate *tupstore;
	MemoryContext oldContext;
	FmgrInfo   *outFns;
	int			ncols;
	int			i;
	Snapshot	snap;
	PgColumnarReadState *readState;
	Datum	   *rvals;
	bool	   *rnulls;
	uint64		projRowNum;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("rel and name must not be NULL")));
	relid = PG_GETARG_OID(0);
	projname = text_to_cstring(PG_GETARG_TEXT_PP(1));

	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		!(rsinfo->allowedModes & SFRM_Materialize))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in context that cannot accept a set")));

	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not a columnar table", get_rel_name(relid))));

	/*
	 * SELECT on the BASE relation, checked before a single row is read (#562).
	 *
	 * These two returned any caller-supplied relation's contents with no
	 * privilege check at all, and CREATE FUNCTION grants EXECUTE to PUBLIC, so
	 * USAGE on the schema was the whole boundary. reconstruct_via_projection is
	 * the worse of the pair: it rebuilds NON-COVERED columns from the base by row
	 * number, so the projection was never the bound on what leaked -- one
	 * projection on any column exposed the whole row.
	 *
	 * ACL_SELECT rather than PgColumnarRequireTableOwner, and that is a
	 * correctness argument rather than a lenient one. Both functions read base
	 * columns, so SELECT on the base is exactly the privilege that governs
	 * reading them by any other route. Ownership would be a stricter bar that
	 * happens to exclude the attacker, which is a different and worse property:
	 * it would also refuse a reader who has been granted SELECT deliberately.
	 * Same pattern as columnar_parallel_export.c:471.
	 */
	{
		AclResult	ac = pg_class_aclcheck(relid, GetUserId(), ACL_SELECT);

		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	}

	/* RLS after the ACL check, matching core's ordering (#563). */
	PgColumnarRequireNoRowSecurity(relid);

	rel = table_open(relid, AccessShareLock);
	/* persist pending base + projection writes so this read sees them */
	PgColumnarFlushWriteStateForRelation(relid);
	storageId = PgColumnarStorageId(rel);

	projs = PgColumnarListProjections(storageId);
	foreach(lc, projs)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (strcmp(p->name, projname) == 0)
		{
			proj = p;
			break;
		}
	}
	if (proj == NULL || proj->projectionId == 0)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("projection \"%s\" does not exist on \"%s\"",
						projname, get_rel_name(relid)),
				 errhint("A declared projection is re-recorded automatically after a "
						 "rewrite (#887), so this is no longer the usual cause. It can "
						 "still read as absent when its declaration names a column the "
						 "table no longer has, which the rewrite reports as a WARNING, "
						 "or when the name given is the implicit base projection, which "
						 "is not readable by name. pgcolumnar.rebuild_projections() "
						 "re-records a declared projection once its declaration "
						 "resolves.")));

	ncols = proj->columnsLen;

	/* projection storage layout: [rownumber int8, projcol1..projcolK] */
	projTupdesc = CreateTemplateTupleDesc(ncols + 1);
	TupleDescInitEntry(projTupdesc, 1, "rownumber", INT8OID, -1, 0);
	for (i = 0; i < ncols; i++)
		TupleDescCopyEntry(projTupdesc, i + 2, RelationGetDescr(rel),
						   (AttrNumber) proj->columns[i]);

	outFns = palloc(sizeof(FmgrInfo) * ncols);
	for (i = 0; i < ncols; i++)
	{
		Form_pg_attribute att = TupleDescAttr(projTupdesc, i + 1);
		Oid			outOid;
		bool		isVarlena;

		getTypeOutputInfo(att->atttypid, &outOid, &isVarlena);
		fmgr_info(outOid, &outFns[i]);
	}

	/* one text column result, materialized into a tuplestore */
	retdesc = CreateTemplateTupleDesc(1);
	TupleDescInitEntry(retdesc, 1, "row", TEXTOID, -1, 0);

	oldContext = MemoryContextSwitchTo(rsinfo->econtext->ecxt_per_query_memory);
	tupstore = tuplestore_begin_heap(true, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = tupstore;
	rsinfo->setDesc = retdesc;
	MemoryContextSwitchTo(oldContext);

	snap = GetActiveSnapshot();
	rvals = palloc(sizeof(Datum) * (ncols + 1));
	rnulls = palloc(sizeof(bool) * (ncols + 1));
	readState = PgColumnarBeginReadWithStorage(rel, snap, proj->projStorageId,
											 projTupdesc, NULL, NULL, 0, NULL);

	while (PgColumnarReadNextRow(readState, rvals, rnulls, &projRowNum))
	{
		uint64		baseRow = (uint64) DatumGetInt64(rvals[0]);
		StringInfoData buf;
		Datum		result;
		bool		resnull = false;

		/*
		 * Only the base row's visibility is wanted here -- every value this loop
		 * emits comes from the projection's own storage. Reconstructing the whole
		 * base row to answer that decoded every column and threw all of it away
		 * (issue #157).
		 */
		if (!PgColumnarRowIsLive(rel, snap, baseRow))
			continue;

		initStringInfo(&buf);
		for (i = 0; i < ncols; i++)
		{
			if (i > 0)
				appendStringInfoChar(&buf, '|');
			if (rnulls[i + 1])
				appendStringInfoString(&buf, "\\N");
			else
				appendStringInfoString(&buf,
									   OutputFunctionCall(&outFns[i], rvals[i + 1]));
		}

		result = CStringGetTextDatum(buf.data);
		tuplestore_putvalues(tupstore, retdesc, &result, &resnull);
		pfree(buf.data);
	}

	PgColumnarEndRead(readState);
	table_close(rel, AccessShareLock);

	return (Datum) 0;
}

/*
 * pgcolumnar.reconstruct_via_projection(rel, name) -> setof text
 *		Read every live row through a projection and reconstruct the full base
 *		row (gap 26, phase 3): columns stored in the projection come from the
 *		projection's own storage, and any remaining table columns are fetched
 *		from the base by the projection's stored row number. This exercises the
 *		row-number join that the planner uses (phase 4) when a chosen projection
 *		does not cover every referenced column. All live table columns are
 *		rendered by their output functions and joined by '|'.
 */
PG_FUNCTION_INFO_V1(pgcolumnar_reconstruct_via_projection);
Datum
pgcolumnar_reconstruct_via_projection(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *projname;
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Relation	rel;
	TupleDesc	tableDesc;
	uint64		storageId;
	List	   *projs;
	ListCell   *lc;
	PgColumnarProjection *proj = NULL;
	TupleDesc	projTupdesc;
	TupleDesc	retdesc;
	Tuplestorestate *tupstore;
	MemoryContext oldContext;
	FmgrInfo   *outFns;			/* per table column */
	int		   *covered;		/* table col -> index into projection rvals, or -1 */
	int			ncols;
	int			tnatts;
	int			i;
	Snapshot	snap;
	PgColumnarReadState *readState;
	Datum	   *rvals;
	bool	   *rnulls;
	Datum	   *basevals;
	bool	   *basenulls;
	Bitmapset  *uncovered = NULL;
	uint64		projRowNum;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("rel and name must not be NULL")));
	relid = PG_GETARG_OID(0);
	projname = text_to_cstring(PG_GETARG_TEXT_PP(1));

	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		!(rsinfo->allowedModes & SFRM_Materialize))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in context that cannot accept a set")));

	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not a columnar table", get_rel_name(relid))));

	/*
	 * SELECT on the BASE relation, checked before a single row is read (#562).
	 *
	 * These two returned any caller-supplied relation's contents with no
	 * privilege check at all, and CREATE FUNCTION grants EXECUTE to PUBLIC, so
	 * USAGE on the schema was the whole boundary. reconstruct_via_projection is
	 * the worse of the pair: it rebuilds NON-COVERED columns from the base by row
	 * number, so the projection was never the bound on what leaked -- one
	 * projection on any column exposed the whole row.
	 *
	 * ACL_SELECT rather than PgColumnarRequireTableOwner, and that is a
	 * correctness argument rather than a lenient one. Both functions read base
	 * columns, so SELECT on the base is exactly the privilege that governs
	 * reading them by any other route. Ownership would be a stricter bar that
	 * happens to exclude the attacker, which is a different and worse property:
	 * it would also refuse a reader who has been granted SELECT deliberately.
	 * Same pattern as columnar_parallel_export.c:471.
	 */
	{
		AclResult	ac = pg_class_aclcheck(relid, GetUserId(), ACL_SELECT);

		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	}

	/* RLS after the ACL check, matching core's ordering (#563). */
	PgColumnarRequireNoRowSecurity(relid);

	rel = table_open(relid, AccessShareLock);
	PgColumnarFlushWriteStateForRelation(relid);
	tableDesc = RelationGetDescr(rel);
	tnatts = tableDesc->natts;
	storageId = PgColumnarStorageId(rel);

	projs = PgColumnarListProjections(storageId);
	foreach(lc, projs)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (strcmp(p->name, projname) == 0)
		{
			proj = p;
			break;
		}
	}
	if (proj == NULL || proj->projectionId == 0)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("projection \"%s\" does not exist on \"%s\"",
						projname, get_rel_name(relid)),
				 errhint("A declared projection is re-recorded automatically after a "
						 "rewrite (#887), so this is no longer the usual cause. It can "
						 "still read as absent when its declaration names a column the "
						 "table no longer has, which the rewrite reports as a WARNING, "
						 "or when the name given is the implicit base projection, which "
						 "is not readable by name. pgcolumnar.rebuild_projections() "
						 "re-records a declared projection once its declaration "
						 "resolves.")));

	ncols = proj->columnsLen;

	/* projection storage layout: [rownumber int8, projcol1..projcolK] */
	projTupdesc = CreateTemplateTupleDesc(ncols + 1);
	TupleDescInitEntry(projTupdesc, 1, "rownumber", INT8OID, -1, 0);
	for (i = 0; i < ncols; i++)
		TupleDescCopyEntry(projTupdesc, i + 2, tableDesc,
						   (AttrNumber) proj->columns[i]);

	/* map each table column to its position in the projection row, or -1 */
	covered = palloc(sizeof(int) * tnatts);
	for (i = 0; i < tnatts; i++)
	{
		int			p;

		covered[i] = -1;
		for (p = 0; p < ncols; p++)
			if ((int) proj->columns[p] == i + 1)
			{
				covered[i] = p + 1;		/* +1: rvals[0] is the row number */
				break;
			}
	}

	outFns = palloc(sizeof(FmgrInfo) * tnatts);
	for (i = 0; i < tnatts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tableDesc, i);
		Oid			outOid;
		bool		isVarlena;

		if (att->attisdropped)
			continue;
		getTypeOutputInfo(att->atttypid, &outOid, &isVarlena);
		fmgr_info(outOid, &outFns[i]);
	}

	retdesc = CreateTemplateTupleDesc(1);
	TupleDescInitEntry(retdesc, 1, "row", TEXTOID, -1, 0);

	oldContext = MemoryContextSwitchTo(rsinfo->econtext->ecxt_per_query_memory);
	tupstore = tuplestore_begin_heap(true, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = tupstore;
	rsinfo->setDesc = retdesc;
	MemoryContextSwitchTo(oldContext);

	snap = GetActiveSnapshot();
	rvals = palloc(sizeof(Datum) * (ncols + 1));
	rnulls = palloc(sizeof(bool) * (ncols + 1));
	basevals = palloc(sizeof(Datum) * tnatts);
	basenulls = palloc(sizeof(bool) * tnatts);

	/*
	 * The base row is only read for the columns the projection does not carry,
	 * so those are the only ones worth decoding (issue #157).
	 */
	for (i = 0; i < tnatts; i++)
	{
		if (TupleDescAttr(tableDesc, i)->attisdropped)
			continue;
		if (covered[i] < 0)
			uncovered = bms_add_member(uncovered, i);
	}

	readState = PgColumnarBeginReadWithStorage(rel, snap, proj->projStorageId,
											 projTupdesc, NULL, NULL, 0, NULL);

	while (PgColumnarReadNextRow(readState, rvals, rnulls, &projRowNum))
	{
		uint64		baseRow = (uint64) DatumGetInt64(rvals[0]);
		StringInfoData buf;
		Datum		result;
		bool		resnull = false;
		bool		first = true;

		/* fetch the base row: liveness, and only the columns not covered */
		if (!PgColumnarReadRowByNumberCols(rel, snap, baseRow, basevals,
										 basenulls, uncovered))
			continue;

		initStringInfo(&buf);
		for (i = 0; i < tnatts; i++)
		{
			Form_pg_attribute att = TupleDescAttr(tableDesc, i);
			Datum		v;
			bool		isnull;

			if (att->attisdropped)
				continue;
			if (!first)
				appendStringInfoChar(&buf, '|');
			first = false;

			if (covered[i] >= 0)
			{
				v = rvals[covered[i]];
				isnull = rnulls[covered[i]];
			}
			else
			{
				v = basevals[i];
				isnull = basenulls[i];
			}

			if (isnull)
				appendStringInfoString(&buf, "\\N");
			else
				appendStringInfoString(&buf, OutputFunctionCall(&outFns[i], v));
		}

		result = CStringGetTextDatum(buf.data);
		tuplestore_putvalues(tupstore, retdesc, &result, &resnull);
		pfree(buf.data);
	}

	PgColumnarEndRead(readState);
	table_close(rel, AccessShareLock);

	return (Datum) 0;
}

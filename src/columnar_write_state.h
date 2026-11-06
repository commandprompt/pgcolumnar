/*-------------------------------------------------------------------------
 *
 * columnar_write_state.h
 *		The write side: what columnar_write_state.c offers the modules that stage and flush rows.
 *
 * Split out of columnar.h (#496). Every declaration here had exactly ONE
 * consumer outside its defining file, so it was a private arrangement between
 * two files that the other twenty were forced to recompile for.
 *
 * The shared vocabulary these signatures take stays in columnar.h, which this
 * includes.
 *
 * Written fresh for pgColumnar.
 *
 *-------------------------------------------------------------------------
 */
#ifndef PGCOLUMNAR_WRITE_STATE_H
#define PGCOLUMNAR_WRITE_STATE_H

#include "columnar.h"

extern void PgColumnarEnsureStorageRow(Relation rel);	/* pre-create storage row (#300 parallel_copy) */

extern int PgColumnarWriteStateStripeCount(PgColumnarWriteState *ws);

extern uint64 *PgColumnarWriteStateStripeIds(PgColumnarWriteState *ws, int *n);

extern uint64 *PgColumnarWriteStateProjStripeIds(PgColumnarWriteState *ws, int *n);

extern void PgColumnarBackfillProjection(Relation rel,
									   const PgColumnarProjection *proj);

extern bool PgColumnarBufferedRowByNumber(Relation rel, uint64 rowNumber,
										Datum *values, bool *nulls);

extern void PgColumnarForgetWriteStateForRelation(Oid relid);

extern void PgColumnarFlushAllPendingWrites(void);
extern void PgColumnarFlushPendingWritesOfSubXact(SubTransactionId subid);

extern void PgColumnarDiscardAllPendingWrites(void);

extern void PgColumnarWriteStateDiscardSubXact(SubTransactionId subid);

extern void PgColumnarWriteStateAfterAppend(PgColumnarWriteState *writeState);
extern void PgColumnarWriteStatePromoteSubXact(SubTransactionId subid,
											 SubTransactionId parent);

#endif							/* PGCOLUMNAR_WRITE_STATE_H */

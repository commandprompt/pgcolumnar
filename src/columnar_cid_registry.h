/*-------------------------------------------------------------------------
 *
 * columnar_cid_registry.h
 *		Which command of this transaction wrote each row and delete mark this
 *		backend flushed (#2247). See columnar_cid_registry.c.
 *
 * Written fresh for pgColumnar.
 *
 *-------------------------------------------------------------------------
 */
#ifndef PGCOLUMNAR_CID_REGISTRY_H
#define PGCOLUMNAR_CID_REGISTRY_H

#include "columnar.h"

extern int	pgcolumnar_debug_registry_cid_skew;

extern bool PgColumnarCidVisible(Snapshot snapshot, CommandId cid);
extern void PgColumnarCidRegisterRows(uint64 storageId, uint64 firstRow,
									  uint64 nrows, CommandId cid);
extern void PgColumnarCidRegisterMarks(uint64 storageId, uint64 startRow,
									   const char *mask, uint32 maskLen,
									   CommandId cid);
extern bool PgColumnarCidRowHidden(uint64 storageId, uint64 rowNumber,
								   Snapshot snapshot);
extern bool PgColumnarCidMarkHidden(uint64 storageId, uint64 rowNumber,
									Snapshot snapshot);
extern CommandId PgColumnarCidLeaderMax(bool *any);
extern void PgColumnarCidCheckWorker(bool leaderAny, CommandId leaderMax,
									 Snapshot snapshot);
extern void PgColumnarCidRegistryDiscardSubXact(SubTransactionId subid);
extern void PgColumnarCidRegistryPromoteSubXact(SubTransactionId subid,
												SubTransactionId parent);
extern void PgColumnarCidRegistryReset(void);

/*
 * The shared-memory area a parallel scan carries for the worker check. A
 * custom scan puts it after its counter, at PGC_PARALLEL_CID_OFFSET.
 */
#define PGC_PARALLEL_CID_OFFSET MAXALIGN(sizeof(pg_atomic_uint32))
extern Size PgColumnarParallelCidSize(void);
extern void PgColumnarParallelCidStore(void *at);
extern void PgColumnarParallelCidCheck(const void *at, Snapshot snapshot);

#endif							/* PGCOLUMNAR_CID_REGISTRY_H */

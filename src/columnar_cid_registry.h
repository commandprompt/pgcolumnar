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

extern bool PgColumnarCidVisible(Snapshot snapshot, CommandId cid);
extern void PgColumnarCidRegisterRows(uint64 storageId, uint64 firstRow,
									  uint64 nrows, CommandId cid);
extern void PgColumnarCidRegisterMarkRows(uint64 storageId, const uint64 *rows,
										  uint32 n, CommandId cid);
extern bool PgColumnarCidRowHidden(uint64 storageId, uint64 rowNumber,
								   Snapshot snapshot);
extern bool PgColumnarCidMarkHidden(uint64 storageId, uint64 rowNumber,
									Snapshot snapshot);
extern void PgColumnarCidRegistryDiscardSubXact(SubTransactionId subid);
extern void PgColumnarCidRegistryPromoteSubXact(SubTransactionId subid,
												SubTransactionId parent);
extern void PgColumnarCidRegistryReset(void);

/* the copy for parallel workers */
extern void PgColumnarCidRegistryShmemRegister(void);
struct QueryDesc;
extern void PgColumnarCidRegistryPublish(struct QueryDesc *query, Snapshot snapshot);
extern void PgColumnarCidRegistryUnpublish(struct QueryDesc *query);

#endif							/* PGCOLUMNAR_CID_REGISTRY_H */

/*-------------------------------------------------------------------------
 *
 * columnar_metadata.h
 *		The catalog side of pgColumnar: what columnar_metadata.c offers the
 *		modules that read and write our own catalog tables.
 *
 * Split out of columnar.h (#496). Every declaration here had exactly ONE
 * consumer outside its defining file, so it was a private arrangement between
 * two files that the other twenty were forced to recompile for, and that anyone
 * reading columnar.h had to scan past to reach the interface that is genuinely
 * shared.
 *
 * The shared vocabulary -- the Native*Metadata structs these signatures take,
 * the GUCs, the format constants -- stays in columnar.h, which this includes.
 *
 * Written fresh for pgColumnar.
 *
 *-------------------------------------------------------------------------
 */
#ifndef PGCOLUMNAR_METADATA_H
#define PGCOLUMNAR_METADATA_H

#include "columnar.h"

extern void PgColumnarRetireGroup(uint64 storageId, uint64 groupNumber);

extern void PgColumnarLockChunkGroup(uint64 storageId, uint64 groupNumber);

extern bool PgColumnarAllocateFreeSpace(uint64 storageId, uint64 dataLength,
									  TransactionId oldestXmin, uint64 *fileOffset);

extern bool PgColumnarTrailingFreeSpaceSafe(uint64 storageId, uint64 liveEnd,
										  TransactionId oldestXmin);

extern void PgColumnarDeleteFreeSpaceAtOrAbove(uint64 storageId, uint64 liveEnd);

extern void PgColumnarReconcileFreeList(Relation dataRel);

extern List *PgColumnarComputeAllVisibleGroups(uint64 storageId,
											 TransactionId oldestXmin);

extern void PgColumnarInsertNativeStorageRow(const NativeStorageMetadata *s);

extern void PgColumnarSetSortedExtent(uint64 storageId, int64 firstGroup,
									  int64 lastGroup, List *sortByNames,
									  const char *sortedKind);
/* #415: the current run's clustering key (a list of column-name strings, NIL
 * if unknown) and kind ('zorder'/'lexicographic'/NULL), for recluster's
 * self-gate and sort_status. */
extern void PgColumnarRenameDeclaredSortByColumn(Oid relid, const char *oldName,
												 const char *newName);
extern void PgColumnarRenameSortKeyColumn(uint64 storageId, const char *oldName,
										  const char *newName);
extern void PgColumnarGetSortedInfo(uint64 storageId, int64 *firstGroup,
									int64 *lastGroup, List **sortByNames,
									char **sortedKind);

extern void PgColumnarInsertRowGroupRow(const NativeRowGroupMetadata *rg);

extern void PgColumnarInsertColumnChunkRow(const NativeColumnChunkMetadata *cc);

extern void PgColumnarInsertZoneMapRow(const NativeZoneMapMetadata *z);

extern void PgColumnarInsertBloomRow(const NativeBloomMetadata *b);

/* #445: bracket a stripe flush's metadata inserts to share one open per table. */
extern void PgColumnarBeginMetadataFlush(void);
extern void PgColumnarEndMetadataFlush(void);
extern void PgColumnarResetMetadataFlush(void);

extern List *PgColumnarReadColumnChunkList(uint64 storageId, uint64 groupNumber,
										 Snapshot snapshot);

extern List *PgColumnarReadZoneMapVectors(uint64 storageId, uint64 groupNumber,
										Snapshot snapshot);
extern List *PgColumnarReadZoneMapVectorsForColumn(uint64 storageId,
										uint64 groupNumber, int columnIndex,
										Snapshot snapshot);

extern NativeBloomMetadata *PgColumnarReadBloomForColumn(uint64 storageId,
													   uint64 groupNumber,
													   int columnIndex,
													   Snapshot snapshot);

extern void PgColumnarDeleteOptions(Oid relid);

extern List *PgColumnarReadSortBy(Oid relid);

extern void PgColumnarRecordProjectionDeclaration(Oid relid, const char *name,
												ArrayType *columns,
												ArrayType *sortKey);

extern void PgColumnarDeleteProjectionDeclaration(Oid relid, const char *name);

extern void PgColumnarDeleteProjectionDeclarationsForRel(Oid relid);

extern bool PgColumnarStorageHasDeleteVector(uint64 storageId, Snapshot snapshot);
extern uint64 PgColumnarStorageDeletedCount(uint64 storageId, Snapshot snapshot);

#endif							/* PGCOLUMNAR_METADATA_H */

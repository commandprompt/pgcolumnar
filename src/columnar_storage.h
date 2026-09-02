/*-------------------------------------------------------------------------
 *
 * columnar_storage.h
 *		The physical layer: what columnar_storage.c offers for metapages, offsets and logical data.
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
#ifndef PGCOLUMNAR_STORAGE_H
#define PGCOLUMNAR_STORAGE_H

#include "columnar.h"

/*
 * Block layout of an initialised columnar main fork (spec 3). Block 0 is the
 * metapage, block 1 is reserved and left empty; PgColumnarWriteNewMetapage
 * writes both. COLUMNAR_INITIALIZED_NBLOCKS is what smgrnblocks() reports for a
 * fork that has been through it, which is how a caller tells a rewrite of an
 * existing columnar relation from a CREATE TABLE with no fork yet. Derived
 * rather than written as 2, so that adding a block moves every reader at once.
 */
#define COLUMNAR_METAPAGE_BLOCKNO 0
#define COLUMNAR_EMPTY_BLOCKNO 1
#define COLUMNAR_INITIALIZED_NBLOCKS (COLUMNAR_EMPTY_BLOCKNO + 1)

extern void PgColumnarWriteNewMetapage(const RelFileLocator *newrlocator,
									 struct SMgrRelationData *srel,
									 char persistence, uint64 storageId);

extern void PgColumnarReserveRowNumbers(Relation rel, uint64 rowCount,
									  uint64 *stripeId, uint64 *firstRowNumber);

extern void PgColumnarReserveOffset(Relation rel, uint64 dataLength,
								  uint64 *fileOffset);

extern void PgColumnarAdvanceReservedOffset(Relation rel, uint64 addBytes);

extern void PgColumnarDebugSetMetapageVersion(Relation rel, uint32 versionMajor,
											uint32 versionMinor);

extern void PgColumnarSetReservedOffset(Relation rel, uint64 newOffset);

extern void PgColumnarTruncateMainFork(Relation rel, BlockNumber newnblocks);

extern void PgColumnarWriteLogicalData(Relation rel, uint64 logicalOffset,
									 char *data, uint64 length);

extern void PgColumnarReadLogicalData(Relation rel, uint64 logicalOffset,
									char *dest, uint64 length);

extern void PgColumnarResetMetapage(Relation rel);

#endif							/* PGCOLUMNAR_STORAGE_H */

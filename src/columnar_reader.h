/*-------------------------------------------------------------------------
 *
 * columnar_reader.h
 *		The read side of pgColumnar: what columnar_reader.c offers the scan and fold paths.
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
#ifndef PGCOLUMNAR_READER_H
#define PGCOLUMNAR_READER_H

#include "columnar.h"

extern void PgColumnarDiscardFetchCache(void);
extern void PgColumnarGroupMemoReset(bool contextsLive);

extern bool PgColumnarReadNextRowFiltered(PgColumnarReadState *readState,
										Datum *values, bool *nulls,
										uint64 *rowNumber,
										const bool *qualCols,
										PgColumnarRowFilter filter,
										PgColumnarRowFilter filterNoCount,
										void *filterArg);

extern uint64 PgColumnarRowsFilteredEarly(PgColumnarReadState *readState);

extern bool PgColumnarReadFoldNextGroup(PgColumnarReadState *readState);

extern void PgColumnarReadFoldGroupInfo(PgColumnarReadState *readState, uint64 *nrows,
									  const char **deleteMask, uint32 *deleteMaskLen,
									  const bool **skipVec, bool *decodeSkipped,
									  const uint32 **vecStart,
									  int *vectorCount);

extern bool PgColumnarReadFoldColumn(PgColumnarReadState *readState, int attidx,
								   const char **validity, const char **packed,
								   int16 *attlen, const uint32 **vecRawLen);

extern void PgColumnarReadSetEmitDeleted(PgColumnarReadState *readState, bool emit);
extern void PgColumnarReadSetDeadHorizon(PgColumnarReadState *readState, Snapshot horizon);
extern bool PgColumnarReadLastRowDeleted(PgColumnarReadState *readState);
extern void PgColumnarReadSetProjection(PgColumnarReadState *readState,
										Bitmapset *projectedColumns);

extern int	PgColumnarReadProjectedCount(PgColumnarReadState *readState);

extern PgColumnarLivenessCache *PgColumnarBuildLivenessCache(Relation rel,
														 Snapshot snapshot);

extern bool PgColumnarLivenessCacheIsLive(PgColumnarLivenessCache *cache,
										uint64 rowNumber);

extern void PgColumnarFreeLivenessCache(PgColumnarLivenessCache *cache);

extern double PgColumnarEstimatePruneSurvival(uint64 storageId, TupleDesc tupdesc,
											List *qual, Index scanrelid,
											uint64 ngroups, int sampleTarget);

#endif							/* PGCOLUMNAR_READER_H */

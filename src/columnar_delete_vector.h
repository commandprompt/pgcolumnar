/*-------------------------------------------------------------------------
 *
 * columnar_delete_vector.h
 *		Row deletion: what columnar_delete_vector.c offers the reader and the table AM.
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
#ifndef PGCOLUMNAR_DELETE_VECTOR_H
#define PGCOLUMNAR_DELETE_VECTOR_H

#include "columnar.h"

extern void PgColumnarMarkRowDeleted(Relation rel, uint64 rowNumber);

extern bool PgColumnarDeleteVectorBufferedDeleted(Relation rel, uint64 rowNumber);

extern void PgColumnarFlushAllDeleteVectors(void);
extern void PgColumnarFlushDeleteVectorsOfSubXact(SubTransactionId subid);

extern void PgColumnarDiscardAllDeleteVectors(void);

extern void PgColumnarDeleteVectorDiscardSubXact(SubTransactionId subid);

extern void PgColumnarDeleteVectorPromoteSubXact(SubTransactionId subid,
										  SubTransactionId parent);

#endif							/* PGCOLUMNAR_DELETE_VECTOR_H */

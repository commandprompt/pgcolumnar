/*-------------------------------------------------------------------------
 *
 * pgcolumnar_storage.c
 *		Physical storage layer for pgColumnar: metapage, the logical-to-
 *		physical byte mapping, and the append-only reservation model.
 *
 * Implements spec sections 2, 2.1, 2.2, 3, and 6. All storage lives in the
 * relation's main fork using standard PostgreSQL pages, so the buffer
 * manager, WAL, and checksums apply.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_storage.h"
#include "fmgr.h"
#include "columnar_compat.h"
#include "access/rmgr.h"
#include "access/xlog.h"
#include "access/xloginsert.h"
#include "access/xlogrecord.h"
#include "catalog/storage.h"
#include "catalog/storage_xlog.h"
#include "common/int.h"
#include "miscadmin.h"
#include "storage/bufmgr.h"
#include "storage/lmgr.h"
#include "storage/proc.h"
#include "storage/procarray.h"
#include "storage/smgr.h"
#include "utils/rel.h"

#if PG_VERSION_NUM >= 170000
#include "storage/read_stream.h"
#endif

/*
 * COLUMNAR_METAPAGE_BLOCKNO, COLUMNAR_EMPTY_BLOCKNO and the derived
 * COLUMNAR_INITIALIZED_NBLOCKS are in columnar_storage.h: the metapage struct
 * lives right after the page header on block 0, and columnar_tableam.c needs
 * the block count to recognise an already-initialised fork.
 */
#define PgColumnarMetapagePointer(page) ((PgColumnarMetapage *) PageGetContents(page))

/*
 * Stream/prefetch the block reads in PgColumnarReadLogicalData via the read
 * stream API (PostgreSQL 17+), which lets the PostgreSQL 18 asynchronous I/O
 * subsystem read ahead. On by default; off falls back to synchronous ReadBuffer.
 * On PostgreSQL 16 and earlier the streaming path is compiled out and this GUC
 * has no effect.
 */
bool		pgcolumnar_enable_read_stream = true;

#if PG_VERSION_NUM >= 170000
/* contiguous ascending block range for the read stream callback */
typedef struct PgColumnarBlockRange
{
	BlockNumber next;
	BlockNumber last;
}			PgColumnarBlockRange;

static BlockNumber
pgcolumnar_read_stream_next(ReadStream *stream, void *private_data,
						  void *per_buffer_data)
{
	PgColumnarBlockRange *range = (PgColumnarBlockRange *) private_data;

	if (range->next > range->last)
		return InvalidBlockNumber;
	return range->next++;
}
#endif

/*
 * PgColumnarWriteNewMetapage
 *		Initialize a freshly created relation's storage: block 0 holds the
 *		metapage with the initial reserved values from spec 3, block 1 is
 *		reserved and left empty. Written with a WAL full-page image and an
 *		immediate sync because the new storage does not pass through shared
 *		buffers here.
 */
void
PgColumnarWriteNewMetapage(const RelFileLocator *newrlocator,
						 SMgrRelation srel, char persistence,
						 uint64 storageId)
{
	Page		page = PgColumnarAllocPage();
	PgColumnarMetapage *meta;
	bool		needsWAL = (persistence == RELPERSISTENCE_PERMANENT);

	/* block 0: metapage */
	PageInit(page, BLCKSZ, 0);
	meta = PgColumnarMetapagePointer(page);
	meta->versionMajor = COLUMNAR_VERSION_MAJOR;
	meta->versionMinor = COLUMNAR_VERSION_MINOR;
	meta->storageId = storageId;
	meta->reservedStripeId = 1;
	meta->reservedRowNumber = COLUMNAR_FIRST_ROW_NUMBER;
	meta->reservedOffset = COLUMNAR_FIRST_LOGICAL_OFFSET;
	meta->unloggedReset = false;
	((PageHeader) page)->pd_lower =
		((char *) meta - (char *) page) + sizeof(PgColumnarMetapage);

	if (needsWAL)
		log_newpage(&COLUMNAR_SMGR_LOCATOR(srel), MAIN_FORKNUM,
					COLUMNAR_METAPAGE_BLOCKNO, page, true);
	PageSetChecksumInplace(page, COLUMNAR_METAPAGE_BLOCKNO);
	smgrextend(srel, MAIN_FORKNUM, COLUMNAR_METAPAGE_BLOCKNO, page, true);

	/* block 1: reserved, empty */
	PageInit(page, BLCKSZ, 0);
	if (needsWAL)
		log_newpage(&COLUMNAR_SMGR_LOCATOR(srel), MAIN_FORKNUM,
					COLUMNAR_EMPTY_BLOCKNO, page, true);
	PageSetChecksumInplace(page, COLUMNAR_EMPTY_BLOCKNO);
	smgrextend(srel, MAIN_FORKNUM, COLUMNAR_EMPTY_BLOCKNO, page, true);

	smgrimmedsync(srel, MAIN_FORKNUM);

	pfree(page);
}

/*
 * PgColumnarReadMetapage
 *		Read the metapage of an existing relation into *meta.
 */
void
PgColumnarReadMetapage(Relation rel, PgColumnarMetapage *meta)
{
	Buffer		buffer;
	Page		page;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_SHARE);
	page = BufferGetPage(buffer);
	memcpy(meta, PgColumnarMetapagePointer(page), sizeof(PgColumnarMetapage));
	UnlockReleaseBuffer(buffer);

	if (meta->versionMajor != COLUMNAR_VERSION_MAJOR)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("unsupported columnar format version %u.%u",
						meta->versionMajor, meta->versionMinor)));
}

uint64
PgColumnarStorageId(Relation rel)
{
	PgColumnarMetapage meta;

	PgColumnarReadMetapage(rel, &meta);
	return meta.storageId;
}

/*
 * PgColumnarReserveRowNumbers
 *		Reserve a stripe id and a contiguous run of "rowCount" row numbers by
 *		advancing the metapage's reservedStripeId and reservedRowNumber marks
 *		(spec 2.2, 6). Returns the reserved stripe id and the first row number
 *		of the run.
 *
 *		This is done when a writer begins buffering a stripe, so that every
 *		row gets its final, stable row number (and therefore its item pointer)
 *		at insert time. That is what lets an index carry a correct TID for a
 *		freshly inserted row (spec 6, 9). The byte offset is reserved
 *		separately, at flush, once the stripe's size is known
 *		(PgColumnarReserveOffset). Row numbers not used by a short stripe are
 *		simply left as a gap; row numbers need only be unique and stable.
 *
 *		Serialized by the exclusive lock on the metapage buffer; no relation
 *		extension lock is needed here because no data pages are extended.
 *
 *		Refuses a run that would reach COLUMNAR_ROW_NUMBER_LIMIT (#2073): a row
 *		number past it would wrap its item pointer's block number, and an index
 *		would then point at the wrong row. The refusal comes before the
 *		metapage changes, so a refused writer uses up nothing.
 */
void
PgColumnarReserveRowNumbers(Relation rel, uint64 rowCount,
						  uint64 *stripeId, uint64 *firstRowNumber)
{
	Buffer		buffer;
	Page		page;
	PgColumnarMetapage *meta;
	uint64		end;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(buffer);
	meta = PgColumnarMetapagePointer(page);

	/*
	 * One comparison of where the run would end. It also refuses a table
	 * whose metapage an older version already advanced past the limit, since
	 * that run ends past the limit too. There is no separate clause for that
	 * case, so no test has to reach it. The addition cannot overflow for any
	 * metapage a writer produced, but a corrupt one could hold any value.
	 */
	if (pg_add_u64_overflow(meta->reservedRowNumber, rowCount, &end) ||
		end > COLUMNAR_ROW_NUMBER_LIMIT)
	{
		uint64		next = meta->reservedRowNumber;

		UnlockReleaseBuffer(buffer);
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("columnar table \"%s\" has run out of row numbers",
						RelationGetRelationName(rel)),
				 errdetail("The writer needs row numbers " UINT64_FORMAT " to "
						   UINT64_FORMAT ", but the largest row number an item "
						   "pointer can address is " UINT64_FORMAT ".",
						   next, next + rowCount - 1,
						   COLUMNAR_ROW_NUMBER_LIMIT - 1),
				 errhint("Each transaction that writes reserves stripe_row_limit "
						 "row numbers. Run pgcolumnar.vacuum on the table to "
						 "renumber its rows.")));
	}

	*stripeId = meta->reservedStripeId;
	*firstRowNumber = meta->reservedRowNumber;

	meta->reservedStripeId += 1;
	meta->reservedRowNumber += rowCount;

	START_CRIT_SECTION();
	MarkBufferDirty(buffer);
	if (RelationNeedsWAL(rel))
	{
		XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

		PageSetLSN(page, recptr);
	}
	END_CRIT_SECTION();

	UnlockReleaseBuffer(buffer);
}

/*
 * PgColumnarReserveOffset
 *		Reserve a page-aligned logical byte range of "dataLength" bytes for a
 *		stripe's data and return its file offset (spec 2.1, 2.2). New
 *		reservations start on a fresh page.
 *
 *		The caller must already hold the relation extension lock and must
 *		write the reserved data immediately (via PgColumnarWriteLogicalData)
 *		before releasing it, so that reservation is serialized and the P_NEW
 *		extends match the reserved blocks.
 */
void
PgColumnarReserveOffset(Relation rel, uint64 dataLength, uint64 *fileOffset)
{
	Buffer		buffer;
	Page		page;
	PgColumnarMetapage *meta;
	uint64		alignedOffset;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(buffer);
	meta = PgColumnarMetapagePointer(page);

	/* align the start of this reservation up to a page boundary */
	alignedOffset = ((meta->reservedOffset + COLUMNAR_BYTES_PER_PAGE - 1) /
					 COLUMNAR_BYTES_PER_PAGE) * COLUMNAR_BYTES_PER_PAGE;

	*fileOffset = alignedOffset;
	meta->reservedOffset = alignedOffset + dataLength;

	START_CRIT_SECTION();
	MarkBufferDirty(buffer);
	if (RelationNeedsWAL(rel))
	{
		XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

		PageSetLSN(page, recptr);
	}
	END_CRIT_SECTION();

	UnlockReleaseBuffer(buffer);
}

/*
 * PgColumnarAdvanceReservedOffset
 *		Increase the metapage highwater by addBytes without writing any data,
 *		leaving a gap between the physical EOF and the new highwater. This is a
 *		test hook for the gap-tolerant write path (it deliberately produces the
 *		"highwater ahead of EOF" state that an aborted end-truncation leaves).
 *		Increase-only, so it can never make the highwater overlap live data.
 */
void
PgColumnarAdvanceReservedOffset(Relation rel, uint64 addBytes)
{
	Buffer		buffer;
	Page		page;
	PgColumnarMetapage *meta;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(buffer);
	meta = PgColumnarMetapagePointer(page);

	meta->reservedOffset += addBytes;

	START_CRIT_SECTION();
	MarkBufferDirty(buffer);
	if (RelationNeedsWAL(rel))
	{
		XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

		PageSetLSN(page, recptr);
	}
	END_CRIT_SECTION();

	UnlockReleaseBuffer(buffer);
}

/*
 * PgColumnarDebugSetMetapageVersion
 *		Test-only: overwrite the metapage's stored physical format version. Reads
 *		go through PgColumnarReadMetapage, which rejects a version it does not
 *		understand; this lets a test plant a bad version and confirm that guard
 *		fires cleanly. Reachable only via the SQL binding the format suite
 *		creates, never from the shipped catalog.
 */
void
PgColumnarDebugSetMetapageVersion(Relation rel, uint32 versionMajor, uint32 versionMinor)
{
	Buffer		buffer;
	Page		page;
	PgColumnarMetapage *meta;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(buffer);
	meta = PgColumnarMetapagePointer(page);

	meta->versionMajor = versionMajor;
	meta->versionMinor = versionMinor;

	START_CRIT_SECTION();
	MarkBufferDirty(buffer);
	if (RelationNeedsWAL(rel))
	{
		XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

		PageSetLSN(page, recptr);
	}
	END_CRIT_SECTION();

	UnlockReleaseBuffer(buffer);
}

/*
 * PgColumnarSetReservedOffset
 *		Lower the metapage highwater to newOffset (physical end-truncation). The
 *		caller has computed newOffset as the end of all live data and holds an
 *		exclusive lock on the relation, so no reservation can race. Future
 *		reservations then reuse the reclaimed low space instead of appending past
 *		the (now truncated) EOF. It is an error to raise the highwater here.
 */
void
PgColumnarSetReservedOffset(Relation rel, uint64 newOffset)
{
	Buffer		buffer;
	Page		page;
	PgColumnarMetapage *meta;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(buffer);
	meta = PgColumnarMetapagePointer(page);

	Assert(newOffset <= meta->reservedOffset);
	meta->reservedOffset = newOffset;

	START_CRIT_SECTION();
	MarkBufferDirty(buffer);
	if (RelationNeedsWAL(rel))
	{
		XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

		PageSetLSN(page, recptr);
	}
	END_CRIT_SECTION();

	UnlockReleaseBuffer(buffer);
}

/*
 * PgColumnarTruncateMainFork
 *		Physically truncate the relation's MAIN fork to newnblocks, returning the
 *		trailing blocks to the OS (physical end-truncation). Scoped to the main
 *		fork only: the visibility-map fork is indexed by row-number-derived blocks,
 *		independent of data blocks, so it must NOT be truncated here. WAL-logs an
 *		xl_smgr_truncate for the main fork (flag SMGR_TRUNCATE_HEAP) and flushes it
 *		before the physical op so recovery and standbys truncate identically;
 *		smgrtruncate drops the removed blocks' buffers itself. The caller must hold
 *		AccessExclusiveLock so no backend holds or dirties a buffer in the removed
 *		range.
 */
void
PgColumnarTruncateMainFork(Relation rel, BlockNumber newnblocks)
{
	SMgrRelation srel = RelationGetSmgr(rel);
	ForkNumber	fork = MAIN_FORKNUM;
	BlockNumber oldnblocks = smgrnblocks(srel, MAIN_FORKNUM);
	bool		needs_wal = RelationNeedsWAL(rel);

	if (newnblocks >= oldnblocks)
		return;

	/* pending-sync bookkeeping for wal_level=minimal, as RelationTruncate does */
	RelationPreTruncate(rel);

	/*
	 * Mirror RelationTruncate's crash-safety envelope: hold off checkpoint
	 * completion across the WAL record and the physical truncate, inside a
	 * critical section, so a checkpoint cannot complete in the window (which,
	 * combined with a crash, could otherwise leave block state that breaks
	 * replay). The truncation is WAL-flushed before it is performed so recovery
	 * and standbys truncate identically.
	 */
	if (needs_wal)
	{
		Assert((MyProc->delayChkptFlags & DELAY_CHKPT_COMPLETE) == 0);
		MyProc->delayChkptFlags |= DELAY_CHKPT_COMPLETE;
	}

	START_CRIT_SECTION();

	if (needs_wal)
	{
		xl_smgr_truncate xlrec;
		XLogRecPtr	lsn;

		xlrec.blkno = newnblocks;
		COLUMNAR_XLREC_SET_LOCATOR(xlrec, srel);
		xlrec.flags = SMGR_TRUNCATE_HEAP;	/* main fork only; VM fork untouched */

		XLogBeginInsert();
		XLogRegisterData((char *) &xlrec, sizeof(xlrec));
		lsn = XLogInsert(RM_SMGR_ID, XLOG_SMGR_TRUNCATE | XLR_SPECIAL_REL_UPDATE);
		XLogFlush(lsn);
	}

	COLUMNAR_SMGRTRUNCATE(srel, &fork, 1, &oldnblocks, &newnblocks);

	if (needs_wal)
		MyProc->delayChkptFlags &= ~DELAY_CHKPT_COMPLETE;

	END_CRIT_SECTION();
}

/*
 * PgColumnarWriteLogicalData
 *		Write a contiguous logical byte range starting at a page-aligned
 *		logical offset, splitting it across the physical pages it maps to
 *		(spec 2.1). Blocks past the current end of the relation are extended;
 *		blocks below it are reused freed ranges (Phase F reclaim) and written in
 *		place. Reuse is only ever handed out for ranges no snapshot can still read
 *		(oldest-xmin gated), so overwriting the old contents is safe.
 *
 *		The target block may be strictly BEYOND the current EOF (a gap) when the
 *		metapage highwater is ahead of the physical file -- this is the state left
 *		by an aborted or crash-interrupted end-truncation (the file was shortened
 *		but the highwater was not, or the shortening's catalog changes rolled back).
 *		Such a gap is filled with empty pages up to the target block, so the state
 *		self-heals: the next write simply re-extends the fork. The caller holds the
 *		relation extension lock, so the P_NEW extensions here are serialized.
 */
void
PgColumnarWriteLogicalData(Relation rel, uint64 logicalOffset,
						 char *data, uint64 length)
{
	uint64		L = logicalOffset;
	uint64		remaining = length;
	char	   *src = data;
	BlockNumber nblocks = RelationGetNumberOfBlocksInFork(rel, MAIN_FORKNUM);

	Assert(logicalOffset % COLUMNAR_BYTES_PER_PAGE == 0);

	while (remaining > 0)
	{
		BlockNumber blockno = (BlockNumber) (L / COLUMNAR_BYTES_PER_PAGE);
		uint32		pageOffset = (uint32) (L % COLUMNAR_BYTES_PER_PAGE);
		uint32		n = (uint32) Min(COLUMNAR_BYTES_PER_PAGE - pageOffset,
									 remaining);
		Buffer		buffer;
		Page		page;
		PageHeader	phdr;

		if (blockno < nblocks)
		{
			/* reused freed range: overwrite the existing block in place */
			buffer = ReadBufferExtended(rel, MAIN_FORKNUM, blockno,
										RBM_NORMAL, NULL);
		}
		else
		{
			/*
			 * Fill any gap between the current EOF and the target block with
			 * empty, WAL-logged pages so a standby extends identically, then the
			 * final P_NEW lands exactly on the target block. In the common append
			 * case (blockno == nblocks) the gap loop does nothing.
			 */
			while (nblocks < blockno)
			{
				Buffer		gap = ReadBufferExtended(rel, MAIN_FORKNUM, P_NEW,
													 RBM_NORMAL, NULL);
				Page		gpage;

				LockBuffer(gap, BUFFER_LOCK_EXCLUSIVE);
				gpage = BufferGetPage(gap);
				PageInit(gpage, BLCKSZ, 0);
				START_CRIT_SECTION();
				MarkBufferDirty(gap);
				if (RelationNeedsWAL(rel))
					PageSetLSN(gpage, log_newpage_buffer(gap, true));
				END_CRIT_SECTION();
				UnlockReleaseBuffer(gap);
				nblocks++;
			}

			/* append: extend the relation by one block onto the target */
			buffer = ReadBufferExtended(rel, MAIN_FORKNUM, P_NEW,
										RBM_NORMAL, NULL);
			if (BufferGetBlockNumber(buffer) != blockno)
				elog(ERROR,
					 "columnar: unexpected block %u while writing stripe at "
					 "logical offset " UINT64_FORMAT " (expected %u)",
					 BufferGetBlockNumber(buffer), logicalOffset, blockno);
			nblocks = blockno + 1;
		}

		LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
		page = BufferGetPage(buffer);
		PageInit(page, BLCKSZ, 0);
		memcpy((char *) page + SizeOfPageHeaderData + pageOffset, src, n);

		phdr = (PageHeader) page;
		phdr->pd_lower = SizeOfPageHeaderData + pageOffset + n;

		START_CRIT_SECTION();
		MarkBufferDirty(buffer);
		if (RelationNeedsWAL(rel))
		{
			XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

			PageSetLSN(page, recptr);
		}
		END_CRIT_SECTION();

		UnlockReleaseBuffer(buffer);

		src += n;
		L += n;
		remaining -= n;
	}
}

/*
 * PgColumnarReadLogicalData
 *		Read a contiguous logical byte range into dest by walking the pages
 *		it maps to (spec 2.1).
 */
void
PgColumnarReadLogicalData(Relation rel, uint64 logicalOffset,
						char *dest, uint64 length)
{
	uint64		L = logicalOffset;
	uint64		remaining = length;
	char	   *dst = dest;

	if (remaining == 0)
		return;

#if PG_VERSION_NUM >= 170000
	if (pgcolumnar_enable_read_stream)
	{
		/*
		 * The blocks map to a contiguous ascending range, so a read stream can
		 * prefetch them (and use asynchronous I/O on PostgreSQL 18+).
		 * read_stream_next_buffer returns the pinned buffers in request order,
		 * so the same L/pageOffset walk drives the copy; the buffers are share-
		 * locked here just as the synchronous path does.
		 */
		PgColumnarBlockRange range;
		ReadStream *stream;

		range.next = (BlockNumber) (L / COLUMNAR_BYTES_PER_PAGE);
		range.last = (BlockNumber) ((L + remaining - 1) / COLUMNAR_BYTES_PER_PAGE);

		stream = read_stream_begin_relation(READ_STREAM_SEQUENTIAL, NULL, rel,
											MAIN_FORKNUM,
											pgcolumnar_read_stream_next, &range, 0);

		while (remaining > 0)
		{
			uint32		pageOffset = (uint32) (L % COLUMNAR_BYTES_PER_PAGE);
			uint32		n = (uint32) Min(COLUMNAR_BYTES_PER_PAGE - pageOffset,
										 remaining);
			Buffer		buffer = read_stream_next_buffer(stream, NULL);
			Page		page;

			/*
			 * The stream must yield exactly one buffer per block in the range.
			 * Guard at run time (not just Assert) so a corrupt offset that
			 * miscomputes the range ends the stream early with a clean error
			 * instead of LockBuffer(InvalidBuffer) dereferencing out of bounds.
			 */
			if (!BufferIsValid(buffer))
			{
				read_stream_end(stream);
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("columnar: read stream ended before the requested range")));
			}
			Assert(BufferGetBlockNumber(buffer) ==
				   (BlockNumber) (L / COLUMNAR_BYTES_PER_PAGE));
			LockBuffer(buffer, BUFFER_LOCK_SHARE);
			page = BufferGetPage(buffer);
			memcpy(dst, (char *) page + SizeOfPageHeaderData + pageOffset, n);
			UnlockReleaseBuffer(buffer);

			dst += n;
			L += n;
			remaining -= n;
		}
		read_stream_end(stream);
		return;
	}
#endif

	while (remaining > 0)
	{
		BlockNumber blockno = (BlockNumber) (L / COLUMNAR_BYTES_PER_PAGE);
		uint32		pageOffset = (uint32) (L % COLUMNAR_BYTES_PER_PAGE);
		uint32		n = (uint32) Min(COLUMNAR_BYTES_PER_PAGE - pageOffset,
									 remaining);
		Buffer		buffer;
		Page		page;

		buffer = ReadBuffer(rel, blockno);
		LockBuffer(buffer, BUFFER_LOCK_SHARE);
		page = BufferGetPage(buffer);
		memcpy(dst, (char *) page + SizeOfPageHeaderData + pageOffset, n);
		UnlockReleaseBuffer(buffer);

		dst += n;
		L += n;
		remaining -= n;
	}
}

/*
 * PgColumnarResetMetapage
 *		Reset the reserved high-water marks to their initial values, keeping
 *		the storage id. Used by non-transactional truncate.
 */
void
PgColumnarResetMetapage(Relation rel)
{
	Buffer		buffer;
	Page		page;
	PgColumnarMetapage *meta;

	buffer = ReadBuffer(rel, COLUMNAR_METAPAGE_BLOCKNO);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(buffer);
	meta = PgColumnarMetapagePointer(page);

	meta->reservedStripeId = 1;
	meta->reservedRowNumber = COLUMNAR_FIRST_ROW_NUMBER;
	meta->reservedOffset = COLUMNAR_FIRST_LOGICAL_OFFSET;

	START_CRIT_SECTION();
	MarkBufferDirty(buffer);
	if (RelationNeedsWAL(rel))
	{
		XLogRecPtr	recptr = log_newpage_buffer(buffer, true);

		PageSetLSN(page, recptr);
	}
	END_CRIT_SECTION();

	UnlockReleaseBuffer(buffer);
}

/*
 * Row-number <-> item-pointer mapping (spec 6). Row number 0 is invalid.
 *
 * There is deliberately no check against COLUMNAR_ROW_NUMBER_LIMIT here
 * (#2073). The writer refuses to reserve a row number at or past the limit,
 * and pgcolumnar.row_tid refuses one from SQL. A table written before that fix
 * may still hold such rows, and every scan maps them through here; an Assert
 * would abort the backend on a build with assertions, and an error would make
 * the table unreadable. The block number wraps instead, as it always did, so
 * such a table stays readable until pgcolumnar.vacuum renumbers it.
 */
void
PgColumnarRowNumberToItemPointer(uint64 rowNumber, ItemPointer tid)
{
	BlockNumber blockno;
	OffsetNumber offset;

	Assert(rowNumber > 0);
	blockno = (BlockNumber) (rowNumber / COLUMNAR_VALID_ITEMPOINTER_OFFSETS);
	offset = (OffsetNumber) ((rowNumber % COLUMNAR_VALID_ITEMPOINTER_OFFSETS) +
							 FirstOffsetNumber);
	ItemPointerSet(tid, blockno, offset);
}

/*
 * pgcolumnar.row_tid(row_number bigint) -> tid
 *		The item pointer the access method gives a row number, for SQL that
 *		needs a row group's TID range (refresh_rollup, #1294). The mapping lives
 *		here once: restated in SQL as 291 rows a block it held only at the
 *		default block size (#1319 review).
 */
PG_FUNCTION_INFO_V1(pgcolumnar_row_tid);
Datum
pgcolumnar_row_tid(PG_FUNCTION_ARGS)
{
	int64		rowNumber = PG_GETARG_INT64(0);
	ItemPointer tid;

	if (rowNumber < 1)
		ereport(ERROR,
				(errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
				 errmsg("row number " INT64_FORMAT " is out of range", rowNumber),
				 errdetail("Row numbers start at 1.")));
	if ((uint64) rowNumber >= COLUMNAR_ROW_NUMBER_LIMIT)
		ereport(ERROR,
				(errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
				 errmsg("row number " INT64_FORMAT " is out of range", rowNumber),
				 errdetail("The largest row number an item pointer can address is "
						   UINT64_FORMAT ".", COLUMNAR_ROW_NUMBER_LIMIT - 1)));
	tid = (ItemPointer) palloc(sizeof(ItemPointerData));
	PgColumnarRowNumberToItemPointer((uint64) rowNumber, tid);
	PG_RETURN_POINTER(tid);		/* PG_RETURN_ITEMPOINTER is 16+; tid is by reference */
}

uint64
PgColumnarItemPointerToRowNumber(ItemPointer tid)
{
	BlockNumber blockno = ItemPointerGetBlockNumber(tid);
	OffsetNumber offset = ItemPointerGetOffsetNumber(tid);

	return (uint64) blockno * COLUMNAR_VALID_ITEMPOINTER_OFFSETS +
		(offset - FirstOffsetNumber);
}

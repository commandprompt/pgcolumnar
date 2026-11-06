#!/usr/bin/env python3
#
# Result oracle for the Parquet decode fuzzer (test/fuzz_parquet.sh, #1852).
#
# The fuzzer used to check only that the backend survived a mutant. A mutant that
# read without an error was accepted even when the answer was wrong (#1855). This
# helper gives the fuzzer a second reader to compare with: pyarrow reads the mutant
# and writes what it read to a fresh, valid file. The fuzzer then reads that file
# with pgColumnar under the same column list. When pgColumnar read the mutant, its
# answer must equal its answer for pyarrow's rewrite, because both describe the
# same rows as pyarrow decoded them.
#
# pyarrow reads with the footer's "ARROW:schema" key renamed, so it builds its types
# from the Parquet schema alone. That key holds a copy of the Arrow schema, which
# pgColumnar never reads. A mutation that damages only that copy made pyarrow refuse
# a file whose rows pgColumnar read correctly, and the oracle called that a wrong
# answer. The suite checks that every pristine seed still reads the same this way.
#
# Usage:  python3 test/parquet_oracle.py MUTANT REWRITE
# Exit 0: pyarrow read MUTANT and wrote REWRITE.
# Exit 2: pyarrow refused MUTANT; the reason is on stdout.
#
import os
import sys

import pyarrow.parquet as pq


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "rb") as f:
        data = bytearray(f.read())
    # only inside the footer, when its length is sane; the rows are left alone
    flen = int.from_bytes(data[-8:-4], "little") if len(data) >= 12 else 0
    start = len(data) - 8 - flen if 0 < flen <= len(data) - 12 else 0
    data[start:] = data[start:].replace(b"ARROW:schema", b"ARROW:schemX")
    plain = dst + ".plain"
    with open(plain, "wb") as f:
        f.write(bytes(data))
    try:
        table = pq.read_table(plain)
    except Exception as exc:            # noqa: BLE001 - any refusal is the answer
        first = (str(exc).splitlines() or [""])[0][:200]
        print(f"pyarrow refuses: {type(exc).__name__}: {first}")
        sys.exit(2)
    finally:
        os.unlink(plain)
    pq.write_table(table, dst)
    os.chmod(dst, 0o644)


if __name__ == "__main__":
    main()

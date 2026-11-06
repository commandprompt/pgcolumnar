# pgColumnar - PGXS build
# Independent MIT implementation of the columnar table access method.

MODULE_big = pgcolumnar

OBJS = \
	src/columnar_tableam.o \
	src/columnar_storage.o \
	src/columnar_metadata.o \
	src/columnar_extent.o \
	src/columnar_write_state.o \
	src/columnar_sketch.o \
	src/columnar_compression.o \
	src/columnar_encoding.o \
	src/columnar_bloom.o \
	src/columnar_index.o \
	src/columnar_reader.o \
	src/columnar_delete_vector.o \
	src/columnar_customscan.o \
	src/columnar_runtime_filter.o \
	src/columnar_vector.o \
	src/columnar_vacuum.o \
	src/columnar_curve.o \
	src/columnar_unique.o \
	src/columnar_row_lock.o \
	src/columnar_arrow.o \
	src/columnar_flatbuffers.o \
	src/columnar_thrift.o \
	src/columnar_parquet_codec.o \
	src/columnar_parquet.o \
	src/columnar_visibilitymap.o \
	src/columnar_projection.o \
	src/columnar_parquet_reader.o \
	src/columnar_parallel_copy.o \
	src/columnar_parallel_export.o \
	src/columnar_avro.o \
	src/columnar_puffin.o \
	src/columnar_iceberg.o \
	src/columnar_iceberg_rest.o \
	src/columnar_iceberg_fdw.o \
	src/columnar_objstore.o \
	src/columnar_sink.o \
	src/columnar_autovacuum.o

EXTENSION = pgcolumnar
DATA = pgcolumnar--1.0-alpha5.sql pgcolumnar--1.0-dev--1.0-alpha.sql pgcolumnar--1.0-alpha--1.0-alpha2.sql pgcolumnar--1.0-alpha2--1.0-alpha3.sql pgcolumnar--1.0-alpha3--1.0-alpha4.sql pgcolumnar--1.0-alpha4--1.0-alpha5.sql
PGFILEDESC = "pgColumnar - column-oriented table access method"

# make installcheck. Not the project's gate -- that is test/run_all_versions.sh,
# which asserts properties with explicit controls -- but the conventional entry
# point a packager or a new contributor reaches for, and it must not report
# success while running nothing, which is what an empty REGRESS did.
REGRESS = pgcolumnar

# The race specs already exist under test/isolation/specs and were reachable only
# through test/isolation.sh. ISOLATION_OPTS points pg_isolation_regress at that
# directory rather than moving the specs to the layout PGXS assumes by default,
# so there is one copy of them and one set of expected files.
ISOLATION = $(notdir $(basename $(wildcard test/isolation/specs/*.spec)))
ISOLATION_OPTS = --inputdir=test/isolation

# Optional compression codecs. lz4 and zstd are linked when the system
# development libraries are present (detected with pkg-config); otherwise
# those codecs are compiled out cleanly and requests for them fall back to a
# codec that is built in (spec 5). pglz is always available from PostgreSQL.
PKG_CONFIG ?= pkg-config

ifeq ($(shell $(PKG_CONFIG) --exists liblz4 && echo yes),yes)
PG_CPPFLAGS += -DHAVE_LIBLZ4 $(shell $(PKG_CONFIG) --cflags liblz4)
SHLIB_LINK += $(shell $(PKG_CONFIG) --libs liblz4)
endif

ifeq ($(shell $(PKG_CONFIG) --exists libzstd && echo yes),yes)
PG_CPPFLAGS += -DHAVE_LIBZSTD $(shell $(PKG_CONFIG) --cflags libzstd)
SHLIB_LINK += $(shell $(PKG_CONFIG) --libs libzstd)
endif

# zlib, for reading GZIP-compressed Parquet pages (the Parquet GZIP codec is a
# gzip stream). PostgreSQL itself links zlib, but the extension must link it too
# to call inflate; when absent, GZIP Parquet files error cleanly.
ifeq ($(shell $(PKG_CONFIG) --exists zlib && echo yes),yes)
PG_CPPFLAGS += -DHAVE_LIBZ $(shell $(PKG_CONFIG) --cflags zlib)
SHLIB_LINK += $(shell $(PKG_CONFIG) --libs zlib)
endif

PG_CONFIG ?= pg_config

# Build note for source-built servers: PGXS hands this extension the CFLAGS the
# server was configured with, and PostgreSQL's configure adapts those to its own
# compiler. Build the extension with the compiler that configured the server, or
# it can be handed warning flags it does not recognise. That is a toolchain
# mismatch and not something this Makefile can paper over.
#
# Select the C standard by PostgreSQL major version. PostgreSQL 13 through 18
# are written to compile as C17 (their headers predate C23), so pin gnu17 there
# for a deterministic build regardless of the compiler's default. PostgreSQL 19
# uses C23 constructs in its headers (for example typeof_unqual in nodes.h), so
# it needs C23.
PG_MAJORVERSION := $(shell $(PG_CONFIG) --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')

PGXS := $(shell $(PG_CONFIG) --pgxs)

# NO SERVER, NO BUILD (#1344). Without a pg_config that answers, PGXS is empty, and
# every probe below reads a file named by it: the CC probe's sed was handed no file
# and read make's stdin, so a build in a background job or a CI step waited
# forever, and with stdin closed `make clean` exited 0 having cleaned nothing.
# Refuse here, before any of them runs, and say what to set.
ifeq ($(strip $(PGXS)),)
$(error PG_CONFIG=$(PG_CONFIG) gave no PGXS makefile; set PG_CONFIG to the pg_config of the server to build against)
endif

# HEADER DEPENDENCIES, BECAUSE PGXS DOES NOT EMIT THEM HERE (#1158). PostgreSQL
# generates .deps only when the SERVER was configured with --enable-depend, and
# `autodepend` is empty in the Makefile.global of every server this project is
# built against. Without it `make` sees src/*.o as newer than src/*.c and
# rebuilds NOTHING however the headers changed.
#
# That is not a slow build, it is a WRONG one, and it is silent. Measured on
# PG16 before this line existed: edit PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY in
# src/columnar.h, run `make`, and the build succeeds with pgcolumnar.so
# byte-identical at 2b8bfc2a6d75. Every removal proof that mutates a constant,
# macro, struct or inline function in a header ran against a stale object and
# reported a clean pass -- which reads as "the code is not load-bearing", the
# most expensive wrong answer a proof can give.
#
# -MMD writes a .d beside each .o listing the headers it included; -MP adds a
# phony target for each header so a DELETED header does not break the build with
# "No rule to make target". The `-include` below is after the PGXS include,
# because OBJS must be expanded by then.
PG_CFLAGS += -MMD -MP

# The compiler PGXS will use, for the probe below. CC has to be settled before
# the include, because PG_CFLAGS is only honored if it is set before it, so the
# value is read out of the server's Makefile.global unless the caller named one
# on the command line. Probing with make's default cc instead would test a
# compiler that may not be the one that does the build.
PG_MAKEFILE_GLOBAL := $(patsubst %/makefiles/pgxs.mk,%/Makefile.global,$(PGXS))
PROBE_CC := $(if $(filter command line,$(origin CC)),$(CC),\
	$(shell sed -n 's/^CC = //p' $(PG_MAKEFILE_GLOBAL) </dev/null 2>/dev/null | head -1))
PROBE_CC := $(if $(strip $(PROBE_CC)),$(PROBE_CC),cc)

ifeq ($(shell test "$(PG_MAJORVERSION)" -ge 19 && echo yes),yes)
# GCC 14 and later spell C23 "gnu23"; GCC 13 accepts only "gnu2x" and rejects
# the newer spelling outright. Both name the same language, and GCC 13 compiles
# PostgreSQL 19's C23 headers under gnu2x, so ask the compiler which spelling it
# takes rather than hardcode one (#294). Hardcoding gnu23 failed on GCC 13 with
# an error naming a flag the user never set, which reads as a defect in this
# project rather than a toolchain difference.
C23_STD := $(shell echo 'int main(void){return 0;}' \
	| $(PROBE_CC) -std=gnu23 -x c -c -o /dev/null - >/dev/null 2>&1 \
	&& echo gnu23 || echo gnu2x)
PG_CFLAGS += -std=$(C23_STD)
else
PG_CFLAGS += -std=gnu17
endif

include $(PGXS)

# The generated dependency files. A leading `-` so the first build, which has
# none, is not an error.
-include $(OBJS:.o=.d)

# The object-store module is a SEPARATE shared library, built and installed
# alongside this one but never linked into it. See src/columnar_objstore.h: this
# extension is preloaded, so anything it links reaches the postmaster.
#
# A build failure there must not be silent, so these do not use the `-` prefix.
OBJSTORE_DIR = $(realpath $(dir $(firstword $(MAKEFILE_LIST))))/objstore

all: objstore-all
objstore-all:
	$(MAKE) -C $(OBJSTORE_DIR) PG_CONFIG=$(PG_CONFIG) all

install: objstore-install
objstore-install:
	$(MAKE) -C $(OBJSTORE_DIR) PG_CONFIG=$(PG_CONFIG) install

# PGXS's own clean removes OBJS and knows nothing about the .d files beside
# them, so a stale .d would survive `make clean` and name headers that have
# moved.
clean: objstore-clean depclean
depclean:
	rm -f $(OBJS:.o=.d)
objstore-clean:
	$(MAKE) -C $(OBJSTORE_DIR) PG_CONFIG=$(PG_CONFIG) clean

.PHONY: objstore-all objstore-install objstore-clean depclean

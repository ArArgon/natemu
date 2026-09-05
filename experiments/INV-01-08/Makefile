CC      ?= clang
CFLAGS  ?= -O2 -Wall -Wextra -Werror -std=c11
BUILD   ?= /tmp/inv-01-08-build-not-selected
BIN     ?= $(BUILD)/litmus
REPO    := $(shell git rev-parse --show-toplevel)
REV     := $(shell git rev-parse HEAD)
CPPFLAGS += -DF08_REPO='"$(REPO)"' -DF08_CODE_REV='"$(REV)"'

all: $(BIN)

$(BIN): src/litmus.c src/fallback.h src/launch_guard.h | guard $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $<

guard:
	@test "$(BUILD)" != /tmp/inv-01-08-build-not-selected || (echo 'Select an explicit external BUILD directory' >&2; exit 2)
	@python3 -c 'import pathlib; p=pathlib.Path("$(BUILD)").resolve(); b=pathlib.Path("$(BIN)").resolve(); r=pathlib.Path("$(REPO)").resolve(); assert not p.is_relative_to(r) and b.is_relative_to(p) and p != pathlib.Path("/") and p != pathlib.Path.home()'

$(BUILD): guard
	mkdir -p "$(BUILD)"

# Disassembly the code gate uses to confirm the litmus bodies emit the
# instructions they claim (plain LDR/STR, LDAR/STLR, DMB ISH, LDADDAL, CAS).
disasm: $(BIN)
	xcrun llvm-objdump -d --no-show-raw-insn $(BIN) > $(BUILD)/litmus.disasm
	$(CC) $(CPPFLAGS) $(CFLAGS) -S -o $(BUILD)/litmus.s src/litmus.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -DLITMUS_VERIFY -o $(BUILD)/litmus-verify src/litmus.c
	xcrun llvm-objdump -d --no-show-raw-insn $(BUILD)/litmus-verify > $(BUILD)/litmus-verify.disasm
	@echo wrote $(BUILD)/litmus.disasm and $(BUILD)/litmus-verify.disasm

clean:
	@echo 'No recursive clean: remove only your explicitly selected temporary build after inspection.'

.PHONY: all clean disasm guard

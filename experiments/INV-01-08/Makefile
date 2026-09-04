CC      ?= clang
CFLAGS  ?= -O2 -Wall -Wextra -Werror -std=c11
BUILD   ?= build
BIN     ?= $(BUILD)/litmus

all: $(BIN)

$(BIN): src/litmus.c | $(BUILD)
	$(CC) $(CFLAGS) -o $@ $<

$(BUILD):
	mkdir -p $(BUILD)

# Disassembly the code gate uses to confirm the litmus bodies emit the
# instructions they claim (plain LDR/STR, LDAR/STLR, DMB ISH, LDADDAL, CAS).
disasm: $(BIN)
	xcrun llvm-objdump -d --no-show-raw-insn $(BIN) > $(BUILD)/litmus.disasm
	$(CC) $(CFLAGS) -DLITMUS_VERIFY -o $(BUILD)/litmus-verify src/litmus.c
	xcrun llvm-objdump -d --no-show-raw-insn $(BUILD)/litmus-verify > $(BUILD)/litmus-verify.disasm
	@echo wrote $(BUILD)/litmus.disasm and $(BUILD)/litmus-verify.disasm

clean:
	rm -rf $(BUILD)

.PHONY: all clean disasm

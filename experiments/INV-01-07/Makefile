CC      ?= clang
CFLAGS  ?= -O2 -Wall -Wextra -Werror -std=c11 -fblocks
LDFLAGS ?=
BIN     ?= $(BUILD)/timers
BUILD   ?= build

all: $(BIN)

$(BIN): src/timers.c | $(BUILD)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)

.PHONY: all clean

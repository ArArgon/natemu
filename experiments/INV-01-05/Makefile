# Build only. Runs are driven by run.sh, which builds into the raw-data root
# so that nothing is written under experiments/.
BUILD ?= build
CC ?= cc
CFLAGS ?= -O1 -Wall -Wextra -std=c11

$(BUILD)/probe: src/probe.c
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -o $@ src/probe.c $(LDLIBS)

.PHONY: clean
clean:
	rm -rf build

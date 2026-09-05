.PHONY: syntax model fixtures plan build

syntax:
	clang -fsyntax-only -isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX26.4.sdk -O2 -Wall -Wextra -Werror -std=c11 src/protocol.c

model:
	PYTHONDONTWRITEBYTECODE=1 python3 model.py

fixtures:
	PYTHONDONTWRITEBYTECODE=1 python3 tests.py

plan:
	PYTHONDONTWRITEBYTECODE=1 python3 run.py plan

# BUILD must name a fresh directory beneath external NATEMU_RAW_ROOT.
build:
	PYTHONDONTWRITEBYTECODE=1 python3 build.py --output "$(BUILD)"

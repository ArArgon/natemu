# fexcore_build — artifacts from the FEXCore standalone build test

Supporting material for `docs/research/23-fexcore-retrofit.md` §7.1, which reports that
**52 of 56 `FEXCore_object` translation units compile on macOS 26.5.2 / arm64**
(FEX @ `fd141ed6d721d03062619e4702bca1a0c93b6dd9`, Homebrew clang 22.1.8, cmake 4.3.4, ninja).

The build itself ran entirely in `/tmp/fexbuild` against a shallow clone in `/tmp/fexread/FEX`;
nothing was built inside this repo. `/tmp` does not survive a reboot, so the two inputs that
took work to derive are checked in here:

- `shim/` — the six shim headers (`malloc.h`, `linux/futex.h`, `linux/limits.h`, `sys/prctl.h`,
  `sys/sendfile.h`, `syscall.h`, plus `fexshim.h`) that stand in for Linux headers absent on Darwin.
- `configure4.log`, `build6.log` — the successful configure and the final compile round.

The cmake flags and `-D` defines are listed in doc 23 §7.1 alongside the error each one fixes.

**Read the caveat in doc 23 T-2 before citing this.** The shims are deliberately non-functional
(`prctl` returns −1, `SYS_futex` is defined as 0, several futex constants are invented). This
result is evidence about *header and API coupling to Linux* — which is what §7 needed — and about
nothing else. A library built with these shims would not run.

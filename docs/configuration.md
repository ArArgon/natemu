# Configuring a natemu launch

A launch can be described in a TOML file and started with `natemu --config FILE`. The file holds the
same settings as the command-line flags, plus a device list (several disks, read-only disks) that
the flags cannot express. Flags still work alone, exactly as before.

Settings are layered, lowest first: built-in defaults, the file, the deprecated environment
variables, the flags, then `--set KEY=VALUE`. A later layer overrides an earlier one. Paths in the
file are relative to the file's folder; paths in flags and `--set` are relative to the working
directory. `--fexc` is only required when the file does not give `[engine] fexc`, and `--kernel` only when
the file gives no `[boot.kernel] image` and no firmware image is booted (`--firmware` or `[boot.firmware]`).

## An annotated example

This is `crates/cli/tests/example.toml`, which the tests parse, so it stays valid.

```toml
# debian.toml: a direct-boot Debian launch. Relative paths mean this file's folder.

[machine]                    # what the guest sees
ram = "4G"                   # bytes, 0x hex, or a K/M/G suffix
vcpus = 2
transport = { disk = "pci", nic = "pci", rng = "pci" }   # "mmio" or "pci"

[serial]                     # COM1's host end
output = "serial.log"        # a file; the word "stdout" means standard output
stdin = false                # true: the terminal is the console (Ctrl-A x quits)

[engine]                     # how natemu runs the guest
fexc = "../build/fexc"
fp = "fast"                  # or "accurate"
compile_cache = "off"        # a folder or "off"; left out, the user cache folder
compile_cache_mb = 512
speculation_barriers = false

[boot.kernel]
image = "vmlinuz-6.12.107"
initrd = "initrd.img-6.12.107"
cmdline = "console=ttyS0 root=/dev/vda1 ro"

[[disk]]                     # the guest sees disks in this order: /dev/vda, /dev/vdb, ...
id = "root"
path = "runs/r1/root.qcow2"  # a qcow2 overlay, created at the first launch when missing (runs/r1 must exist)
backing = "debian13.raw"     # its base: opened read-only, never written

[[disk]]
id = "data"
path = "data.raw"

[[disk]]
id = "tools"
path = "tools.iso"
readonly = true              # an ISO, or any image the guest must not change

[[disk]]
id = "scratch"
path = "clean.raw"
snapshot = true              # the guest may write; every change is thrown away when the VM exits

[[nic]]                      # user-mode NAT (slirp)
id = "net0"
forward = ["tcp:2222:22", "8080:80"]   # host 127.0.0.1 port : guest port
host_loopback = false

[[rng]]
id = "rng0"

[debug]                      # experiment switches; the defaults are right for normal use
pool = true
check_reuse = false
page_currency = "on"         # on, off or check
writable_on_read = true
perf_log = "perf.log"
inspect = true               # SIGINFO (Ctrl-T) prints each vCPU's state; the run goes on
watchdog = 60                # seconds; a guest hang ends the run with the vCPU's report
```

## Sections and defaults

Every table rejects unknown keys, so a misspelled key is an error, not silently ignored.

| Section | Key | Default | Meaning |
| --- | --- | --- | --- |
| `[machine]` | `ram` | `2G` | Guest RAM: a size string (as `--ram`) or an integer of bytes |
| | `vcpus` | `1` | Processors |
| | `transport` | all `pci` with a file | `disk`, `nic`, `rng`, `console`, each `"mmio"` or `"pci"` |
| | `pvpanic` | `true` | The pvpanic device (ACPI `QEMU0001`, port 505H): a guest kernel panic ends the run (`natemu: Power(4)`, exit status 2) |
| | `smbios` | `true` | SMBIOS 3.0 tables naming the machine `natemu` (what `dmidecode` and `/sys/class/dmi/id` show), the entry point at F0000H; a firmware boot or a Multiboot kernel ignores it |
| | `ps2` | `true` | An 8042 controller with a PS/2 keyboard and mouse (no host input reaches them yet); `false` leaves port 64H a reset-only register |
| `[serial]` | `output` | standard output | A file path, or the word `stdout` |
| | `stdin` | `false` | Feed standard input to COM1 |
| `[engine]` | `fexc` | none (required) | The fexc compiler |
| | `runtime` | the resident built beside the binary | The resident runtime ELF |
| | `fp` | `"fast"` | `"fast"` or `"accurate"` |
| | `compile_cache` | the user cache folder | A folder, or `"off"` |
| | `compile_cache_mb` | `512` | Cache cap in MiB |
| | `speculation_barriers` | `false` | Page-walk barriers (needs FEAT_SB) |
| `[boot.kernel]` | `image` | none (required) | The Linux bzImage |
| | `initrd`, `cmdline` | none, empty | Initramfs and kernel command line |
| `[boot.firmware]` | `image` | none (required) | A firmware image to boot instead of a kernel (`--firmware FILE`; see below) |
| | `start` | `"reset"` | `"reset"` or `"flat32"`: where the first processor starts (see below) |
| `[[disk]]` | `id`, `path` | required | A disk image |
| | `readonly` | `false` | The guest cannot write it |
| | `backing` | none | The base image of a qcow2 overlay at `path` (see below) |
| | `snapshot` | `false` | The guest's writes are thrown away when the VM exits; `path` is never changed (see below) |
| `[[nic]]` | `id` | required | User-mode NAT network device |
| | `forward` | none | List of `[tcp\|udp:]HOST_PORT:GUEST_PORT` strings |
| | `host_loopback` | `false` | The guest may reach the Mac's `127.0.0.1` via `10.0.2.2` |
| `[[rng]]` | `id` | required | Entropy device |
| `[[console]]` | `id` | required | A second console for the guest (see below) |
| | `output` | none | A file: what the guest writes to the console is added to its end (created when missing) |
| | `socket` | none | A Unix socket natemu listens on: a program that connects talks to the guest's console (see below) |
| `[control]` | `socket` | none | A Unix socket natemu listens on for programs that drive the VM: keyboard and mouse input, quit, status ([the control API](control-api.md)) |
| `[display]` | `device` | `"none"` | `"bochs"` gives the guest a display (see below) |
| | `vram_mib` | `16` | The display's video memory in MiB: 4, 8, 16, 32 or 64 |
| | `width`, `height` | `1024`, `768` | The picture size the display tells the guest it prefers |
| `[debug]` | `pool` | `true` | Shared template pool |
| | `check_reuse` | `false` | Verify every reused block (a mismatch stops the run) |
| | `page_currency` | `"on"` | `on`, `off` or `check` |
| | `writable_on_read` | `true` | Map a page writable on a demand read of a dirty entry |
| | `perf_log` | none | Write the timing log to a file |
| | `inspect` | `true` | SIGINFO (Ctrl-T in a terminal, `kill -INFO`) prints every vCPU's state and the run goes on |
| | `watchdog` | `60` | Seconds a vCPU may run guest code without a host exit before the run ends with its report; a vCPU waiting that long in the host (outside a compile) is reported once and the run goes on |
| | `pci_testdev` | `false` | Test option: a PCI test device for guest test programs (see below) |
| | `mapped_testdev` | `false` | Test option: a PCI test device whose memory the guest reads and writes directly (see below) |

The `[debug]` keys are experiment and measurement switches; leave them alone for normal use.

`pci_testdev = true` is a test option, for programs that test how a guest handles PCI devices (such as
kvm-unit-tests' `pci-testdev` test). It adds QEMU's PCI test device (`pci-testdev`, vendor 1B36H, device 0005H) as
two functions, 0 and 1, of the lowest PCI slot no other device uses. Each has a memory and an I/O region with a small
table of tests; function 1 also has a 64 KiB memory region in which every 4-byte word reads as its own offset. The
guest gets a PCI bus for it even when every other device is on `mmio`, and the bus then also passes the I/O ports
C000H-FFFFH to its devices. The launch fails when all 31 slots are taken. The register layout is described in
`crates/host/src/pci/testdev.rs`.

`mapped_testdev = true` is a test option too. It adds natemu's own test device (vendor 1B36H, device 0005H, subsystem
0100H) as function 0 of the lowest PCI slot still free. Its first memory region, 2 MiB, is memory held by natemu that
the guest reads and writes at full speed, wherever the guest places it; its second, 4 KiB, has two registers: one reads
the sum of the first region's contents and one fills it with a pattern. When it is on, natemu prints one more line to
standard error before the stop line, `natemu: mapped {...}`, with how often the guest moved the region and how the
processors followed. The register layout is described in `crates/host/src/pci/mapped_testdev.rs`.

`[boot]` must name exactly one boot method: `[boot.kernel]` or `[boot.firmware]`; `[boot.uefi]` is refused
for now.

### Booting a firmware image

`[boot.firmware]` (or `--firmware FILE`) boots a firmware image instead of a Linux kernel, the way a PC
starts: the image sits at the top of the first 4 GiB of the guest's address space (a 4 MiB image at
FFC00000H to FFFFFFFFH), read-only as a PC's flash ROM is (the guest's writes to it are ignored, except that a
few kinds of store in 64-bit code stop the run with `unsupported_mmio` instead: store pairs, pushes, stores
that cross a page, read-modify-write instructions such as `add [rom], eax`, and LOCK or XCHG), and the
first processor starts at its reset vector, the last 16 bytes, in the state a processor has right after
power-on: 16-bit real mode. `start = "flat32"` starts it at the same address in 32-bit protected mode with
flat segments, the start some firmware (OVMF's `Main32`) also takes. The other processors wait until the
firmware starts them. The image must be whole 4 KiB pages, at most 16 MiB. natemu loads nothing else: no
initrd, no command line, no ACPI tables, and no virtio devices (a launch with any of them is refused).
This is a development feature: natemu runs a firmware's 16- and 32-bit start-up code with its boot-time
interpreter, which does not yet run every instruction such code uses
([docs/restructure/firmware-mode.md](restructure/firmware-mode.md)).

### Transport default

Virtio devices attach over PCI when a `--config` file is given and over MMIO when there is none
(the old command lines). `machine.transport` or `--virtio-transport` overrides either.

### A second console

`[[console]]` gives the guest a console besides COM1: a virtio console, which Linux calls `/dev/hvc0` (the
next one `/dev/hvc1`). It needs exactly one of `output` and `socket`. With `output = "console.log"`,
everything the guest writes to `/dev/hvc0` is added to the end of that file, and the guest reads
nothing from it. A guest that writes faster than the Mac takes the bytes waits for it, as it would at a
slow terminal; at power-off natemu waits up to 2 seconds for the last bytes to reach the file, then
drops what is left and says so on standard error. `machine.transport.console` chooses MMIO or PCI for
every console.

With `socket = "console.sock"`, natemu listens on that Unix socket (a path of at most 103 bytes) and
removes it when it exits; a socket an earlier run left behind is replaced. One program at a time can
connect, for example `nc -U console.sock` or `socat - UNIX-CONNECT:console.sock`: what it sends, the
guest reads from `/dev/hvc0`, and what the guest writes, it receives. A second connection while one is
open is closed at once. While nobody is connected, what the guest writes is dropped: the guest does not
wait, and natemu says at exit on standard error how many bytes it dropped. After the program
disconnects, the next one to connect gets the guest's later output. A connected program that stops
reading makes the guest wait, as a slow terminal does; at power-off natemu waits for it at most 2
seconds. A program that closes its sending half counts as disconnected and receives nothing after
that, so `echo cmd | socat - UNIX-CONNECT:console.sock` gets no reply: use an interactive client.

Unless the guest sets `/dev/hvc0` to raw mode (for example `stty -F /dev/hvc0 raw`), Linux's terminal
layer adds a carriage return before every line feed the guest writes, so the file or the connected
program receives CR LF line ends.

```toml
[[console]]
id = "log"
output = "guest-console.log"
```

### A display

`[display] device = "bochs"` gives the guest a display: the device QEMU calls `bochs-display` (vendor 1234H, device
1111H), which Linux's `bochs` driver runs. It is a PCI device, at the lowest PCI slot no other device uses (after the
`[debug]` test devices), so the guest gets a PCI bus for it even when every other device is on `mmio`; turning it on
moves no other device. The guest draws into the display's video memory directly, at full speed. Nothing on the Mac
shows the picture yet; the console stays on the serial port.

`vram_mib` is the size of the video memory: a power of two from 4 to 64. 64 is the most because natemu places
every PCI device's memory in one 256 MiB range, largest first, and a 128 MiB one would fill the part of that range
it lands in. `width` and `height` are the picture size the display reports as its preferred one (in its EDID, the
description a monitor gives its computer), with 800 x 600 and 640 x 480 as the other sizes it offers. Each must be
at least 64 and at most 4095, and very large sizes are refused (at 60 pictures a second they would need a faster
signal than an EDID can describe; 4095 x 2048 is accepted). The video memory must hold two pictures of the preferred
size (`vram_mib` MiB at least 2 x width x height x 4 bytes), because Linux offers only sizes that fit twice: 1920 x
1080 needs 16, 2560 x 1600 needs 32. These limits are checked when natemu starts, even with `device = "none"`. A
firmware boot has no PCI bus, so it refuses the display.

```toml
[display]
device = "bochs"
width = 1280
height = 800
```

### Serial: `stdout` as a keyword and as a file name

`[serial] output = "stdout"` (and `--set serial.output=stdout`) means standard output. The flag
`--serial stdout` is a file named `stdout` in the working directory, as it always was. To write
to standard output with the flag, leave `--serial` out.

### The device list and guest names

The guest names disks in list order: the first `[[disk]]` is `/dev/vda`, the second `/dev/vdb`, and so on.
Inserting a disk in the middle renumbers every later one, so a `root=` on the command line must
follow the list. Name devices by `id` only in the file and in `--set`; the guest never sees ids.

## Overriding with `--set`

`--set KEY=VALUE` is repeatable and wins over everything else. Keys are the table path:
`--set machine.ram=8G`, `--set engine.fp=accurate`, `--set boot.kernel.cmdline="console=ttyS0 root=/dev/vdb1"`.
A device is addressed by its id: `--set disk.root.path=other.raw`, `--set disk.tools.readonly=false`,
`--set nic.net0.forward=tcp:2222:22,8080:80` (a comma-separated list). `--set` can change a listed
device but cannot create one. Setting one key twice on the command line (by flag, `--set`, or
both) is an error. `--disk`, `--net` and `--rng` are refused when the file lists devices of that
kind. `--set machine.transport` takes both `nic=` and the old `net=`. `--set machine.pvpanic=false`,
`--set machine.smbios=false` and `--set machine.ps2=false` turn those devices off (the benchmarks run with all three off).

## Deprecated environment variables

Ten flags can still be set from the environment: `NATEMU_SERIAL_STDIN`, `NATEMU_SPECULATION_BARRIERS`,
`NATEMU_FP`, `NATEMU_PAGE_CURRENCY`, `NATEMU_WRITABLE_ON_READ`, `NATEMU_POOL`, `NATEMU_CHECK_REUSE`,
`NATEMU_COMPILE_CACHE`, `NATEMU_COMPILE_CACHE_MB` and `NATEMU_PERF_LOG`. They sit above the file and
below the flags. Each one used prints once to standard error, for example:

```
natemu: NATEMU_FP is deprecated; use [engine] fp or --set engine.fp=...
```

## Errors you can see

Every message is one line, starts with `natemu:`, and names the file (or `config` when there is
none) and the key. All of these are found before the VM starts, and the process exits 1.

| Message (shortened) | Meaning |
| --- | --- |
| `c.toml: bogus: unknown field 'bogus', expected one of ...` | A misspelled or unknown table |
| `c.toml: disk[0].fromat: unknown field 'fromat', expected one of ...` | A misspelled key; `disk[0]` is the first `[[disk]]` |
| `c.toml: string values must be quoted, expected literal string` | TOML syntax error (the text is the TOML parser's) |
| `c.toml: machine.ram: bad size 4X: bytes, 0x hex, or a K/M/G suffix` | `ram` is not a size |
| `c.toml: machine.transport.disk: bad transport "usb": mmio or pci` | Transport is not `mmio` or `pci` |
| `c.toml: [boot] needs exactly one boot method, [boot.kernel], [boot.firmware] or [boot.uefi]; found none` | `[boot]` without a method (also `found more`) |
| `c.toml: [boot.uefi]: UEFI boot is planned (M3) and not built yet` | No UEFI boot yet |
| `c.toml: [boot.kernel] needs image (or pass --kernel FILE)` | The table has no kernel |
| `c.toml: no boot method: --kernel FILE, or [boot.kernel] in a --config file (or a firmware image: --firmware FILE)` | No kernel or firmware anywhere |
| `c.toml: two boot methods: a kernel and a firmware image (...)` | A kernel and a firmware image both given |
| `c.toml: [boot.firmware] needs image (or pass --firmware FILE)` | The table has no image |
| `c.toml: a firmware boot takes no initrd or kernel command line` | Also `has no virtio devices yet` for a disk, nic, rng or console |
| `c.toml: no fexc: --fexc FILE, or [engine] fexc in a --config file` | No compiler anywhere |
| `c.toml: device id "A b" must match [a-z0-9_-]+ (disk 1)` | An id has other characters |
| `c.toml: device id "a" is used twice (disk 1 and nic 1)` | Ids are unique across all device kinds |
| `c.toml: at most one [[nic]] is supported in M1; found 2` | Also for `[[rng]]` |
| `c.toml: disks "a" and "b" are the same file; that is allowed only when both are readonly or snapshot` | One image, two writers |
| `c.toml: nic[0].forward: bad forward "x": [tcp\|udp:]HOST_PORT:GUEST_PORT, decimal ports 1-65535` | A forward is malformed |
| `c.toml: console[0]: needs exactly one of output, socket; found none` | Also `found both` |
| `console "log": c.sock: exists and is not a socket` | Something other than a socket is at the `socket` path (also `another program listens on this socket`, and a path over 103 bytes) |
| `control socket vm.sock: another program listens on this socket` | Another natemu (or program) uses the `[control] socket` path (also `exists and is not a socket`, and a path over 103 bytes) |
| `console "log": no interrupt line left on mmio (lines 16-23 are all held); use pci` | Every MMIO interrupt line is taken (6 MMIO disks with the NIC and entropy device) |
| `console "log": PCI slots 1-31 are all used; use mmio or fewer disks on pci` | 29 disks on PCI leave no slot |
| `c.toml: display.device: unknown display device "vga": bochs or none` | Only the Bochs display exists |
| `c.toml: display.vram_mib: 3: a power of two from 4 to 64 (MiB)` | `vram_mib` is not 4, 8, 16, 32 or 64 |
| `c.toml: display.width, display.height: 63 x 768: not a mode the EDID can describe (...)` | A side under 64 or over 4095, or a size too large for 60 pictures a second |
| `c.toml: display.vram_mib: 4 MiB holds fewer than two 1024 x 768 frames; 1024 x 768 needs at least 8` | The video memory is too small for the preferred size |
| `c.toml: a firmware boot has no PCI bus for the display ([display] device)` | A firmware boot with the display on |
| `--set bad: expected key=value` | `--set` without `=` |
| `--set disk.zz.path: no disk with id "zz" (an override cannot create a device)` | Unknown device id |
| `--set machine.nope: unknown key; expected one of ...` | Unknown key; the message lists all valid keys |
| `--set machine.ram: set twice on the command line` | A key given twice |
| `--set machine.ps2: bad boolean "maybe": 1/0, yes/no, on/off or true/false` | A switch (`machine.pvpanic`, `machine.smbios`, `machine.ps2`, ...) given something else |
| `--disk: the config file lists [[disk]] devices; use --set disk.ID.path=... or edit the file` | Flag and file disagree on who owns the devices |
| `c.toml: disk "a" is its own backing file` | `backing` and `path` are one file |
| `c.toml: the backing file of disk "a" is disk "b", which is writable; ...` | A base must not change under its overlay |
| `disk top.qcow2: top.qcow2 is based on /x/base.raw; add backing = "/x/base.raw" to this disk (or --set disk.disk0.backing=/x/base.raw)` | An overlay opened without its base |
| `disk top.qcow2: top.qcow2 is based on /x/base.raw, not on the backing /y/other.raw` | `backing` names another file than the overlay's |
| `disk d.raw: a backing file needs a qcow2 image` | `backing` on a raw image |
| `c.toml: disks "a" and "b" both name /x/new.qcow2, which does not exist yet; ...` | One new overlay named by two disks |
| `disk top.qcow2: another launch created this overlay first; launch again to use it` | Two launches created one overlay at once |
| `disk top.qcow2: does not exist; a read-only overlay is never created ...` | `readonly` and `backing` on a missing file |

## Limits

- At most 6 disks on MMIO and 29 on PCI. Over either, the launch is refused with
  `at most 6 disks on mmio (interrupt lines 16-23); use pci for more` (or
  `PCI slots 1-31 are all used: at most 29 disks on pci`).
- One NIC and one entropy device.
- A console on MMIO needs one of the interrupt lines 16-23 that no other MMIO device holds, and one on
  PCI a slot after the disks'; a launch without one is refused, naming the console.
- One display, of at most 64 MiB of video memory (see [A display](#a-display)).
- No `[boot.uefi]` yet.
- Disk images are detected by content, not name (raw or qcow2; VMDK, VHDX, VDI, VHD and DMG are
  refused by name).
- **Overlays.** A disk with `backing` is a qcow2 overlay over that base (raw or qcow2). If `path`
  does not exist, natemu creates it (never overwriting a file, and never creating folders): a qcow2 v3 image, 64 KiB clusters,
  the base's virtual size, recording the base's absolute path and format, as `qemu-img create -f
  qcow2 -b BASE -F FMT` would. If it exists, it must be a qcow2 image based on that same file
  (compared by device and inode). A read-only disk with `backing` must already exist. The base is
  opened read-only with a shared lock, so several overlays (and read-only disks) may share it; it
  must not be another disk's writable image. An overlay whose base the configuration does not name
  is refused, with the key to add (a downloaded image could otherwise name any host file). Bases
  below the configured one must be named relative to their image's folder, with a backing format.
- **Snapshot disks** (`snapshot = true`, or `--set disk.disk0.snapshot=true` with `--disk`): the guest can write
  to the disk as usual, but every change is thrown away when the VM exits, as with QEMU's `-snapshot`. natemu
  creates an empty overlay over `path` in the temporary folder and deletes the file as soon as it is open, so the
  changes live only in that open, already-deleted file and the Mac frees its space at exit, even after a crash.
  `path` is opened read-only, so other read-only or snapshot disks may share it. Not with `readonly` or `backing`.
- The new overlay is written under a temporary name in the same folder,
  `.NAME.natemu-new-PID-TIME-N`, and moved to `path` only when complete, so `path` never holds a
  partial image. A crash while creating leaves at most such a file; natemu never reads it, and it
  may be deleted.
- An overlay natemu creates records its base by absolute path, so in M1 it cannot itself be the
  `backing` of another overlay: links below the configured base must be relative names (a
  downloaded image could otherwise name any host file). Point the second overlay's `backing` at
  the original base instead, or make the chain with qemu-img using relative names
  (`qemu-img create -f qcow2 -b mid.qcow2 -F qcow2 top.qcow2` in the folder of `mid.qcow2`, itself
  made with a relative `-b`), and name the chain's top base as `backing`.
- **Heavy writes to a new image can pause for seconds.** A sparse raw image, or a qcow2 image that is
  still growing, gets its space from macOS as the guest writes. While the guest's disk flush is being
  written out, macOS holds those writes back. A busy Mac can take 10–20 s to write the flush out, and
  the guest then waits that long (natemu reports `virtio-block: slow host disk` past 2 s; it gives up
  on a single disk operation only after 2 minutes). Writes into space the image already holds don't
  wait, so preallocating the image (`mkfile 16g disk.raw` for raw, or `qemu-img create -f qcow2 -o
  preallocation=full disk.qcow2 16G` for qcow2) keeps writes going; a guest that waits for its own
  flush still waits as long.
- Two read-only disks on one file get different serials: the second use's serial starts with a
  letter after `f` (`g`, `h`, ...), so it matches no file's.
- A file's `[boot.kernel]` must give `image` unless `--kernel` supplies it, and `[boot.firmware]` unless
  `--firmware` does.

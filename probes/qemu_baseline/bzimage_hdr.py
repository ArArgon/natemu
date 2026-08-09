#!/usr/bin/env python3
"""bzimage_hdr.py -- read-only: decode the Linux x86 setup_header of a bzImage.

For doc 24: turns "the boot protocol says X" into "the kernel we actually boot
says X".  Parses arch/x86/include/uapi/asm/bootparam.h struct setup_header out
of the image at offset 0x1f1.  Touches nothing.

  ./bzimage_hdr.py work/nb-x86_64/boot/vmlinuz-virt
"""
import struct, sys

F = [  # (offset, size, name)
    (0x1f1,1,"setup_sects"), (0x1f2,2,"root_flags"), (0x1f4,4,"syssize"),
    (0x1f8,2,"ram_size"), (0x1fa,2,"vid_mode"), (0x1fc,2,"root_dev"),
    (0x1fe,2,"boot_flag"), (0x200,2,"jump"), (0x202,4,"header"),
    (0x206,2,"version"), (0x208,4,"realmode_swtch"), (0x20c,2,"start_sys_seg"),
    (0x20e,2,"kernel_version"), (0x210,1,"type_of_loader"),
    (0x211,1,"loadflags"), (0x212,2,"setup_move_size"),
    (0x214,4,"code32_start"), (0x218,4,"ramdisk_image"), (0x21c,4,"ramdisk_size"),
    (0x224,2,"heap_end_ptr"), (0x226,1,"ext_loader_ver"), (0x227,1,"ext_loader_type"),
    (0x228,4,"cmd_line_ptr"), (0x22c,4,"initrd_addr_max"),
    (0x230,4,"kernel_alignment"), (0x234,1,"relocatable_kernel"),
    (0x235,1,"min_alignment"), (0x236,2,"xloadflags"), (0x238,4,"cmdline_size"),
    (0x23c,4,"hardware_subarch"), (0x240,8,"hardware_subarch_data"),
    (0x248,4,"payload_offset"), (0x24c,4,"payload_length"), (0x250,8,"setup_data"),
    (0x258,8,"pref_address"), (0x260,4,"init_size"), (0x264,4,"handover_offset"),
    (0x268,4,"kernel_info_offset"),
]
LOADFLAGS = [(0,"LOADED_HIGH"),(1,"KASLR_FLAG"),(5,"QUIET_FLAG"),
             (6,"KEEP_SEGMENTS"),(7,"CAN_USE_HEAP")]
XLF = [(0,"XLF_KERNEL_64"),(1,"XLF_CAN_BE_LOADED_ABOVE_4G"),
       (2,"XLF_EFI_HANDOVER_32"),(3,"XLF_EFI_HANDOVER_64"),(4,"XLF_EFI_KEXEC"),
       (5,"XLF_5LEVEL"),(6,"XLF_5LEVEL_ENABLED"),(7,"XLF_MEM_ENCRYPTION")]

def main(path):
    b = open(path,"rb").read()
    print(f"file={path} size={len(b)}")
    v = {}
    for off,sz,name in F:
        fmt = {1:"<B",2:"<H",4:"<I",8:"<Q"}[sz]
        val = struct.unpack_from(fmt,b,off)[0]
        v[name]=val
        print(f"  0x{off:03x} {name:<24} = 0x{val:x} ({val})")
    hdr = struct.unpack_from("<4s",b,0x202)[0]
    print(f"\n  magic@0x202 = {hdr!r}  (expect b'HdrS')")
    print(f"  boot protocol version = {v['version']>>8}.{v['version']&0xff:02d}")
    print("  loadflags:", ", ".join(n for i,n in LOADFLAGS if v['loadflags']>>i & 1))
    print("  xloadflags:", ", ".join(n for i,n in XLF if v['xloadflags']>>i & 1))
    print(f"  64-bit entry point = load_addr + 0x200")
    print(f"  protected-mode kernel starts at file offset "
          f"0x{(v['setup_sects'] or 4)+1:x}*512 = "
          f"{((v['setup_sects'] or 4)+1)*512}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv)>1 else "work/nb-x86_64/boot/vmlinuz-virt")

ENTRY(_start)
PHDRS { boot PT_LOAD FLAGS(5); text PT_LOAD FLAGS(5); }
SECTIONS {
  . = 0x0000800040000000;
  .boot : { KEEP(*(.text.boot)) } :boot
  ASSERT(. <= 0x0000800040001000, "boot entry exceeds reserved page")
  . = 0x00008000400a0000;
  .text : { *(.text .text.*) } :text
  .rodata : { *(.rodata .rodata.*) } :text
  .data : { *(.data .data.*) } :text
  .bss (NOLOAD) : { *(.bss .bss.*) *(COMMON) } :text
  ASSERT(. <= 0x00008000400e0000, "resident image exceeds reserved text")
  /DISCARD/ : { *(.eh_frame*) *(.comment*) }
}

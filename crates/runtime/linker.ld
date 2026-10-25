ENTRY(_start)
PHDRS { text PT_LOAD FLAGS(5); }
SECTIONS {
  . = 0x0000800040000000;
  .text : { KEEP(*(.text.boot)) *(.text .text.*) } :text
  .rodata : { *(.rodata .rodata.*) } :text
  .data : { *(.data .data.*) } :text
  .bss (NOLOAD) : { *(.bss .bss.*) *(COMMON) } :text
  ASSERT(. <= 0x0000800040010000, "resident image exceeds reserved text")
  /DISCARD/ : { *(.eh_frame*) *(.comment*) }
}

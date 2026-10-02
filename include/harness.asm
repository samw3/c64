// =====================================================================================
//  harness.asm - C64H scene image layout + runtime macros (KickAssembler 5.x)
//
//  Image (.prg, load address $3B00, one contiguous block):
//    $3B00-$3BFF  Header page   magic, version, flags, entry, mailbox, VIC-II shadow, title
//    $3C00-$3FE7  ColorRam      1000 color RAM nybbles (copied to $D800 by HX_Init)
//    $4000-$7FFF  Bank          VIC bank 1: everything the VIC-II fetches lives here
//    $8000-$8FFF  Code          raster routine, max 4K (segment overflow = assembly error)
//
//  Contract with the capture harness:
//    * Execution starts at the header's entry address (normally the first byte of Code).
//    * When initialization is done (IRQs running), write HX_ST_READY to HX_STATUS
//      (HX_SignalReady). Frames are captured from the first full frame after that.
//    * Optionally HX_FrameTick once per frame; the harness logs HX_FRAME per capture.
//    * Report a fatal problem with HX_SignalError(code); the capture aborts with it.
//
//  Cycle-exact raster IRQs (HX_StableDoubleIrq, HX_StableTimerInit/Irq, HX_Delay) live in
//  stable.asm, which this file imports.
//
//  Keep the constants in sync with harness/image.py (the test suite cross-checks them).
// =====================================================================================
#importonce

.const HX_LOAD      = $3b00
.const HX_HEADER    = $3b00
.const HX_STATUS    = $3b08         // mailbox: 0 booting, 1 ready, $80+n error n
.const HX_FRAME     = $3b09         // optional frame counter (HX_FrameTick)
.const HX_VIC       = $3b40         // VIC-II shadow, $D000-$D02E one-to-one
.const HX_TITLE     = $3b80         // ASCII title, NUL terminated (max 63 chars)
.const HX_COLORRAM  = $3c00
.const HX_BANK      = $4000
.const HX_CODE      = $8000
.const HX_CODE_MAX  = $1000

.const HX_VERSION   = 1
.const HX_FLAG_NTSC = $01
.const HX_ST_BOOT   = $00
.const HX_ST_READY  = $01
.const HX_ST_ERROR  = $80

#import "stable.asm"

.segmentdef Header   [start=HX_HEADER,   min=HX_HEADER,   max=HX_HEADER+$ff, fill]
.segmentdef ColorRam [start=HX_COLORRAM, min=HX_COLORRAM, max=HX_BANK-1,     fill]
.segmentdef Bank     [start=HX_BANK,     min=HX_BANK,     max=HX_BANK+$3fff, fill]
.segmentdef Code     [start=HX_CODE,     max=HX_CODE+HX_CODE_MAX-1]
.segmentdef Upstart  [start=$0801]

// image.prg: what the harness injects.  run.prg: same + BASIC "SYS" stub, autostarts anywhere.
.file [name="image.prg", segments="Header,ColorRam,Bank,Code"]
.file [name="run.prg",   segments="Upstart,Header,ColorRam,Bank,Code"]

// ------------------------------------------------------------------ image content

// Header page: magic, version, flags (HX_FLAG_NTSC), entry point, zeroed mailbox, title.
// Also emits the BASIC upstart for run.prg. Leaves the current segment undefined:
// always follow with a .segment directive.
.macro HX_Header(entry, title, flags) {
    .if (title.size() > 63) .error "HX_Header: title longer than 63 characters"
    .eval hx_cycles = (flags & HX_FLAG_NTSC) != 0 ? 65 : 63     // raster timing for stable.asm
    .segment Header
    * = HX_HEADER "C64H header"
    .encoding "ascii"
    .text "C64H"
    .byte HX_VERSION, flags
    .word entry
    .byte HX_ST_BOOT, 0                 // HX_STATUS, HX_FRAME
    * = HX_TITLE "Title"
    .text title
    .byte 0
    .encoding "screencode_mixed"
    .segment Upstart
    BasicUpstart(entry)
}

// VIC-II shadow ($D000-$D02E). `regs` is a Hashtable keyed by register ($11 or $d011 both
// work). Unlisted registers default to 0, except $D011=$1B and $D016=$C8 (text mode, on).
// $D019/$D01A are not copied by HX_Init (IRQs are enabled by HX_StartIrq).
.macro HX_VicShadow(regs) {
    .segment Header
    * = HX_VIC "VIC-II shadow"
    .for (var r = 0; r < $2f; r++) .byte hx_vicval(regs, r)
}

.function hx_vicval(regs, r) {
    .var v = regs.containsKey(r) ? regs.get(r) : regs.containsKey($d000 + r) ? regs.get($d000 + r) : -1
    .if (v == -1) .return r == $11 ? $1b : r == $16 ? $c8 : 0
    .if (v < 0 || v > 255) .error "HX_VicShadow: value out of range for $d0" + toHexString(r, 2)
    .return v
}

// ------------------------------------------------------------------ runtime

// Bring the machine to a known state and apply the shadows:
// IRQs off, KERNAL/BASIC banked out ($01=$35), CIA interrupts off, NMI/IRQ vectors -> RTI,
// VIC bank 1 ($4000), color RAM and VIC-II registers from the image. Clobbers A, X, SP.
.macro HX_Init() {
    sei
    cld
    ldx #$ff
    txs
    lda #$2f
    sta $00
    lda #$35
    sta $01
    lda #$7f
    sta $dc0d
    sta $dd0d
    lda $dc0d
    lda $dd0d
    lda #<rti_stub
    sta $fffa
    sta $fffe
    lda #>rti_stub
    sta $fffb
    sta $ffff
    jmp !+
rti_stub:
    rti
!:  lda #$3f
    sta $dd02
    lda $dd00
    and #%11111100
    ora #%00000010                  // bank 1: $4000-$7fff
    sta $dd00
    ldx #0
!:  lda HX_COLORRAM+$000,x
    sta $d800,x
    lda HX_COLORRAM+$100,x
    sta $d900,x
    lda HX_COLORRAM+$200,x
    sta $da00,x
    lda HX_COLORRAM+$300,x
    sta $db00,x
    inx
    bne !-
    ldx #$2e
!loop:
    cpx #$19
    beq !skip+
    cpx #$1a
    beq !skip+
    lda HX_VIC,x
    sta $d000,x
!skip:
    dex
    bpl !loop-
    lda #$00
    sta $d01a
    lda #$ff
    sta $d019
}

// Point the IRQ vector at `handler`, arm the raster compare at `line`, enable raster IRQs, CLI.
.macro HX_StartIrq(handler, line) {
    HX_NextIrq(handler, line)
    lda #$01
    sta $d01a
    lda #$ff
    sta $d019
    cli
}

// Chain to the next raster IRQ (use inside a handler). Clobbers A.
.macro HX_NextIrq(handler, line) {
    lda #<handler
    sta $fffe
    lda #>handler
    sta $ffff
    HX_SetRaster(line)
}

// Set the raster compare line (0-311), including the 9th bit in $D011. Clobbers A.
.macro HX_SetRaster(line) {
    lda #<line
    sta $d012
    lda $d011
    .if (line > 255) {
        ora #$80
    } else {
        and #$7f
    }
    sta $d011
}

.macro HX_IrqEnter() {
    pha
    txa
    pha
    tya
    pha
}

// Acknowledge the VIC-II interrupt, restore A/X/Y and return.
.macro HX_IrqExit() {
    lda #$ff
    sta $d019
    pla
    tay
    pla
    tax
    pla
    rti
}

.macro HX_SignalReady() {
    lda #HX_ST_READY
    sta HX_STATUS
}

.macro HX_SignalError(code) {
    .if (code < 0 || code > $7f) .error "HX_SignalError: code must be 0-127"
    lda #HX_ST_ERROR | code
    sta HX_STATUS
}

.macro HX_FrameTick() {
    inc HX_FRAME
}

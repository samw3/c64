// =====================================================================================
//  stable.asm - cycle-exact ("stable") raster interrupts. Imported by harness.asm.
//
//  A raster IRQ normally starts 0-7 cycles late, depending on which instruction it
//  interrupted, so register writes wobble horizontally from frame to frame. Two cures:
//
//  HX_StableDoubleIrq()   Double IRQ: a second raster IRQ fires while the CPU executes
//                         NOPs (0/1 cycle late), then a $D012 compare removes the last cycle.
//                         Self-contained, but busy for the rest of line L and all of L+1.
//                         Lands on line L+2, cycle 3.
//
//  HX_StableTimerInit()   Once, after HX_Init and before HX_StartIrq: locks CIA1 timer A
//  HX_StableTimerIrq()    to the raster. Each IRQ then reads the timer to measure how late
//                         it is and burns the difference (31-38 cycles of handler time).
//                         Lands on line L itself, cycle 47. Owns CIA1 timer A.
//
//  Both replace HX_IrqEnter as the first thing in a raster IRQ handler (they save A/X/Y the
//  same way), so the handler still ends with HX_NextIrq + HX_IrqExit.
//
//  Cycles are 0-based, as VICE's monitor shows them (Bauer's VIC-II article counts from 1).
//  PAL and NTSC (6567R8) land on the same cycles. Visible effect: a color register write
//  (the last cycle of sta) on cycle c shows from frame column x = 8*c - 95 (normal borders).
//  Check any handler's timing with:  make probe SCENE=<name> AT="<label> ..."
//
//  Constraints:
//   * No badline and no sprite DMA on the lines involved (double IRQ: L..L+2, timer: L).
//     Badlines are $30-$F7 where (line & 7) == YSCROLL while the display is on.
//   * Double IRQ: L != 255 and L+2 <= last line (PAL 311, NTSC 262).
//   * Timer IRQ: L != 0, and nothing else may reprogram CIA1 timer A.
//   * Timer init: lines $10-$1A (or $110-$11A) must be free of sprites.
//   * The timer IRQ absorbs 0-7 cycles of lateness: ordinary code, including a 7-cycle
//     read-modify-write right after a taken branch (tested). The double IRQ doesn't care.
//
//  Calibrated against VICE 3.10 x64sc; tests/test_stable.py proves both on PAL and NTSC
//  under a main loop that makes every IRQ arrive with a different jitter.
// =====================================================================================
#importonce

.const HX_DBL_LAND_CYCLE   = 3      // documented landing points (asserted by the tests)
.const HX_TIMER_LAND_CYCLE = 47
.const HX_DBL_EARLY        = 2      // cycles until the NOP-slide IRQ is taken, early case
.const HX_TIMER_PHASE      = 13     // timer start offset: timer & 7 = 7 for the earliest IRQ

// Cycles per raster line for the standard declared in HX_Header (63 PAL, 65 NTSC 6567R8).
.var hx_cycles = 63

// Wait exactly n cycles (n = 0 or n >= 2) with NOP/BIT only. Registers kept; N/V/Z clobbered.
.macro HX_Delay(n) {
    .if (n == 1 || n < 0) .error "HX_Delay: cannot wait exactly " + n + " cycles"
    .if ((n & 1) == 1) {
        bit $ea
        .fill (n - 3) / 2, $ea
    } else {
        .fill n / 2, $ea
    }
}

.macro hx_nopagecross(from, to, what) {
    .errorif (>from) != (>to), what + " crosses a page boundary ($" + toHexString(from, 4) + "): move the handler (e.g. .align $20)"
}

// ------------------------------------------------------------------ double IRQ

.macro HX_StableDoubleIrq() {
    pha                             // save A/X/Y exactly like HX_IrqEnter
    txa
    pha
    tya
    pha
    lda #<stage2
    sta $fffe
    lda #>stage2
    sta $ffff
    inc $d012                       // next raster compare: L+1 (keeps $D011 bit 7)
    asl $d019                       // ack (also clears the IRQ the dummy write may raise)
    tsx                             // stage 2 discards the second IRQ's stack frame
    cli
    .fill 12, $ea                   // the second IRQ lands in here: 0 or 1 cycle late
stage2:
    txs
    asl $d019
    // Wait until the next $D012 read falls on the last cycle of line L+1 (if the IRQ was
    // early) or the first cycle of L+2 (if late), then beq adds the missing cycle.
    .var wait = hx_cycles - 24 - HX_DBL_EARLY
    ldx #floor((wait - 2) / 5)
loop:
    dex
    bne loop
    HX_Delay(wait - 5 * floor((wait - 2) / 5))
    lda $d012
    cmp $d012
    beq !+
!:
    hx_nopagecross(loop, loop + 3, "HX_StableDoubleIrq wait loop")   // bne target vs next instruction
}

// ------------------------------------------------------------------ CIA timer

// Lock CIA1 timer A to the raster: sync to one exact cycle by polling (on lines $10-$1A or
// $110-$11A, whichever comes first), then start the timer with a period of one raster line.
// IRQs must be off. Busy-waits for up to ~12 raster lines.
.macro HX_StableTimerInit() {
    lda #$00
    sta $dc0e                       // stop timer A
    lda #<(hx_cycles - 1)
    sta $dc04
    lda #>(hx_cycles - 1)
    sta $dc05
    ldx #$0f
!:  cpx $d012                       // wait for line $0f ...
    bne !-
    inx
!:  cpx $d012                       // ... then for the start of line $10 (within 8 cycles)
    bne !-
    nop
sync:                               // each pass reads $D012 one cycle earlier in the line,
    inx                             // until the read lands on the previous line's last cycle
    .var pass = hx_cycles - 11      // padding per pass: read-to-read = one line minus one cycle
    ldy #floor((pass - 2) / 5)
dly:
    dey
    bne dly
    HX_Delay(pass - 5 * floor((pass - 2) / 5))
    cpx $d012
    beq sync
synced:                             // exactly at cycle 2 of a known line
    HX_Delay(HX_TIMER_PHASE)
    lda #%00010001                  // start, continuous, force load
    sta $dc0e
    hx_nopagecross(sync, synced, "HX_StableTimerInit sync loop")
}

.macro HX_StableTimerIrq() {
    pha
    lda $dc04                       // timer low byte: one less per cycle of IRQ lateness
    lsr                             // burn (timer & 7) cycles: +1, +2, +4 per bit
    bcs !+
!:  lsr
    bcc b1
    bit $ea
b1: lsr
    bcc b2
    nop
    bit $ea
b2: txa
    pha
    tya
    pha
    hx_nopagecross(b1 - 2, b2, "HX_StableTimerIrq")
}

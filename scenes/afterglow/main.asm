// Afterglow: a mountain lake at twilight (see gfx.py for the painting).
//
// One stable raster IRQ per frame (CIA timer, line 49) runs a cycle-exact kernel down the
// display, to the black bank of the near shore. On every line it writes $D021 (background) and
// $D016 (xscroll) in the right border, so each line has its own background color and its own
// horizontal offset:
//   sky     the per-line background carries the gradient bands, so the three cell colors
//           are free for the moon, clouds and mountains
//   lake    animated: ripple lines light the black water, xscroll makes the reflection wobble
// After the kernel (lower border) the next frame's lake lines and star colors are copied in
// from a 64-frame loop of records in the VIC bank.
//
// Kernel row (8 raster lines, badline second), rows 0-20 in a loop:
//   line 50+8r      loop control (cycles 0-9), load next values, wait, store at 55-62
//   line 51+8r      badline: load (0-9), the VIC-II stalls the store until cycle 54
//   lines 52-57+8r  load (0-9), wait, store at 55-62
// From line 218 the kernel is unrolled, because the reeds in the foreground are sprites 3-7:
// they stand still while the lake wobbles behind them. Their data fetches stall the CPU from
// cycle 60 to cycle 9 of the next line, so on those lines the stores move to 55 and 59 and
// every line restarts on cycle 10. A badline under the sprites leaves the CPU cycles 10 and
// 54-59 only: the line before preloads X for its xscroll, and the line after a badline keeps
// the badline's background (gfx.py plans for that).
#import "harness.asm"
#import "gfx.asm"

.const KERNEL_LINE = 49             // stable timer IRQ; the kernel starts on line 50
.const D016_BASE   = $10 | 4        // multicolor, 38 columns (hides the xscroll edge), xscroll 4
.const BGK = $9000                  // $D021 for window lines 0-199 (raster 51-250)
.const XSK = $9100                  // $D016 for window lines 0-199
.label rows  = $02                  // kernel row counter
.label frame = $03                  // animation frame 0-63
.label rec   = $04                  // pointer to this frame's record (2 bytes)

.const LOOP_ROWS   = 21             // rows 0-20 (lines 50-217), then the unrolled section
.const SECTION     = 50 + 8 * LOOP_ROWS
.const LAST        = 51 + GFX_BANK_Y - 1   // sets up the first line of the black bank

HX_Header(start, "afterglow", 0)
.var vic = Hashtable().put(
    $d011, $3b,                     // bitmap mode, display on, 25 rows, yscroll 3
    $d016, D016_BASE,
    $d018, GFX_D018,
    $d020, GFX_BORDER,
    $d021, GFX_BG,
    $d015, %11111000,               // sprites 3-7: the reeds (hires, black, in front)
    $d010, GFX_SPR_MSB)
.for (var i = 0; i < 5; i++) {
    .eval vic.put($d006 + 2 * i, GFX_SPR_X.get(i))
    .eval vic.put($d007 + 2 * i, GFX_SPR_Y)
}
HX_VicShadow(vic)

.segment ColorRam
    .import binary "colorram.bin"
.segment Bank
    .import binary "bank.bin"

.segment Code
start:
    HX_Init()
    ldx #0
!:  lda GFX_BGTAB,x
    sta BGK,x
    lda #D016_BASE
    sta XSK,x
    inx
    cpx #200
    bne !-
    lda #0
    sta frame
    lda #<GFX_ANIM
    sta rec
    lda #>GFX_ANIM
    sta rec+1
    jsr animate                     // frame 0's lake and stars before the first frame
    HX_StableTimerInit()
    HX_StartIrq(kernel_irq, KERNEL_LINE)
    HX_SignalReady()
    jmp *

// Copy the current record into the kernel tables and color RAM, then advance to the next.
animate:
    ldy #GFX_WATER_LINES - 1
!:  lda (rec),y
    tax
    and #$07
    ora #$10
    sta XSK + GFX_WATER_Y,y
    txa
    lsr
    lsr
    lsr
    lsr
    sta BGK + GFX_WATER_Y,y
    dey
    bpl !-
    .for (var i = 0; i < GFX_STARS; i++) {
        ldy #GFX_WATER_LINES + i
        lda (rec),y
        sta GFX_STAR_HI.get(i) * 256 + GFX_STAR_LO.get(i)
    }
    inc frame
    lda frame
    cmp #GFX_ANIM_FRAMES
    bne !+
    lda #0
    sta frame
    lda #<GFX_ANIM
    sta rec
    lda #>GFX_ANIM
    sta rec+1
    rts
!:  lda rec
    clc
    adc #GFX_ANIM_RECORD
    sta rec
    bcc !+
    inc rec+1
!:  rts

// One raster line: load the next line's values, wait, store them in the right border.
.macro Line(delay) {
    lda BGK,x
    ldy XSK,x
    inx
    HX_Delay(delay)
    sta $d021                       // cycle 58 (normal lines)
    sty $d016                       // cycle 62
}

// Raster line n: a badline (yscroll 3)? Are sprites 3-7 fetched at cycles 0-9 of it?
.function badline(n) { .return n >= $30 && n <= $f7 && (n & 7) == 3 }
.function reeds(n) { .return n > GFX_SPR_Y && n <= GFX_SPR_Y + 21 }
.function ix(n) { .return n - 51 }  // BGK/XSK index of raster line n

// Lines first..last, unrolled; each sets up the next line. Enters on cycle 8 of `first`.
.macro Section(first, last) {
    .var s = 8                                  // cycle the current line's code starts on
    .for (var n = first; n <= last; n++) {
        .var stalls = reeds(n + 1)              // sprite fetches from cycle 60 on
        .if (badline(n) && reeds(n)) {
            .errorif s != 10, "badline under sprites must start on cycle 10"
            stx $d016                           // xscroll of n+1, stalled until 54: lands on 56
            lda BGK + ix(n + 2)                 // background of n+2, completes on the next line's cycle 10
            .eval s = 11
        } else .if (badline(n)) {
            .errorif s != 0, "badline must start on cycle 0"
            lda BGK + ix(n + 1)
            ldy XSK + ix(n + 1)
            HX_Delay(2)
            sta $d021                           // stalled until 54: lands on 56
            sty $d016                           // 60
            .if (stalls) {
                .eval s = 10
            } else {
                nop
                .eval s = 0
            }
        } else {
            .var after = badline(n - 1) && reeds(n - 1)     // A already holds our background
            .var before = badline(n + 1) && reeds(n + 1)    // preload X for the badline
            .if (!after) lda BGK + ix(n + 1)
            ldy XSK + ix(n + 1)
            .if (before) ldx XSK + ix(n + 2)
            HX_Delay((stalls ? 52 : 55) - s - (after ? 4 : 8) - (before ? 4 : 0))
            sta $d021                           // 55 (58 without sprites)
            sty $d016                           // 59 (62)
            .eval s = stalls ? 10 : 0
        }
    }
}

    .align $100
kernel_irq:
    HX_StableTimerIrq()             // line 49, cycle 47
    lda #LOOP_ROWS
    sta rows
    ldx #0
    HX_Delay(19)
row:                                // line 50+8r, cycle 10
    Line(35)
    lda BGK,x                       // line 51+8r: badline
    ldy XSK,x
    inx
    sta $d021                       // stalled by the VIC-II, lands on cycle 56
    sty $d016
    nop
    Line(45)
    Line(45)
    Line(45)
    Line(45)
    Line(45)
    Line(45)
    dec rows                        // line 58+8r, cycles 0-9
    beq section
    jmp row
section:                            // line 218, cycle 8
    Section(SECTION, LAST)
done:                               // line 238, cycle 10 (after the reeds' fetch): the bank
    jsr animate
animated:
    HX_FrameTick()
    HX_NextIrq(kernel_irq, KERNEL_LINE)
    HX_IrqExit()

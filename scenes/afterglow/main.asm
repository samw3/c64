// Afterglow: a mountain lake at twilight (see gfx.py for the painting).
//
// One stable raster IRQ per frame (CIA timer, line 49) runs a cycle-exact kernel over the
// 200 display lines. On every line it writes $D021 (background) and $D016 (xscroll) in the
// right border, so each line has its own background color and its own horizontal offset:
//   sky     the per-line background carries the gradient bands, so the three cell colors
//           are free for the moon, clouds and mountains
//   lake    animated: ripple lines light the black water, xscroll makes the reflection wobble
// After the kernel (lower border) the next frame's lake lines and star colors are copied in
// from a 64-frame loop of records in the VIC bank.
//
// Kernel row (8 raster lines, badline second):
//   line 50+8r      loop control (cycles 0-9), load next values, wait, store at 55-62
//   line 51+8r      badline: load (0-9), the VIC-II stalls the store until cycle 54
//   lines 52-57+8r  load (0-9), wait, store at 55-62
#import "harness.asm"
#import "gfx.asm"

.const KERNEL_LINE = 49             // stable timer IRQ; the kernel starts on line 50
.const D016_BASE   = $10 | 4        // multicolor, 38 columns (hides the xscroll edge), xscroll 4
.const BGK = $9000                  // $D021 for window lines 0-199 (raster 51-250)
.const XSK = $9100                  // $D016 for window lines 0-199
.label rows  = $02                  // kernel row counter
.label frame = $03                  // animation frame 0-63
.label rec   = $04                  // pointer to this frame's record (2 bytes)

HX_Header(start, "afterglow", 0)
HX_VicShadow(Hashtable().put(
    $d011, $3b,                     // bitmap mode, display on, 25 rows, yscroll 3
    $d016, D016_BASE,
    $d018, GFX_D018,
    $d020, GFX_BORDER,
    $d021, GFX_BG))

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

    .align $100
kernel_irq:
    HX_StableTimerIrq()             // line 49, cycle 47
    lda #25
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
    beq done
    jmp row
done:                               // line 250, cycle 8: lower border
    jsr animate
animated:
    HX_FrameTick()
    HX_NextIrq(kernel_irq, KERNEL_LINE)
    HX_IrqExit()

// Testcard: multicolor bitmap + raster IRQ chain + moving sprite (see gfx.py for the layout).
//
// IRQ chain (raster lines, PAL):
//   $010  border = luma ramp[HX_FRAME & 15]          top band (visible from line 16)
//   $024  border = GFX_BORDER                         rest of the frame
//   $0fc  border = luma ramp[(HX_FRAME + 8) & 15]     bottom band; move sprite 0, HX_FrameTick
#import "harness.asm"
#import "gfx.asm"

.const SPRITE_X0 = 40
.const SPRITE_Y  = 50 + 13 * 8 + 2
.label sx = $02                 // 16-bit sprite x (zero page is free: KERNAL is banked out)

HX_Header(start, "testcard", 0)
HX_VicShadow(Hashtable().put(
    $d011, $3b,                 // bitmap mode, display on, 25 rows, yscroll 3
    $d016, $18,                 // multicolor, 40 columns
    $d018, GFX_D018,
    $d020, GFX_BORDER,
    $d021, GFX_BG,
    $d015, %00000001,           // sprite 0 on
    $d000, SPRITE_X0,
    $d001, SPRITE_Y,
    $d027, 7))                  // yellow

.segment ColorRam
    .import binary "colorram.bin"
.segment Bank
    .import binary "bank.bin"

.segment Code
start:
    HX_Init()
    lda #<SPRITE_X0
    sta sx
    lda #>SPRITE_X0
    sta sx+1
    HX_StartIrq(irq_top, $010)
    HX_SignalReady()
    jmp *

irq_top:
    HX_IrqEnter()
    lda HX_FRAME
    and #$0f
    tax
    lda luma,x
    sta $d020
    HX_NextIrq(irq_mid, $024)
    HX_IrqExit()

irq_mid:
    HX_IrqEnter()
    lda #GFX_BORDER
    sta $d020
    HX_NextIrq(irq_bottom, $0fc)
    HX_IrqExit()

irq_bottom:
    HX_IrqEnter()
    lda HX_FRAME
    clc
    adc #8
    and #$0f
    tax
    lda luma,x
    sta $d020
    // sprite 0 x += 1, wrapping at 344 (off the right edge)
    inc sx
    bne !+
    inc sx+1
!:  lda sx+1
    beq !set+
    lda sx
    cmp #<344
    bcc !set+
    lda #0
    sta sx
    sta sx+1
!set:
    lda sx
    sta $d000
    lda sx+1
    sta $d010
    HX_FrameTick()
    HX_NextIrq(irq_top, $010)
    HX_IrqExit()

luma:
    .fill 16, GFX_LUMA.get(i)

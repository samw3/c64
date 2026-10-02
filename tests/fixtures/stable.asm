// Stable raster test. The main loop runs 7-cycle instructions and every handler returns
// 0-7 cycles late (varying), so each raster IRQ arrives with a different jitter (a stable
// handler exits on a fixed cycle, which would otherwise freeze the next IRQ's jitter).
// The display is off (no badlines re-synchronizing the CPU), so the whole frame is border.
// Two border splits must nevertheless land on the same cycle every frame:
//   irq_a  line $50  HX_StableDoubleIrq -> line $52: border red
//   irq_b  line $90  HX_StableTimerIrq  -> line $90: border green
//   irq_c  line $f9  plain IRQ: border back to black, frame tick
// Assemble with -define NTSC for the NTSC variant.
#import "harness.asm"

#if NTSC
.const FLAGS = HX_FLAG_NTSC
#else
.const FLAGS = 0
#endif
.const A_DELAY = 22              // cycles between landing and the red write
.label salt = $20

// Burn 0-7 cycles, a different amount on every call (salt advances 3x per frame).
.macro Scramble() {
    inc salt
    lda salt
    lsr
    bcs !+
!:  lsr
    bcc !+
    bit $ea
!:  lsr
    bcc !+
    nop
    bit $ea
!:
}

HX_Header(start, "stable", FLAGS)
HX_VicShadow(Hashtable().put(
    $d011, $0b,                  // display off: no badlines, every pixel is border
    $d020, 0))

.segment Code
start:
    HX_Init()
    HX_StableTimerInit()
    HX_StartIrq(irq_a, $50)
    HX_SignalReady()
main:
    ldx #0
!:  .for (var i = 0; i < 16; i++) inc $0300,x     // 7 cycles each
    jmp !-

    .align $40
irq_a:
    HX_StableDoubleIrq()
a_stable:
    HX_Delay(A_DELAY)
    lda #2
    sta $d020
    HX_NextIrq(irq_b, $90)
    Scramble()
    HX_IrqExit()

    .align $40
irq_b:
    HX_StableTimerIrq()
b_stable:
    lda #5
    sta $d020
    HX_NextIrq(irq_c, $f9)
    Scramble()
    HX_IrqExit()

irq_c:
    HX_IrqEnter()
    lda #0
    sta $d020
    HX_FrameTick()
    HX_NextIrq(irq_a, $50)
    Scramble()
    HX_IrqExit()

// Signals READY, then executes a JAM opcode a few frames later: the harness must report the JAM.
#import "harness.asm"
HX_Header(start, "jam", 0)
HX_VicShadow(Hashtable().put($d020, 2))
.segment Code
start:
    HX_Init()
    HX_SignalReady()
    ldx #3                  // let three frames go by
!wait:
    lda #$80
!:  cmp $d012
    bne !-
!:  cmp $d012
    beq !-
    dex
    bne !wait-
crash:
    .byte $02               // JAM

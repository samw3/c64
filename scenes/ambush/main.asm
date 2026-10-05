// Ambush: a fantasy hero fights five creatures in a forest clearing (see gfx.py).
//
// Sprites: 0 the hero's hires black overlay, 1 his multicolor body (same position: sprite 0
// draws crisp hires outlines over sprite 1's fills), 2 his hires sword (behind him, so his
// fist covers the grip), 3-7 five multicolor creatures.
//
// Sprite streaming: gfx.py stores each frame once, facing right. At init the routine unpacks
// them into a store at $9000 (64 bytes each), mirroring the copies the timeline needs facing
// left. Every hardware sprite owns two slots at $4000; when its frame changes, the new frame
// is copied into the slot that isn't on screen and the pointer flips in the lower border.
//
// The timeline is one byte stream per channel (gfx.py encodes them):
//   0ccc dddd            short run: ccc+1 frames of the signed nybble dddd
//   10cc cccc  vvvvvvvv  long run: cccccc+1 frames of the byte v
//   11ll llll  oooooooo  replay llllll+1 earlier tokens starting o bytes back, then continue
// Channels: X and Y per actor (as per-frame deltas), frame per actor, color per hardware
// sprite, then $D015, $D01B, the scroll registers (screen shake), and one "poke" channel per
// ambient sparkle: a byte the IRQ writes to its screen or color RAM cell.
//
// Frame flow: the IRQ in the lower border (line 250) applies the shadow registers the main
// loop prepared, then the main loop prepares the next frame while this one is displayed.
// READY is signalled from the IRQ that applies frame 0, so captured frame k is timeline
// frame k (mod the loop).
#import "harness.asm"
#import "gfx.asm"

.const IRQ_LINE  = 250              // lower border: the 24-row display ends on line 246
.const STORE     = $9000            // up to 255 sprite frames, unpacked at init
.const REV_HIRES = $0200            // bit-reversal table (mirroring hires sprites)
.const REV_MC    = $0300            // pair-reversal table (mirroring multicolor sprites)
.const PTRS      = GFX_SCREEN + $3f8

.const CH = $0400                   // channel state, TL_NCH bytes each
.label ch_lo   = CH                 // read pointer
.label ch_hi   = CH + TL_NCH
.label ch_cnt  = CH + 2 * TL_NCH    // frames left in the current run
.label ch_val  = CH + 3 * TL_NCH    // the run's value (positions: delta per frame)
.label ch_left = CH + 4 * TL_NCH    // tokens left to replay (0: reading the stream itself)
.label ch_rlo  = CH + 5 * TL_NCH    // where to continue after the replay
.label ch_rhi  = CH + 6 * TL_NCH

.label src    = $02                 // 2: source pointer
.label dst    = $04                 // 2: destination pointer
.label tmp    = $06
.label idx    = $07
.label go     = $08                 // IRQ -> main loop: the shadow was applied
.label ready  = $09                 // main loop -> IRQ: the next shadow is complete
.label live   = $0a                 // set once frame 0 is on screen
.label tick   = $0b                 // 2: frame within the loop
.label xlo    = $10                 // 7 actors: X (9 bits), Y
.label xhi    = $18
.label ypos   = $20
.label cur    = $28                 // 8: store frame each hardware sprite shows
.label buf    = $30                 // 8: which of its two slots holds it
.label sh     = $38                 // shadow: 16 X/Y bytes in VIC order
.label sh_msb = $48
.label sh_en  = $49
.label sh_pr  = $4a
.label sh_d011 = $4b
.label sh_d016 = $4c
.label sh_col = $50                 // 8
.label sh_ptr = $58                 // 8

HX_Header(start, "ambush", 0)
HX_VicShadow(Hashtable().put(
    $d011, GFX_D011,                // bitmap, display on, 24 rows, yscroll 3
    $d016, GFX_D016,                // multicolor, 38 columns, xscroll 4
    $d018, GFX_D018,
    $d020, GFX_BORDER,
    $d021, GFX_BG,
    $d01c, %11111010,               // multicolor: hero body, creatures
    $d025, GFX_D025,
    $d026, GFX_D026))

.segment ColorRam
    .import binary "colorram.bin"
.segment Bank
    .import binary "bank.bin"

.segment Code
start:
    HX_Init()
    jsr make_luts
    jsr build_store
    ldx #7
!:  lda #$ff                        // no frame in any slot yet
    sta cur,x
    lda #1                          // the first copy goes to slot 0
    sta buf,x
    dex
    bpl !-
    jsr reset_timeline
    jsr next_frame                  // frame 0 into the shadow
    lda #1
    sta ready
    lda #0
    sta go
    sta live
    HX_StartIrq(irq, IRQ_LINE)
main:
    lda go
    beq main
    lda #0
    sta go
    jsr next_frame
    lda #1
    sta ready
    jmp main

// ------------------------------------------------------------------ lower border
irq:
    HX_IrqEnter()
    lda ready
    beq !skip+
    ldx #15
!:  lda sh,x
    sta $d000,x
    dex
    bpl !-
    lda sh_msb
    sta $d010
    ldx #7
!:  lda sh_col,x
    sta $d027,x
    lda sh_ptr,x
    sta PTRS,x
    dex
    bpl !-
    lda sh_en
    sta $d015
    lda sh_pr
    sta $d01b
    lda sh_d011                     // raster compare bit 8 stays 0 (line 250)
    sta $d011
    lda sh_d016
    sta $d016
    .if (TL_POKES > 0) {
        ldx #TL_POKES - 1           // ambient: sparkles in screen and color RAM
    !:  lda poke_lo,x
        sta dst
        lda poke_hi,x
        sta dst+1
        lda ch_val + TL_CH_POKE,x
        ldy #0
        sta (dst),y
        dex
        bpl !-
    }
    lda #0
    sta ready
    lda #1
    sta go
    lda live
    bne !skip+
    inc live
    HX_SignalReady()                // frame 0 shows from the next frame on
!skip:
    HX_FrameTick()
    HX_IrqExit()

// ------------------------------------------------------------------ one frame of the timeline
next_frame:
    ldx #TL_NCH - 1                 // advance every channel by one frame
dec_loop:
    lda ch_cnt,x
    bne dec_have
    jsr fetch
dec_have:
    dec ch_cnt,x
    dex
    bpl dec_loop

    ldx #TL_ACTORS - 1              // positions += deltas
pos_loop:
    lda ch_val + TL_CH_X,x
    bmi pos_neg
    clc
    adc xlo,x
    sta xlo,x
    bcc pos_y
    inc xhi,x
    bcs pos_y
pos_neg:
    clc
    adc xlo,x
    sta xlo,x
    bcs pos_y
    dec xhi,x
pos_y:
    lda ch_val + TL_CH_Y,x
    clc
    adc ypos,x
    sta ypos,x
    dex
    bpl pos_loop

    ldx #7                          // frames: copy the ones that changed
frm_loop:
    ldy hw_fch,x
    lda ch_val,y
    clc
    adc hw_add,x
    cmp cur,x
    beq frm_next
    sta cur,x
    jsr copy_frame
frm_next:
    dex
    bpl frm_loop

    lda #0                          // shadow registers
    sta sh_msb
    ldx #7
sh_loop:
    ldy hw_actor,x
    lda xhi,y
    lsr
    rol sh_msb
    lda xlo,y
    pha
    lda ypos,y
    ldy vic2,x
    sta sh+1,y
    pla
    sta sh,y
    lda ch_val + TL_CH_COL,x
    sta sh_col,x
    dex
    bpl sh_loop
    lda ch_val + TL_CH_EN
    sta sh_en
    lda ch_val + TL_CH_PR
    sta sh_pr
    lda ch_val + TL_CH_SCROLL
    and #$07
    ora #$10
    sta sh_d016
    lda ch_val + TL_CH_SCROLL
    lsr
    lsr
    lsr
    lsr
    ora #$30
    sta sh_d011

    inc tick                        // end of the loop: start over
    bne !+
    inc tick+1
!:  lda tick
    cmp #<TL_LOOP
    bne nf_done
    lda tick+1
    cmp #>TL_LOOP
    bne nf_done
    jsr reset_timeline
nf_done:
    rts

// Read channel X's next token into ch_cnt/ch_val.
fetch:
    lda ch_lo,x
    sta src
    lda ch_hi,x
    sta src+1
    ldy #0
    lda ch_left,x
    bne f_token                     // replaying: only plain runs here
    lda (src),y
    cmp #$c0
    bcc f_token
    and #$3f                        // replay: remember where to continue, jump back
    adc #0                          // carry is set: +1
    sta ch_left,x
    lda src
    clc
    adc #2
    sta ch_rlo,x
    lda src+1
    adc #0
    sta ch_rhi,x
    iny
    lda src
    sec
    sbc (src),y
    sta tmp
    lda src+1
    sbc #0
    sta src+1
    lda tmp
    sta src
    dey
f_token:
    lda (src),y
    bmi f_long
    lsr                             // short run: ccc+1 frames, signed nybble
    lsr
    lsr
    lsr
    clc
    adc #1
    sta ch_cnt,x
    lda (src),y
    and #$0f
    cmp #$08
    bcc !+
    ora #$f0
!:  sta ch_val,x
    lda #1
    bne f_next
f_long:
    and #$3f
    clc
    adc #1
    sta ch_cnt,x
    iny
    lda (src),y
    sta ch_val,x
    lda #2
f_next:
    clc
    adc src
    sta ch_lo,x
    lda src+1
    adc #0
    sta ch_hi,x
    lda ch_left,x
    beq !+
    dec ch_left,x
    bne !+
    lda ch_rlo,x                    // replay done
    sta ch_lo,x
    lda ch_rhi,x
    sta ch_hi,x
!:  rts

// Copy store frame A into the free slot of hardware sprite X and point the shadow at it.
copy_frame:
    tay
    lsr
    lsr
    clc
    adc #>STORE
    sta cp_src + 2
    tya
    and #3
    tay
    lda lo64,y
    sta cp_src + 1
    lda buf,x
    eor #1
    sta buf,x
    txa
    asl
    ora buf,x                       // slot = 2 * sprite + buffer
    sta sh_ptr,x                    // slots start at the bank's first byte: pointer = slot
    tay
    lsr
    lsr
    clc
    adc #>GFX_SLOTS
    sta cp_dst + 2
    tya
    and #3
    tay
    lda lo64,y
    sta cp_dst + 1
    ldy #62
cp_loop:
cp_src:
    lda $ffff,y
cp_dst:
    sta $ffff,y
    dey
    bpl cp_loop
    rts

reset_timeline:
    ldx #TL_NCH - 1
!:  lda tl_start_lo,x
    sta ch_lo,x
    lda tl_start_hi,x
    sta ch_hi,x
    lda #0
    sta ch_cnt,x
    sta ch_left,x
    dex
    bpl !-
    ldx #TL_ACTORS - 1
!:  lda tl_x0lo,x
    sta xlo,x
    lda tl_x0hi,x
    sta xhi,x
    lda tl_y0,x
    sta ypos,x
    dex
    bpl !-
    lda #0
    sta tick
    sta tick+1
    rts

// ------------------------------------------------------------------ init: the sprite store
make_luts:
    ldx #0
!loop:
    stx tmp
    lda #0
    ldy #8
!:  lsr tmp
    rol
    dey
    bne !-
    sta REV_HIRES,x
    and #$aa                        // multicolor: swap the bits back within each pair
    lsr
    sta tmp
    lda REV_HIRES,x
    and #$55
    asl
    ora tmp
    sta REV_MC,x
    inx
    bne !loop-
    rts

build_store:
    ldx #0
bs_loop:
    stx idx
    lda store_src,x                 // src = sources + 64 * index
    tay
    lsr
    lsr
    clc
    adc #>GFX_SOURCES
    sta src+1
    tya
    and #3
    tay
    lda lo64,y
    sta src
    txa                             // dst = store + 64 * n
    lsr
    lsr
    clc
    adc #>STORE
    sta dst+1
    txa
    and #3
    tay
    lda lo64,y
    sta dst
    lda store_mode,x
    beq bs_copy
    cmp #1
    beq bs_hires
    jsr mirror_mc
    jmp bs_next
bs_hires:
    jsr mirror_hires
    jmp bs_next
bs_copy:
    ldy #62
!:  lda (src),y
    sta (dst),y
    dey
    bpl !-
bs_next:
    ldx idx
    inx
    cpx #TL_STORE
    bne bs_loop
    rts

// Mirror a sprite: each row's three bytes in reverse order, each byte reversed by `lut`.
.macro Mirror(lut) {
    ldy #0
row:
    iny
    iny
    lda (src),y
    tax
    lda lut,x
    dey
    dey
    sta (dst),y
    iny
    lda (src),y
    tax
    lda lut,x
    sta (dst),y
    dey
    lda (src),y
    tax
    lda lut,x
    iny
    iny
    sta (dst),y
    iny
    cpy #63
    bne row
    rts
}
mirror_hires:
    Mirror(REV_HIRES)
mirror_mc:
    Mirror(REV_MC)

lo64:     .byte $00, $40, $80, $c0
hw_actor: .byte 0, 0, 1, 2, 3, 4, 5, 6
hw_fch:   .byte TL_CH_F, TL_CH_F, TL_CH_F + 1, TL_CH_F + 2, TL_CH_F + 3, TL_CH_F + 4, TL_CH_F + 5, TL_CH_F + 6
hw_add:   .byte 0, 1, 0, 0, 0, 0, 0, 0
vic2:     .byte 0, 2, 4, 6, 8, 10, 12, 14

    #import "timeline.asm"

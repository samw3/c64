// Never signals READY: the harness must time out and report where the CPU is spinning.
#import "harness.asm"
HX_Header(start, "noready", 0)
HX_VicShadow(Hashtable())
.segment Code
start:
    HX_Init()
spin:
    jmp spin

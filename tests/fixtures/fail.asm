// Reports an error through the mailbox instead of READY.
#import "harness.asm"
HX_Header(start, "fail", 0)
HX_VicShadow(Hashtable())
.segment Code
start:
    HX_Init()
    HX_SignalError(5)
    jmp *

# C64 graphics harness
#   make setup                         build headless VICE + fetch KickAssembler (once)
#   make SCENE=testcard                assemble build/<scene>/image.prg (+ run.prg)
#   make capture SCENE=x FRAMES=8      run headless, write frames to out/<scene>/ (CRT=1: + crt/ view)
#   make crt SCENE=x                   CRT view of the last capture (out/<scene>/crt/)
#   make inspect SCENE=x               decode the image's VIC-II setup
#   make probe SCENE=x AT="lbl ..."    raster line/cycle where code labels run (jitter check)
#   make new SCENE=x                   start a scene from scenes/_template
#   make test                          end-to-end harness tests
#   make showcase                      README images from a fresh capture of afterglow (needs ffmpeg)

SCENE  ?= testcard
FRAMES ?= 4
SKIP   ?= 0
EVERY  ?= 1
ARGS   ?=
PY     ?= python3
JAVA   ?= java

ROOT     := $(abspath .)
KICKASS  := $(JAVA) -jar $(ROOT)/tools/kickass/KickAss.jar
SCENEDIR := scenes/$(SCENE)
BUILD    := $(ROOT)/build/$(SCENE)
OUT      := out/$(SCENE)
IMAGE    := $(BUILD)/image.prg

export PYTHONPATH := $(ROOT)

.PHONY: all image capture crt inspect probe new test showcase setup clean
all: image
image: $(IMAGE)

$(IMAGE): $(wildcard $(SCENEDIR)/*) $(wildcard include/*.asm) $(wildcard harness/*.py)
	@test -f $(SCENEDIR)/main.asm || { echo "no scene at $(SCENEDIR) (try: make new SCENE=$(SCENE))"; exit 1; }
	@test -f tools/kickass/KickAss.jar || { echo "KickAssembler missing: run make setup"; exit 1; }
	@mkdir -p $(BUILD)
	@if [ -f $(SCENEDIR)/gfx.py ]; then echo "gfx: $(SCENEDIR)/gfx.py"; $(PY) $(SCENEDIR)/gfx.py $(BUILD); fi
	@$(KICKASS) $(SCENEDIR)/main.asm -odir $(BUILD) -libdir $(ROOT)/include -libdir $(BUILD) \
		-vicesymbols -showmem -symbolfile > $(BUILD)/kickass.log 2>&1 \
		|| { grep -v '^\s*$$' $(BUILD)/kickass.log | tail -25; rm -f $(IMAGE); exit 1; }
	@sed -n '/^Memory Map/,/^Writing/p' $(BUILD)/kickass.log | grep -E '^\s+\$$|segment' || true
	@echo "built $(IMAGE)"

capture: image
	$(PY) -m harness capture $(IMAGE) --frames $(FRAMES) --skip $(SKIP) --every $(EVERY) --out $(OUT) \
		$(if $(CRT),--crt) $(ARGS)

crt:
	@$(PY) -m harness crt $(OUT) $(ARGS)

inspect: image
	@$(PY) -m harness inspect $(IMAGE)

probe: image
	@test -n "$(AT)" || { echo 'usage: make probe SCENE=x AT="label ..."'; exit 1; }
	@$(PY) -m harness probe $(IMAGE) $(foreach a,$(AT),--at $(a)) $(ARGS)

new:
	@test ! -e $(SCENEDIR) || { echo "$(SCENEDIR) already exists"; exit 1; }
	@cp -R scenes/_template $(SCENEDIR)
	@sed -i '' 's/"template"/"$(SCENE)"/' $(SCENEDIR)/main.asm
	@echo "created $(SCENEDIR): edit gfx.py / main.asm, then make capture SCENE=$(SCENE)"

test:
	$(PY) -m unittest discover -s tests -v

# CRT view: one palette for the whole loop, ordered dither (no shimmer between frames), only
# the changed rectangle per frame. Exact frames: the indexed 2x copies keep the C64 palette.
showcase:
	$(MAKE) capture SCENE=afterglow FRAMES=64 CRT=1
	ffmpeg -v error -y -framerate 50 -i out/afterglow/crt/%03d.png -loop 0 \
		-vf "split[a][b];[a]palettegen=stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
		docs/afterglow-crt.gif
	ffmpeg -v error -y -framerate 50 -i out/afterglow/zoom/%03d.png -loop 0 docs/afterglow.gif

setup:
	tools/setup.sh

clean:
	rm -rf build out

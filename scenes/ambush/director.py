"""The choreography: every sprite's state on every frame of the loop.

Coordinates are hires bitmap pixels (x 0..319, y 0..199) of a sprite's 24x21 box, top-left;
gfx.py turns them into register values (and adds the screen shake). Facing is +1 (right, as
drawn) or -1 (mirrored). Ground creatures and the hero stand on row 20 of their box.

The fight, in duels, while the others menace (L = 512 frames, 10.24 s):
    0   standoff: the wolf snarls on the right, the imp hops and taunts on the left, the bat
        circles, the wisp drifts
   40   the wolf charges and leaps; the hero cuts it out of the air (hit 81)
  118   the hero turns; the imp hops in and leaps with its pitchfork; cut down (135), it bursts
  150   the spider creeps in on the right and lurks
  187   the bat dives; a rising slash from a crouch knocks it out of the sky (200)
  250   the hero runs at the spider as it rears and cuts it (284)
  300   the wolf and the imp come back
  330   the wisp swoops and burns the hero (356); he answers and bursts it (396)
  434   victory pose while the bat and the wisp return for the next round
Every hit freezes hero and victim for STOP frames (hit-stop): the victim flashes white, the
blade flashes gold with an impact burst, and the screen shakes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Sword effects beyond the plain blades: swing smears (trail -> lead angle) and impacts (the
# blade stopped at an angle with a flash `at` pixels out), shown during hit-stop.
SWORD_FX = {
    "smear_slash": ("smear", 100, 0),
    "smear_rise": ("smear", 0, 90),
    "impact_slash": ("impact", 0, 11),
    "impact_rise": ("impact", 90, 11),
}

BADDIE_FRAMES = [
    "wolf_run0", "wolf_run1", "wolf_run2", "wolf_run3", "wolf_snarl0", "wolf_snarl1", "wolf_leap", "wolf_hurt",
    "bat_fly0", "bat_fly1", "bat_fly2", "bat_fly3", "bat_dive", "bat_hurt",
    "spider_walk0", "spider_walk1", "spider_walk2", "spider_walk3", "spider_rear", "spider_hurt",
    "imp_idle0", "imp_idle1", "imp_leap", "imp_stab", "imp_hurt",
    "wisp0", "wisp1", "wisp2", "wisp3", "wisp_hurt",
    "poof0", "poof1", "poof2", "poof3",
]

L = 512
GROUND = 171                        # bitmap row the feet stand on
FLOOR = GROUND - 20                 # box y of anything standing
HX = 144                            # the hero's home position (box x)
HERO_TUNIC = 14                     # light blue
BLACK, WHITE = 0, 1


@dataclass
class Hero:
    frame: str = "idle0"
    facing: int = 1
    x: float = HX
    y: float = FLOOR
    color: tuple = (BLACK, HERO_TUNIC)
    visible: bool = True


@dataclass
class Baddie:
    frame: str = "poof0"
    facing: int = 1
    x: float = 0
    y: float = 0
    color: int = BLACK
    visible: bool = False
    behind: bool = False


@dataclass
class Timeline:
    length: int
    hero: list = field(default_factory=list)
    sword: list = field(default_factory=list)        # None: from the pose; False: hidden; or a frame
    sword_color: list = field(default_factory=list)
    baddies: list = field(default_factory=list)      # per frame: 5 Baddie
    shake: list = field(default_factory=list)        # per frame: (dx, dy)


# ------------------------------------------------------------------ motion helpers
def lerp(a, b, u):
    return a + (b - a) * u


def smooth(u):
    return u * u * (3 - 2 * u)


def ease_out(u):
    return 1 - (1 - u) ** 2


def ease_in(u):
    return u * u


def arc(p0, p1, height, u):
    """Ballistic hop from p0 to p1 peaking `height` px above the higher end."""
    x = lerp(p0[0], p1[0], u)
    y = lerp(p0[1], p1[1], u) - 4 * height * u * (1 - u)
    return x, y


def cycle(frames, ticks, t, offset=0):
    return frames[((t + offset) // ticks) % len(frames)]


class Script:
    """An actor's states as clips: clip(t0, t1, fn) with fn(local t, duration) -> dict."""

    def __init__(self, default):
        self.clips = []
        self.default = default

    def clip(self, t0, t1, fn):
        self.clips.append((t0, t1, fn))
        return self

    def at(self, t):
        for t0, t1, fn in self.clips:
            if t0 <= t < t1:
                return {**self.default, **fn(t - t0, t1 - t0)}
        return dict(self.default)


def hidden(_t, _n):
    return {"visible": False}


# ------------------------------------------------------------------ the hits
# Frames where the blade connects (the impact flash and hit-stop start there), and where the
# box of each victim sits at that moment, worked out from the hero's fist and blade.
STOP = 4                            # hit-stop: hero and victim freeze, the victim flashes
HIT_WOLF, HIT_IMP, HIT_BAT, HIT_SPIDER, HIT_WISP = 81, 135, 200, 284, 396
HIT_HERO = 356                      # the wisp burns the hero
WOLF_AT = (HX + 26, FLOOR - 2)
IMP_AT = (HX - 20, FLOOR - 2)
IMP_HOME = 64
BAT_AT = (HX - 7, FLOOR - 29)
SPIDER_AT = (HX + 60, FLOOR)
WISP_AT = (HX - 13, FLOOR - 2)
WISP_BURN = (HX - 14, FLOOR - 8)


# ------------------------------------------------------------------ the hero
def hero_script():
    s = Script({"frame": "idle0", "facing": 1, "x": HX, "y": FLOOR, "sword": None, "color": (BLACK, HERO_TUNIC),
                "swordcol": WHITE})

    def idle(facing, x=HX, offset=0):
        return lambda t, n: {"frame": cycle(["idle0", "idle1"], 24, t, offset), "facing": facing, "x": x}

    def pose(frame, facing, x=HX, sword=None, y=FLOOR):
        return lambda t, n: {"frame": frame, "facing": facing, "x": x, "sword": sword, "y": y}

    def slash(hit, facing, x=HX):
        """windup 8 (drawing back a pixel), swing 3 (smear, lunging), the impact held for the
        hit-stop with the blade flashing gold, follow-through 14 easing back."""
        t0 = hit - 11
        s.clip(t0, t0 + 8, pose("windup", facing, x - facing))
        s.clip(t0 + 8, hit, pose("swing", facing, x + 2 * facing, "smear_slash"))
        s.clip(hit, hit + STOP, lambda t, n: {**pose("swing", facing, x + 3 * facing, "impact_slash")(t, n),
                                              "swordcol": 7 if t < 2 else WHITE})
        s.clip(hit + STOP, hit + STOP + 14, lambda t, n: {
            "frame": "follow", "facing": facing, "x": x + facing * (4 - 3 * smooth(t / n))})
        return hit + STOP + 14

    def run(t0, t1, facing, x0, x1):
        s.clip(t0, t1, lambda t, n: {"frame": cycle(["run0", "run1", "run2", "run3"], 4, t), "facing": facing,
                                     "x": lerp(x0, x1, t / n)})

    s.clip(0, HIT_WOLF - 11, idle(1))
    e = slash(HIT_WOLF, 1)                              # the wolf
    s.clip(e, 118, idle(1))
    s.clip(118, HIT_IMP - 11, idle(-1))                 # turn: the imp
    e = slash(HIT_IMP, -1)
    jump = HIT_BAT - 3                                  # the bat: crouch, rising slash
    s.clip(e, jump - 10, idle(-1))
    s.clip(jump - 10, jump, pose("crouch", -1))
    land = HX - 0.4 * 26
    s.clip(jump, jump + 26, lambda t, n: {"frame": "jump", "facing": -1, "x": HX - 0.4 * t,
                                          "y": FLOOR - 26 * math.sin(math.pi * t / n),
                                          "sword": "smear_rise" if t < 3 else "impact_rise" if t < 3 + STOP else None,
                                          "swordcol": 7 if 3 <= t < 5 else WHITE})
    s.clip(jump + 26, jump + 34, pose("crouch", -1, land))
    s.clip(jump + 34, 250, idle(-1, land))
    run(250, HIT_SPIDER - 11, 1, land, HX + 34)          # the spider: run in and cut
    e = slash(HIT_SPIDER, 1, HX + 34)
    s.clip(e, 312, idle(1, HX + 34))
    run(312, 334, -1, HX + 34, HX)
    s.clip(334, HIT_HERO, idle(-1))
    s.clip(HIT_HERO, HIT_HERO + 20, lambda t, n: {      # burned by the wisp: knocked back, blinking
        "frame": "hurt", "facing": -1, "x": HX + 8 * ease_out(min(1, t / 8)),
        "color": (WHITE, WHITE) if t % 4 < 2 and t < 12 else (BLACK, HERO_TUNIC)})
    s.clip(HIT_HERO + 20, HIT_WISP - 11, idle(-1, HX + 8))
    e = slash(HIT_WISP, -1, HX + 8)                     # the wisp
    s.clip(e, 425, idle(-1, HX + 8))
    s.clip(425, 434, lambda t, n: {"frame": "idle0", "facing": 1, "x": HX + 8 - 8 * smooth(t / n)})
    s.clip(434, 474, lambda t, n: {**pose("victory", 1)(t, n),        # the blade glints
                                   "swordcol": [WHITE, 7, WHITE, 15][(t // 3) % 4] if 6 <= t < 30 else WHITE})
    s.clip(474, L, idle(1))
    return s


# ------------------------------------------------------------------ the creatures
POOF = ["poof0", "poof1", "poof2", "poof3"]


def knocked(frame, p0, p1, height, color=BLACK):
    """Flying back after a hit: a ballistic arc, flashing white for the first frames."""
    return lambda t, n: {**dict(zip(("x", "y"), arc(p0, p1, height, t / n))), "frame": frame,
                         "color": WHITE if t < 3 else color}


def frozen(frame, p, color=WHITE):
    return lambda t, n: {"frame": frame, "x": p[0], "y": p[1], "color": color}


def poof(p):
    return lambda t, n: {"frame": cycle(POOF, 4, t), "x": p[0], "y": p[1], "color": WHITE}


def wolf_script():
    s = Script({"frame": "wolf_snarl0", "facing": -1, "x": 232, "y": FLOOR, "visible": True, "color": BLACK})
    snarl = ["wolf_snarl0", "wolf_snarl1"]
    run = ["wolf_run0", "wolf_run1", "wolf_run2", "wolf_run3"]
    h = HIT_WOLF
    s.clip(0, 40, lambda t, n: {"frame": cycle(snarl, 10, t), "x": 232 - 10 * t / n})
    s.clip(40, 48, lambda t, n: {"frame": "wolf_snarl1", "x": 222 + 4 * smooth(t / n)})       # gather
    s.clip(48, h - 13, lambda t, n: {"frame": cycle(run, 3, t), "x": 226 - 46 * ease_in(t / n)})
    s.clip(h - 13, h, lambda t, n: {**dict(zip(("x", "y"), arc((180, FLOOR), WOLF_AT, 10, t / n))),
                                    "frame": "wolf_leap"})
    s.clip(h, h + STOP, frozen("wolf_hurt", WOLF_AT))
    s.clip(h + STOP, h + 28, knocked("wolf_hurt", WOLF_AT, (250, FLOOR), 24))
    s.clip(h + 28, h + 40, lambda t, n: {"frame": "wolf_hurt", "x": 250 + 8 * ease_out(t / n)})  # skid
    s.clip(h + 40, h + 70, lambda t, n: {"frame": cycle(run, 3, t), "facing": 1, "x": 258 + 80 * ease_in(t / n)})
    s.clip(h + 70, 300, hidden)
    s.clip(300, 340, lambda t, n: {"frame": cycle(run, 4, t), "x": 334 - 84 * ease_out(t / n)})
    s.clip(340, 500, lambda t, n: {"frame": cycle(snarl, 10, t), "x": 250 - 18 * smooth(t / n)})
    s.clip(500, L, lambda t, n: {"frame": cycle(snarl, 10, t), "x": 232})
    return s


def imp_script():
    s = Script({"frame": "imp_idle0", "facing": 1, "x": IMP_HOME, "y": FLOOR, "visible": True, "color": BLACK})

    def hop(x0, x1, height, period):
        def fn(t, n):
            u = (t % period) / period
            k = t // period
            hops = max(1, n // period)
            xa = lerp(x0, x1, k / hops)
            xb = lerp(x0, x1, min(1, (k + 1) / hops))
            x, y = arc((xa, FLOOR), (xb, FLOOR), height, u)
            return {"frame": "imp_idle1" if 0.15 < u < 0.85 else "imp_idle0", "x": x, "y": y}
        return fn

    def taunt(t, n):                                   # jabs the pitchfork at the hero
        return {"frame": "imp_stab" if t < n - 3 else "imp_idle0", "x": IMP_HOME + 3 * math.sin(math.pi * t / n)}

    h = HIT_IMP
    s.clip(0, 48, hop(IMP_HOME, IMP_HOME, 5, 16))
    s.clip(48, 64, taunt)
    s.clip(64, 96, hop(IMP_HOME, IMP_HOME, 5, 16))
    s.clip(96, 120, hop(IMP_HOME, 100, 8, 12))
    s.clip(120, h - 11, lambda t, n: {"frame": "imp_idle0", "x": 100})                          # crouch
    s.clip(h - 11, h, lambda t, n: {**dict(zip(("x", "y"), arc((100, FLOOR), IMP_AT, 12, t / n))),
                                    "frame": "imp_leap"})
    s.clip(h, h + STOP, frozen("imp_hurt", IMP_AT))
    s.clip(h + STOP, h + 23, knocked("imp_hurt", IMP_AT, (IMP_HOME, FLOOR), 18))
    s.clip(h + 23, h + 39, poof((IMP_HOME, FLOOR)))
    s.clip(h + 39, 262, hidden)
    s.clip(262, 304, hop(-26, IMP_HOME, 8, 14))
    s.clip(304, 400, hop(IMP_HOME, IMP_HOME, 5, 16))
    s.clip(400, 416, taunt)
    s.clip(416, L, hop(IMP_HOME, IMP_HOME, 5, 16))
    return s


def bat_xy(t):
    """The bat's patrol: a lazy figure eight over the right half of the clearing."""
    a = 2 * math.pi * t / 128
    return 196 + 54 * math.sin(a), 74 + 12 * math.sin(2 * a)


def bat_script():
    fly = ["bat_fly0", "bat_fly1", "bat_fly2", "bat_fly3"]
    s = Script({"frame": "bat_fly0", "facing": 1, "x": 0, "y": 0, "visible": True, "color": BLACK})

    def patrol(t, n, t0):
        x, y = bat_xy(t0 + t)
        x1, _ = bat_xy(t0 + t + 1)
        return {"frame": cycle(fly, 3, t0 + t), "x": x, "y": y}

    h = HIT_BAT
    s.clip(0, 170, lambda t, n: patrol(t, n, 0))
    start = bat_xy(170)
    s.clip(170, 182, lambda t, n: {"frame": cycle(fly, 2, t), "x": start[0] - 6 * t / n,
                                   "y": start[1] - 8 * smooth(t / n)})                              # rear up
    s.clip(182, h, lambda t, n: {"frame": "bat_dive", "facing": -1,
                                 "x": lerp(start[0] - 6, BAT_AT[0], ease_in(t / n)),
                                 "y": lerp(start[1] - 8, BAT_AT[1], ease_in(t / n))})
    s.clip(h, h + STOP, frozen("bat_hurt", BAT_AT))
    s.clip(h + STOP, h + 18, knocked("bat_hurt", BAT_AT, (90, 96), 20))
    s.clip(h + 18, h + 34, poof((90, 96)))
    s.clip(h + 34, 330, hidden)
    entry = bat_xy(370)
    s.clip(330, 370, lambda t, n: {"frame": cycle(fly, 3, t),
                                   "x": lerp(-24, entry[0], smooth(t / n)), "y": lerp(30, entry[1], smooth(t / n))})
    s.clip(370, L, lambda t, n: patrol(t, n, 370))
    return s


def spider_script():
    walk = ["spider_walk0", "spider_walk1", "spider_walk2", "spider_walk3"]
    s = Script({"frame": "spider_walk0", "facing": 1, "x": 0, "y": FLOOR, "visible": True, "color": BLACK})
    h = HIT_SPIDER
    x0 = SPIDER_AT[0]
    s.clip(0, 150, hidden)
    s.clip(150, 196, lambda t, n: {"frame": cycle(walk, 4, t), "x": 334 - 84 * t / n})
    s.clip(196, 250, lambda t, n: {"frame": cycle(walk, 4, t), "x": 250 - 6 * math.sin(2 * math.pi * t / 54)})
    s.clip(250, 266, lambda t, n: {"frame": cycle(walk, 3, t), "x": lerp(250, x0, t / n)})
    s.clip(266, h, lambda t, n: {"frame": "spider_rear", "x": x0})
    s.clip(h, h + STOP, frozen("spider_hurt", SPIDER_AT))
    s.clip(h + STOP, h + 20, knocked("spider_hurt", SPIDER_AT, (x0 + 24, FLOOR), 10))
    s.clip(h + 20, h + 36, poof((x0 + 24, FLOOR)))
    s.clip(h + 36, L, hidden)
    return s


def wisp_xy(t):
    """The wisp's drift over the left half, slow and wavering."""
    a = 2 * math.pi * t / 256
    return 62 + 22 * math.sin(a), 96 + 9 * math.sin(3 * a + 0.5)


def wisp_script():
    flick = ["wisp0", "wisp1", "wisp2", "wisp3"]
    s = Script({"frame": "wisp0", "facing": 1, "x": 0, "y": 0, "visible": True, "color": WHITE})

    def drift(t, n, t0):
        x, y = wisp_xy(t0 + t)
        return {"frame": cycle(flick, 5, t0 + t), "x": x, "y": y}

    h = HIT_WISP
    s.clip(0, 330, lambda t, n: drift(t, n, 0))
    p0 = wisp_xy(330)
    s.clip(330, HIT_HERO, lambda t, n: {                # swoop up and over, into the hero's face
        "frame": cycle(flick, 3, t), "x": lerp(p0[0], WISP_BURN[0], ease_in(t / n)),
        "y": lerp(p0[1], WISP_BURN[1], t / n) - 24 * math.sin(math.pi * t / n)})
    hover = (WISP_AT[0] - 20, WISP_AT[1] - 10)
    s.clip(HIT_HERO, HIT_HERO + 16, lambda t, n: {"frame": cycle(flick, 3, t),
                                                  **dict(zip(("x", "y"), arc(WISP_BURN, hover, 10, t / n)))})
    s.clip(HIT_HERO + 16, h, lambda t, n: {"frame": cycle(flick, 4, t),
                                           "x": lerp(hover[0], WISP_AT[0], ease_in(t / n)),
                                           "y": lerp(hover[1], WISP_AT[1], smooth(t / n))})
    s.clip(h, h + STOP, lambda t, n: {"frame": "wisp_hurt", "x": WISP_AT[0], "y": WISP_AT[1],
                                      "color": 7 if t % 2 else WHITE})
    s.clip(h + STOP, h + 20, poof(WISP_AT))
    s.clip(h + 20, 460, hidden)
    p1 = wisp_xy(L)
    s.clip(460, 476, lambda t, n: {"frame": cycle(POOF[::-1], 4, t), "x": p1[0], "y": p1[1]})
    s.clip(476, L, lambda t, n: {"frame": cycle(flick, 5, t), "x": p1[0], "y": p1[1] + 2 * math.sin(math.pi * t / n)})
    return s


SHAKES = {HIT_WOLF: 3, HIT_IMP: 2, HIT_BAT: 2, HIT_SPIDER: 3, HIT_WISP: 4, HIT_HERO: 2}
SHAKE_PATTERN = [(2, 1), (-2, -1), (1, 0), (-1, 1), (1, -1), (-1, 0), (1, 0), (0, 0)]


def direct(poses: dict) -> Timeline:
    tl = Timeline(L)
    hero = hero_script()
    creatures = [wolf_script(), imp_script(), spider_script(), bat_script(), wisp_script()]
    for t in range(L):
        h = hero.at(t)
        tl.hero.append(Hero(h["frame"], h["facing"], h["x"], h["y"], h["color"]))
        tl.sword.append(h["sword"])
        tl.sword_color.append(h["swordcol"])
        bs = []
        for c in creatures:
            d = c.at(t)
            bs.append(Baddie(d["frame"], d["facing"], d["x"], d["y"], d["color"], d["visible"], d.get("behind", False)))
        tl.baddies.append(bs)
        dx = dy = 0
        for t0, strength in SHAKES.items():
            k = t - t0
            if 0 <= k < len(SHAKE_PATTERN):
                fx, fy = SHAKE_PATTERN[k]
                dx, dy = int(round(fx * strength / 3)), int(round(fy * strength / 3))
        tl.shake.append((max(-2, min(2, dx)), max(-2, min(2, dy))))
    return tl

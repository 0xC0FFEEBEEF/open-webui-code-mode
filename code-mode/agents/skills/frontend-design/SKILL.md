---
name: Frontend design
description: How to design a page, dashboard or app so it looks made by a person with taste instead of generated. Load this before writing or restyling any HTML or CSS, before making design ideas or mockups, and whenever the user says a page looks generic, plain, ugly or "AI".
---

# Designing pages that don't look generated

A page looks generated when nobody decided anything: default font, default purple, everything in the same rounded
card, the same gap everywhere. Good design is a few clear decisions, held to everywhere. Make the decisions first,
write them down, then build to them. Work through the steps in order.

## 1. Decide before you write any CSS

Write these four lines in your reply (or your plan) before the first style rule:

- **Who and what for.** Who opens this page, how often, and what do they want from it in the first three seconds?
- **The one thing.** What is the single most important thing on the page? It gets the most size, weight and space.
  Everything else is clearly second or third.
- **The direction.** One sentence naming the feel, concrete enough that two people would draw something similar:
  "a flight instrument panel: black, dense, monospace numbers, thin lines", "a printed annual report: off-white
  paper, a serif headline, one big chart", "a transit map: bold flat colour, heavy sans, strict grid".
  Not "clean and modern": that describes nothing.
- **Type and colour.** The one or two typefaces, the background, the text colour and the single accent colour.

If the user gave a direction, that is the direction. If the look is open and the user has not said to just pick
one, make an ideas board (section 2) and let them choose. If they gave none and want it built now, use their known
taste (section 7).

## 2. Ideas board: show options before you commit

Use this when the user asks for ideas, options, variations or a redesign, or when a new project's look is open.
It costs a few minutes and saves rebuilding the whole page later.

1. Think of **three directions that differ in structure and feel**, not three colour schemes of one layout. Change
   what is biggest, how dense it is, the typefaces, and where things sit. For example a dense instrument panel, an
   editorial page with one large chart, and a bold poster with huge numbers.
2. Make each one a small, self-contained page of the **same key screen**: `ideas/a-<name>.html`,
   `ideas/b-<name>.html`, `ideas/c-<name>.html`. Styles inline in a `<style>` block, no JavaScript needed, about a
   screen tall. Use the real content: real labels, and real values if you already have them. If you don't, use
   plausible ones; the board says they are mockups, so that is honest here (and only here).
3. Copy the board next to them and fill it in:
   `cp ~/.config/opencode/skills/frontend-design/board.html ideas/index.html`, then edit only the two places marked
   `EDIT`: the heading, and one `<a href="a-<name>.html" data-name="Short name">one sentence</a>` line per idea.
4. Make it what the user sees. If the project has no `index.html` yet, write one that only sends the browser to the
   board, and replace it when you build the real page:
   `<!doctype html><meta http-equiv="refresh" content="0; url=ideas/"><a href="ideas/">Ideas board</a>`
   If the project already has its page, leave it alone; the board is at `ideas/` in the Preview.
5. Open the board on the desktop, look at each idea at full size (press 1, 2, 3; 0 goes back), fix what looks off,
   run `check` and `desktop verify` on the board address (`.../ideas/`).
6. End your turn with the three names, one line each on what it goes for, which one you would pick and why, and
   the question: which one, or which mix? Then stop. Build the real page only after the user answers, starting
   from the chosen idea's tokens.

## 3. Tokens first

Start the stylesheet with custom properties and use only these in the rules below. A value that appears as a raw
number or colour further down is a decision nobody made.

```css
:root {
  /* colour: neutrals do nine tenths of the work, one accent does the rest */
  --bg: ...; --surface: ...; --line: ...; --text: ...; --text-dim: ...; --accent: ...;
  --good: ...; --warn: ...; --bad: ...;            /* only where the colour means something */
  /* type: at most five sizes, each about 1.25x the one before */
  --font-text: ...; --font-display: ...; --font-mono: ...;
  --size-1: 0.8rem; --size-2: 1rem; --size-3: 1.25rem; --size-4: 1.6rem; --size-5: 2.6rem;
  /* space: every margin, padding and gap comes from this scale */
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 24px; --space-6: 40px; --space-7: 64px;
  --radius: ...;                                    /* one value; 0 to 6px reads sharper than 12px+ */
}
```

## 4. The rules

**Type.** Pick a typeface that fits the direction, and at most two families (one for text, one for headings or
for numbers). Not Inter, Roboto, Arial or the system font for everything: that is the default nobody chose. Load
them from Google Fonts with a `<link>` in the head, and check the name exists (`desktop errors` shows a failed font
request). Some that work well: IBM Plex Sans + IBM Plex Mono, Instrument Sans, Schibsted Grotesk, Hanken Grotesk,
Archivo, Bricolage Grotesque, Barlow Condensed (labels, big numbers), Fraunces, Newsreader, Instrument Serif,
Source Serif 4, JetBrains Mono, Martian Mono. Numbers that change or sit in columns get
`font-variant-numeric: tabular-nums`. Body text 45 to 75 characters per line. Use weight and size for hierarchy:
two weights are usually enough.

**Colour.** One accent, used rarely: the live value, the thing to click, the item that is selected. If everything
is coloured, nothing stands out. Tint the neutrals slightly warm or cool instead of pure grey, and avoid pure
`#000` text on pure `#fff`. Body text needs strong contrast with its background (4.5:1 or more). Red, amber and
green mean bad, warning and good and are used for nothing else.

**Layout.** Group things with space and alignment, not with boxes. A border or a background is for the rare thing
that must be set apart. Never a box inside a box. Line elements up on shared left edges; most text is left
aligned, not centred. Give the important thing room and let secondary things be small and tight: uneven is better
than uniform. A 1px line in `--line` separates better than a shadow.

**Data and dashboards.** Every number has a label, a unit and a time ("updated 14:02 UTC"). The key reading is
large; its context (change, range, threshold) is small and next to it. Time runs left to right, oldest to newest:
check the order of the data before you draw it. Charts use the accent and neutrals, with other colours only for
thresholds; no rainbow series, no 3D, no heavy gridlines. Right-align columns of numbers. Design the empty, loading
and error states too: say what is missing and why, in the page's own style.

**Details.** Every clickable thing has a hover and a `:focus-visible` style. The page works at 360px wide (one
column, nothing cut off, no sideways scroll) and at 1600px (content does not stretch edge to edge). Icons are
simple inline SVG in one stroke weight, or nothing; never emoji.

**Motion.** Little and with a reason: something that changed, something that opened. 150 to 250ms, ease-out.
Respect `prefers-reduced-motion`. Nothing loops, bounces or pulses for decoration.

**Words.** Write the text for this page and this user: specific labels, real names, short sentences. No "Welcome
to", no taglines, no "powered by", no invented statistics, no lorem ipsum.

## 5. The tells: what makes a page look generated

If you notice one of these in your page, replace it with a decision:

- purple, indigo or blue-to-pink gradients; gradient-filled headline text; glowing or neon shadows
- frosted-glass panels; a soft shadow under every element
- everything in rounded cards; cards inside cards; three identical cards with an icon, a title and a sentence
- a centred hero with a huge headline, a subtitle and two buttons, on something that is really a tool
- emoji used as icons or in headings
- one font at one weight; every heading the same size; the same gap between everything
- a different bright colour for each item; dark theme made of blue-grey panels with a bright blue button
- everything lifts or grows on hover; everything fades in
- filler words, fake numbers, a footer with "Made with" in it

## 6. Look at it, then fix it. Twice.

You cannot judge a design from its code. After the page works:

1. `desktop screenshot /tmp/look.png` and read the image (the small numbered tags on it are from the tool, not
   the page). Scroll and take another if the page is longer than the screen.
2. Answer these honestly, from the picture, in your notes:
   - What are the first three things the eye lands on? Are they the three that matter most?
   - Swap the text for another topic: would it be just another generated page? Which tell from section 5 is here?
   - How many borders or backgrounds are nested at the deepest point? More than one is a box in a box.
   - How many colours and how many text sizes are there? Do the left edges line up?
   - Is anything cut off, overlapping, cramped, or floating in empty space?
3. Get an outside opinion from fresh eyes that have not seen your code:
   `desktop see "You are a strict senior product designer. Look at this page. First: what does the eye land on, and is it the most important thing? Then list the five worst design problems you see, most serious first, each with the exact element and what to change. Say plainly if it looks like a generic generated template and why."`
4. Fix what you found and what it reported, then look again. Do at least two rounds. Stop when a round finds
   nothing serious, not when you are tired of it.

## 7. The user's own taste

When the user gives no direction and does not want options, use what they have asked for before: dark and flat
with a true black background, dense with real information, open rows separated by hairlines, **no boxes inside
boxes**, one restrained accent, small and quick motion, nothing decorative. Minimal, like an instrument, not like
a marketing page. When they do give a direction, theirs wins over this.

## 8. When you finish

Tell the user in two or three lines what you decided (direction, typefaces, accent) so they can steer it, and what
the last look showed.

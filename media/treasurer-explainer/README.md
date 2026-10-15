# Treasurer — 30-second motion graphic

Deliverable: `treasurer-30s.mp4`, 1920×1080, 30 fps, H.264/AAC, exactly 30 seconds.
Includes burned-in narration captions, a synthesized English voiceover, and an original procedural music bed. The separate `.srt` file is available for reuse.

This is an illustrated product explainer. The example records are explanatory graphics; they are not a recording of executed Stripe transactions.

## Story

| Time | Question | Message |
|---|---|---|
| 00–04 | What? | A spending agent that explains every pound. |
| 04–08 | Why? | Faster purchases with limits and accountability. |
| 08–15 | Where? | Local inbox files → Treasurer → Stripe test mode. |
| 15–20.5 | How? | Extract the request, recall Cognee notes, evaluate written policy. |
| 20.5–24.5 | How? | Clear, request approval, hold, or decline. |
| 24.5–30 | How? | Explain the rule, evidence, and decision history. |

## Visual target and reference lock

The existing `ui/index.html` is the build target: Schibsted Grotesk, JetBrains Mono, forest green `#1D4A3A`, neutral canvas `#F5F6F4`, ink `#17211D`, and its green/amber/purple/red status colors. Preserve these roles and the card's restrained ring motif.

Refero research reviewed fintech, graphic typography, and Stripe product directions. Primary supporting reference: [N26](https://n26.com), Refero style `59911817-9d14-445a-9f1b-617418001061`. Preserve clear ledger structure, flat color fields, high contrast, generous whitespace, and explanatory product graphics. Secondary reference: [Stripe](https://stripe.com), style `ff64110d-58dd-4e18-a500-0a95073943b1`; borrow only contained product graphics and the text/graphic narrative. Omit its gradient and violet action system. The existing Treasurer brand owns all actual colors and typography.

| Decision | Source | Role / reason |
|---|---|---|
| Forest opening and closing; pale middle scenes | Existing app + N26's flat brand hero | Brand continuity and clear chapter changes. |
| Large sans headlines; mono rule IDs and amounts | Existing app + N26 hierarchy | Readable at video scale; evidence distinct from narrative. |
| Request → gates → evidence receipt | User's What/Why/Where/How brief + app flow | Motion explains how a request changes state. |
| Semantic status colors | Existing UI | Green clear, amber approval, purple hold, red decline. |
| Short eased entrances; moving request token | Refero `references/motion.md` | Continuity and attention, with stable reading holds. |
| Native vector primitives | Explanatory motion graphic brief + N26 imagery | Crisp, reproducible graphics; no bitmap asset generation needed. |

No live mailbox integration is implied. Payments are explicitly in Stripe test mode. No invented authorization IDs or completed-payment claims are used.

## Re-render

Requires Python with Pillow and NumPy plus FFmpeg on PATH. No application dependencies, secrets, Stripe access, or Cognee access are used.

```powershell
python media/treasurer-explainer/render.py --preview
python media/treasurer-explainer/render.py
```

The source downloads Google Fonts once into `assets/` and retains the OFL licenses. `audio/` contains the six source narration clips and their UTF-8 scripts. `generate_voice.ps1` can regenerate narration using Windows System.Speech. The renderer builds the 30-second mix, captions, animation, and MP4.

## Validation

Verified final MP4: 30.000 seconds, 900 video frames, 1920×1080 at 30 fps, H.264 video, stereo AAC at 48 kHz. FFmpeg decoded the complete file without errors. Source storyboard and an extracted encoded frame were visually reviewed against the existing app brand and reference lock; card clipping and rule wording were corrected. Captions fit inside the safe area and all six narration clips fit their scene windows. Machine-readable media metadata is retained in `validation.json`.

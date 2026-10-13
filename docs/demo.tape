# The README's demo GIF: `make demo` (needs vhs, Docker and the local stack's images).
# A real run of scripts/demo.py (about 85 s), recorded by vhs (charm.sh/vhs) and played back in ~60 s.
Output docs/demo.gif

Require uv
Require docker

Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 15
Set Width 1440
Set Height 680
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms
Set PlaybackSpeed 1.45

# Off camera: start the app with the demo's admission and retry settings.
Hide
# The markers are split in the typed command so only the echo's output can match them.
Type "uv run python scripts/demo.py --prepare > /dev/null 2>&1 && echo PRE''PARED"
Enter
Wait+Screen@600s /PREPARED/
Type "clear"
Enter
Sleep 500ms
Show

Type "uv run python scripts/demo.py"
Sleep 400ms
Enter
Wait+Screen@600s /dead letters/
Sleep 5s

Hide
Type "uv run python scripts/demo.py --stop > /dev/null 2>&1 && echo STO''PPED"
Enter
Wait+Screen@120s /STOPPED/

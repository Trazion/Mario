# Mario Camera Streamer — Keyboard Shortcuts (v3.5)

The web UI listens for the following keys when no input/textarea is focused.
(Implementation lives in `templates/index.html`. Add the listener block in
DOMContentLoaded if it isn't wired yet.)

| Key            | Action                                  |
|----------------|-----------------------------------------|
| `Space`        | Start / stop the stream                 |
| `N` / `→`      | Skip to next clip                       |
| `P` / `←`      | (not yet — previous clip)               |
| `S`            | Toggle shuffle mode                     |
| `R`            | Toggle auto-restart                     |
| `M`            | Mute / unmute audio                     |
| `F`            | Toggle fullscreen preview               |
| `,` / `.`      | Volume down / up (steps of 0.1)         |
| `1`–`9`        | Jump to playlist index 1–9              |
| `?`            | Show shortcuts overlay                  |
| `Esc`          | Close any open modal / overlay          |

## Wiring example (drop into `index.html`)

```js
document.addEventListener('keydown', (e) => {
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  switch (e.key) {
    case ' ': e.preventDefault(); window.toggleStream?.(); break;
    case 'n': case 'ArrowRight': window.skipNext?.(); break;
    case 's': window.toggleShuffle?.(); break;
    case 'r': window.toggleAutoRestart?.(); break;
    case 'm': window.toggleMute?.(); break;
    case 'f': document.getElementById('preview')?.requestFullscreen?.(); break;
    case ',': window.changeVolume?.(-0.1); break;
    case '.': window.changeVolume?.(+0.1); break;
    case '?': window.showShortcutsHelp?.(); break;
    case 'Escape': document.querySelectorAll('.modal.open').forEach(m=>m.classList.remove('open')); break;
    default:
      if (/^[1-9]$/.test(e.key)) window.jumpTo?.(parseInt(e.key,10)-1);
  }
});
```

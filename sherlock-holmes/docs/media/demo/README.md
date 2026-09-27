# Demo GIF

`../error-sweep-demo.gif` is real output of the plugin on the bundled, synthetic fixtures, rendered in
a terminal-like page and captured frame by frame. To regenerate it after a change to the sweep, the
diagram or the report format:

1. In a scratch directory, produce the inputs with the installed plugin:
   - `sweep.txt` - `sweep-errors.py --format table --fixture <plugin>/skills/error-sweep/fixtures/cascade`
   - `map.txt` and `map.html` - `window-map.py --start 2026-09-17T13:00:00.000Z --end 2026-09-17T14:10:00.000Z --fixture <plugin>/skills/error-sweep/fixtures/cascade --out map.html > map.txt`
   - `t.json` - `collect-trace.py --trace-id 2aa803b2e40c97a2490d754a465fe9de --source tempo --fixture <plugin>/evals/fixtures/01-downstream-503 --out t.json`
   - `diagram.html` - `trace-diagram.py --input t.json --out diagram.html`
   - `report-excerpt.md` - an excerpt of a real `trace-debug` report on the same fixture (the one used
     is kept here; it is model output, so a new run will differ in wording).
2. Copy `stage.html`, `drive.mjs` and `capture.mjs` there and run `node capture.mjs "$PWD"` (Node 22+,
   Google Chrome; it starts its own headless Chrome on port 9336 - `CDP_PORT` to change - and writes
   `frames/` plus `frames/list.txt`). The map scene uses real mouse clicks and keys on the archify
   viewer; the coordinates in `capture.mjs` match the 1200x860 viewport and the cascade map, so a
   change in the map's layout means re-reading them (`drive.mjs` has a `center(selector)` helper).
3. Assemble:

   ```bash
   ffmpeg -f concat -safe 0 -i frames/list.txt \
     -vf "split[a][b];[a]palettegen=stats_mode=diff:max_colors=160[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
     -loop 0 error-sweep-demo.gif
   ```

Use fixtures only: this repository is public, and a real trace carries service names, routes and ids.
